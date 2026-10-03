# -*- coding: utf-8 -*-
"""
change-bios-logo —— BIOS 开机 Logo 修改工具（Windows 桌面版）
=============================================================

界面流程：
  1. 【载入 BIOS 文件】 选择固件镜像
  2. 【Logo 解析】     从固件里解出开机 Logo，显示位置 / 规格 / 预览；
                        点击预览图即可把 Logo 备份保存到工具所在目录
  3. 【上传新 Logo】   选择图片（限图片格式，≤ 1 MB）
  4. 【Logo 替换】     等长替换并重新打包成新的 BIOS 文件

硬性保证：只重写 Logo 所在压缩段的字节，段头 / FFS 头 / FV 头 /
其余全部内容逐字节保留，文件总长不变，不触碰固件代码。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import bioslogo as B

APP_NAME = "change-bios-logo"
APP_TITLE = "change-bios-logo — BIOS 开机 Logo 修改工具"
PREVIEW_BOX = (210, 286)                   # 预览缩略图最大尺寸

# 上传图片不再限制体积：真正决定成败的是渲染出来的 293x400 画布能否压进 Logo 段，
# 与输入图片自身多大无关（大图会被等比缩放）。这里只留一个明显异常的保护值，
# 避免误选几十 GB 的文件把内存吃光。
MAX_LOGO_BYTES = 256 * 1024 * 1024

TRIM_THRESHOLD = 10                        # 判定"黑边"的亮度阈值
ZOOM_MIN, ZOOM_MAX = 0.30, 3.00

# 输出 Logo 的尺寸策略：不再要求与原 Logo 的尺寸或比例一致
SIZE_ORIG = "orig"          # 跟原 Logo 一样：沿用原 Logo 的像素尺寸与 BMP 头（最保险）
SIZE_SOURCE = "source"      # 原图：用上传图片自己的像素尺寸，一个像素都不缩放
SIZE_FIXED = "fixed"        # 固定 720 × 480（默认尺寸）
SIZE_CUSTOM = "custom"      # 用户手填 W × H
SIZE_MODES = (SIZE_ORIG, SIZE_SOURCE, SIZE_FIXED, SIZE_CUSTOM)
SIZE_LABELS = {
    SIZE_ORIG: "跟原 Logo 一样（最保险）",
    SIZE_SOURCE: "原图（上传图片的像素尺寸）",
    SIZE_FIXED: "720×480",
    SIZE_CUSTOM: "自定义（可填 W×H）",
}
SIZE_FIXED_WH = (720, 480)
SIZE_MIN, SIZE_MAX = 8, 8192

# 输出颜色位数：位数越低，同样的画面压缩后越小，也就能用越大的尺寸。
# 注意：整份固件里作为开机 Logo 的 BMP 只有 24bpp 一处，没有任何调色板位图的
# 先例，所以 8/4/1 位都属于「未在真机验证过」的实验选项。
BPP_24, BPP_8, BPP_4, BPP_1 = 24, 8, 4, 1
BPP_MODES = (BPP_24, BPP_8, BPP_4, BPP_1)
BPP_LABELS = {
    BPP_24: "24 位（与原 Logo 同格式，默认）",
    BPP_8: "8 位 / 256 色（未验证）",
    BPP_4: "4 位 / 16 色（未验证，最省）",
    BPP_1: "1 位 / 黑白（未验证）",
}
BPP_TIPS = {
    BPP_24: "● 24 位：与原固件 Logo 完全相同的格式，兼容性最好 —— 请先确认这一档能正常开机显示。",
    BPP_8: "● 8 位：256 色调色板。整份固件里没有调色板 Logo 的先例，属于未验证选项。",
    BPP_4: "● 4 位：16 色调色板，同样画面能放大约一倍，但整份固件里没有先例，属于未验证选项。",
    BPP_1: "● 1 位：纯黑白、无抗锯齿，极其省空间，同样属于未验证选项。",
}
# 未压缩数据超过这个体积就跳过「容量预检」，免得为一张必然放不下的巨图白等很久
PROBE_RAW_LIMIT = 8_000_000

IMAGE_TYPES = [
    ("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff"),
    ("PNG", "*.png"), ("JPEG", "*.jpg *.jpeg"), ("BMP", "*.bmp"),
    ("GIF", "*.gif"), ("WebP", "*.webp"), ("所有文件", "*.*"),
]
BIOS_TYPES = [
    ("BIOS 固件", "*.F44d *.f44d *.bin *.rom *.cap *.fd *.BIN *.ROM *.CAP"),
    ("所有文件", "*.*"),
]


# --------------------------------------------------------------------------
def tool_dir() -> Path:
    """工具自身所在目录（打包成 exe 后就是 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1024.0
    return f"{n}"


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


# --------------------------------------------------------------------------
class CropDialog(tk.Toplevel):
    """手动框选裁剪对话框。关闭后读 self.result（全分辨率 PIL 图；取消为 None）。"""

    MAX_W, MAX_H = 860, 560

    def __init__(self, parent, image: Image.Image, aspect=None):
        super().__init__(parent)
        self.title("裁剪图片")
        self.transient(parent)
        self.resizable(False, False)
        self.src = image.convert("RGB")
        self.aspect = aspect                 # (aw, ah) 或 None
        self.result: Image.Image | None = None

        self.scale = min(self.MAX_W / self.src.width,
                         self.MAX_H / self.src.height, 1.0)
        self.dw = max(1, round(self.src.width * self.scale))
        self.dh = max(1, round(self.src.height * self.scale))

        self.photo = ImageTk.PhotoImage(
            self.src.resize((self.dw, self.dh), Image.LANCZOS))

        wrap = ttk.Frame(self, padding=10)
        wrap.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(wrap, width=self.dw, height=self.dh,
                                highlightthickness=1, highlightbackground="#555",
                                cursor="crosshair")
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        self.sel = (0, 0, self.dw, self.dh)
        self._ax = self._ay = 0
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)

        self.lock = tk.BooleanVar(value=bool(aspect))
        bar = ttk.Frame(wrap)
        bar.pack(fill="x", pady=(8, 0))
        tip = "锁定比例"
        if aspect:
            tip += f" {aspect[0]}:{aspect[1]}"
        ttk.Checkbutton(bar, text=tip, variable=self.lock).pack(side="left")
        ttk.Button(bar, text="全选", command=self._sel_all).pack(side="left", padx=(10, 4))
        ttk.Button(bar, text="按目标比例", command=self._sel_aspect).pack(side="left")
        ttk.Button(bar, text="去黑边", command=self._sel_content).pack(side="left", padx=4)
        ttk.Button(bar, text="还原", command=self._sel_all).pack(side="left")

        self.size_lab = ttk.Label(wrap, text="", style="Hint.TLabel")
        self.size_lab.pack(anchor="w", pady=(6, 0))

        foot = ttk.Frame(wrap)
        foot.pack(fill="x", pady=(8, 0))
        ttk.Button(foot, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(foot, text="确定", command=self._ok).pack(side="right", padx=6)

        self._draw()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._ok())
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            self.geometry(f"+{px + max(0, (pw - self.winfo_width()) // 2)}"
                          f"+{py + max(0, (ph - self.winfo_height()) // 3)}")
        except Exception:
            pass
        self.grab_set()
        self.canvas.focus_set()

    # ---------------------------------------------------------- 选择框
    def _press(self, ev):
        self._ax, self._ay = ev.x, ev.y
        self.sel = (ev.x, ev.y, ev.x, ev.y)
        self._draw()

    def _drag(self, ev):
        x1 = min(max(ev.x, 0), self.dw)
        y1 = min(max(ev.y, 0), self.dh)
        r = (min(self._ax, x1), min(self._ay, y1),
             max(self._ax, x1), max(self._ay, y1))
        if self.lock.get() and self.aspect:
            r = self._fit_aspect(r, x1, y1)
        self.sel = r
        self._draw()

    def _release(self, _ev):
        r = self.sel
        if r[2] - r[0] < 4 or r[3] - r[1] < 4:
            self.sel = (0, 0, self.dw, self.dh)
        self._draw()

    def _fit_aspect(self, r, bx, by):
        ar = self.aspect[0] / self.aspect[1]
        w = max(4.0, float(r[2] - r[0]))
        h = max(4.0, float(r[3] - r[1]))
        if w / h > ar:
            h = w / ar
        else:
            w = h * ar
        x0 = self._ax if bx >= self._ax else self._ax - w
        y0 = self._ay if by >= self._ay else self._ay - h
        x1, y1 = x0 + w, y0 + h
        if x0 < 0:
            x1 -= x0
            x0 = 0.0
        if y0 < 0:
            y1 -= y0
            y0 = 0.0
        if x1 > self.dw:
            x0 -= (x1 - self.dw)
            x1 = float(self.dw)
        if y1 > self.dh:
            y0 -= (y1 - self.dh)
            y1 = float(self.dh)
        return (max(0.0, x0), max(0.0, y0), x1, y1)

    def _sel_all(self):
        self.sel = (0, 0, self.dw, self.dh)
        self._draw()

    def _sel_aspect(self):
        if not self.aspect:
            self._sel_all()
            return
        ar = self.aspect[0] / self.aspect[1]
        w = float(self.dw)
        h = w / ar
        if h > self.dh:
            h = float(self.dh)
            w = h * ar
        x0 = (self.dw - w) / 2.0
        y0 = (self.dh - h) / 2.0
        self.sel = (x0, y0, x0 + w, y0 + h)
        self._draw()

    def _sel_content(self):
        bb = B.content_bbox(self.src, TRIM_THRESHOLD)
        if bb is None:
            self._sel_all()
            return
        s = self.scale
        self.sel = (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s)
        if self.lock.get() and self.aspect:
            r = self.sel
            self._ax, self._ay = r[0], r[1]
            self.sel = self._fit_aspect(r, r[2], r[3])
        self._draw()

    def _draw(self):
        self.canvas.delete("sel")
        x0, y0, x1, y1 = self.sel
        dim = dict(fill="#000000", stipple="gray50", outline="", tags="sel")
        self.canvas.create_rectangle(0, 0, self.dw, y0, **dim)
        self.canvas.create_rectangle(0, y1, self.dw, self.dh, **dim)
        self.canvas.create_rectangle(0, y0, x0, y1, **dim)
        self.canvas.create_rectangle(x1, y0, self.dw, y1, **dim)
        self.canvas.create_rectangle(x0, y0, x1, y1,
                                     outline="#00c2ff", width=2, tags="sel")
        sw = max(1, round((x1 - x0) / self.scale))
        sh = max(1, round((y1 - y0) / self.scale))
        self.size_lab.configure(
            text=f"裁剪区域：{sw} × {sh} 像素（原图 {self.src.width} × "
                 f"{self.src.height}）→ 会等比缩放进 293×400 的画布")

    # ---------------------------------------------------------- 结果
    def _source_rect(self):
        x0, y0, x1, y1 = self.sel
        s = self.scale
        l = int(round(x0 / s))
        t = int(round(y0 / s))
        r = int(round(x1 / s))
        b = int(round(y1 / s))
        l = min(max(l, 0), self.src.width - 1)
        t = min(max(t, 0), self.src.height - 1)
        r = min(max(r, l + 1), self.src.width)
        b = min(max(b, t + 1), self.src.height)
        return (l, t, r, b)

    def _ok(self):
        try:
            self.result = self.src.crop(self._source_rect())
        except Exception:
            self.result = None
        self._close()

    def _cancel(self):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# --------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.tool_dir = tool_dir()
        self.backup_dir = self.tool_dir / "backup"

        # 状态
        self.src_path: Path | None = None
        self.src_data: bytes | None = None
        self.slots: list = []
        self.slot: B.LogoSlot | None = None
        self.orig_image: Image.Image | None = None
        self.new_path: Path | None = None
        self.new_image_raw: Image.Image | None = None   # 上传的原图（用于"还原"）
        self.new_image: Image.Image | None = None       # 当前工作图（可能已裁剪）
        self.fitted_image: Image.Image | None = None    # 自动缩小后真正写入固件的那张
        self._probe_job = None
        self._probe_gen = 0
        self._probe_running = False
        self.last_output: Path | None = None

        self._orig_photo = None
        self._new_photo = None
        self._busy = False
        self._ui_queue: queue.Queue = queue.Queue()
        self._preview_job = None

        self._build_ui()
        self._refresh_buttons()
        self.log(f"{APP_NAME} 已启动")
        self.log(f"工具目录：{self.tool_dir}")
        self._pump()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("1145x925")
        self.root.minsize(1020, 820)

        try:
            ico = self.tool_dir / "app.ico"
            if ico.exists():
                self.root.iconbitmap(default=str(ico))
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        base_font = ("Microsoft YaHei UI", 9)
        self.root.option_add("*Font", base_font)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Sub.TLabel", foreground="#5a6673")
        style.configure("Big.TButton", padding=(10, 7))
        style.configure("Hint.TLabel", foreground="#7a8794", font=("Microsoft YaHei UI", 8))

        # ---- 标题 ----
        head = ttk.Frame(self.root, padding=(14, 12, 14, 4))
        head.pack(fill="x")
        ttk.Label(head, text="BIOS 开机 Logo 修改工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            head,
            text=f"{APP_NAME} · 只替换固件里的开机 Logo 图片，段头 / FFS / FV 结构与"
                 f"其余字节全部保持原样，文件等长。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Separator(self.root).pack(fill="x", pady=(8, 0))

        # ---- 按钮区 ----
        bar = ttk.Frame(self.root, padding=(14, 10, 14, 2))
        bar.pack(fill="x")
        self.btn_load = ttk.Button(bar, text="① 载入 BIOS 文件", style="Big.TButton",
                                   command=self.on_load)
        self.btn_upload = ttk.Button(bar, text="② 上传新 Logo", style="Big.TButton",
                                     command=self.on_upload)
        self.btn_replace = ttk.Button(bar, text="③ Logo 替换", style="Big.TButton",
                                      command=self.on_replace)
        for b in (self.btn_load, self.btn_upload, self.btn_replace):
            b.pack(side="left", padx=(0, 8))

        self.btn_open_dir = ttk.Button(bar, text="打开工具目录", command=self.on_open_dir)
        self.btn_open_dir.pack(side="right")

        # ---- 槽位选择 ----
        bar_slot = ttk.Frame(self.root, padding=(14, 2, 14, 4))
        bar_slot.pack(fill="x")
        ttk.Label(bar_slot, text="Logo 槽位：").pack(side="left")
        self.slot_var = tk.StringVar(value="—")
        self.slot_box = ttk.Combobox(bar_slot, textvariable=self.slot_var, width=24,
                                     state="readonly", values=["—"])
        self.slot_box.pack(side="left")
        self.slot_box.bind("<<ComboboxSelected>>", self.on_slot_change)
        ttk.Label(bar_slot,
                  text="（一个固件里可能有多份 Logo，可分别查看与替换）",
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))

        # ---- 图片适配控制 ----
        bar_fit = ttk.Frame(self.root, padding=(14, 2, 14, 6))
        bar_fit.pack(fill="x")
        self.auto_trim = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar_fit, text="自动去黑边", variable=self.auto_trim,
                        command=self._update_result_preview).pack(side="left")
        ttk.Label(bar_fit, text="   适配方式：").pack(side="left")
        self.fit_label = tk.StringVar(value=B.FIT_LABELS[B.FIT_CONTAIN])
        self.fit_box = ttk.Combobox(
            bar_fit, textvariable=self.fit_label, state="readonly", width=10,
            values=[B.FIT_LABELS[m] for m in B.FIT_MODES])
        self.fit_box.pack(side="left")
        self.fit_box.bind("<<ComboboxSelected>>",
                          lambda _e: self._on_fit_change())

        ttk.Label(bar_fit, text="   缩放：").pack(side="left")
        self.zoom_var = tk.DoubleVar(value=1.0)
        ttk.Scale(bar_fit, from_=ZOOM_MIN, to=ZOOM_MAX, variable=self.zoom_var,
                  command=self._on_zoom, length=120).pack(side="left")
        self.zoom_lab = ttk.Label(bar_fit, text="1.00×", width=7)
        self.zoom_lab.pack(side="left")

        self.btn_crop = ttk.Button(bar_fit, text="裁剪图片…", command=self.on_crop)
        self.btn_crop.pack(side="left", padx=(8, 4))
        self.btn_fitreset = ttk.Button(bar_fit, text="重置", command=self.on_reset_fit)
        self.btn_fitreset.pack(side="left")

        # ---- 输出尺寸（可任意，不要求与原 Logo 一致）----
        bar_size = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        bar_size.pack(fill="x")
        ttk.Label(bar_size, text="输出尺寸：").pack(side="left")
        self.size_label = tk.StringVar(value=SIZE_LABELS[SIZE_ORIG])
        self.size_box = ttk.Combobox(
            bar_size, textvariable=self.size_label, state="readonly", width=18,
            values=[SIZE_LABELS[m] for m in SIZE_MODES])
        self.size_box.pack(side="left")
        self.size_box.bind("<<ComboboxSelected>>",
                           lambda _e: self._on_size_change())

        self.size_w = tk.StringVar(value="")
        self.size_h = tk.StringVar(value="")
        self.sp_w = ttk.Spinbox(bar_size, from_=8, to=8192, width=5,
                                textvariable=self.size_w, command=self._on_size_change)
        self.sp_w.pack(side="left", padx=(6, 0))
        ttk.Label(bar_size, text="×").pack(side="left")
        self.sp_h = ttk.Spinbox(bar_size, from_=8, to=8192, width=5,
                                textvariable=self.size_h, command=self._on_size_change)
        self.sp_h.pack(side="left")
        self.sp_w.bind("<Return>", lambda _e: self._on_size_change())
        self.sp_h.bind("<Return>", lambda _e: self._on_size_change())
        self.sp_w.bind("<FocusOut>", lambda _e: self._on_size_change())
        self.sp_h.bind("<FocusOut>", lambda _e: self._on_size_change())

        self.size_now = ttk.Label(bar_size, text="", style="Hint.TLabel")
        self.size_now.pack(side="left", padx=(10, 0))
        self._sync_size_widgets()

        # ---- 颜色位数 / 自动缩小（决定「一张图能放多大」）----
        bar_bpp = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        bar_bpp.pack(fill="x")
        ttk.Label(bar_bpp, text="颜色位数：").pack(side="left")
        self.bpp_label = tk.StringVar(value=BPP_LABELS[BPP_24])
        self.bpp_box = ttk.Combobox(
            bar_bpp, textvariable=self.bpp_label, state="readonly", width=24,
            values=[BPP_LABELS[m] for m in BPP_MODES])
        self.bpp_box.pack(side="left")
        self.bpp_box.bind("<<ComboboxSelected>>",
                          lambda _e: self._on_bpp_change())

        self.auto_fit = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar_bpp, text="放不下时自动缩小",
                        variable=self.auto_fit,
                        command=self._on_bpp_change).pack(side="left", padx=(14, 0))
        self.bpp_now = ttk.Label(bar_bpp, text="", style="Hint.TLabel")
        self.bpp_now.pack(side="left", padx=(10, 0))
        self._update_bpp_now()

        # ---- 适配方式说明 ----
        bar_tip = ttk.Frame(self.root, padding=(14, 0, 14, 4))
        bar_tip.pack(fill="x")
        self.fit_tip = ttk.Label(bar_tip, text="", style="Hint.TLabel")
        self.fit_tip.pack(side="left")
        self._update_fit_tip()

        # ---- 信息区 ----
        info_box = ttk.LabelFrame(self.root, text=" BIOS / Logo 信息 ", padding=8)
        info_box.pack(fill="x", padx=14, pady=(4, 6))
        self.info = tk.Text(info_box, height=9, wrap="none", relief="flat",
                            background="#f7f9fb", foreground="#1f2d3d",
                            font=("Consolas", 9))
        info_sb = ttk.Scrollbar(info_box, orient="vertical", command=self.info.yview)
        self.info.configure(yscrollcommand=info_sb.set)
        self.info.pack(side="left", fill="both", expand=True)
        info_sb.pack(side="right", fill="y")
        self._set_info("尚未载入 BIOS 文件。\n\n请先点击「① 载入 BIOS 文件」。")

        # ---- 预览区 ----
        pv = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        pv.pack(fill="both", expand=True)
        pv.columnconfigure(0, weight=1, uniform="p")
        pv.columnconfigure(1, weight=1, uniform="p")

        self.pv_orig = self._make_preview(pv, 0, "原 Logo（解析自 BIOS）",
                                          "点击图片 → 备份到工具目录")
        self.pv_new = self._make_preview(pv, 1, "替换结果（实际写入固件的画面）",
                                         "点击图片 → 备份到工具目录")

        # ---- 日志区 ----
        log_box = ttk.LabelFrame(self.root, text=" 运行日志 ", padding=6)
        log_box.pack(fill="both", padx=14, pady=(0, 6))
        self.log_text = tk.Text(log_box, height=7, wrap="word", relief="flat",
                                background="#1f2d3d", foreground="#d7e0ea",
                                insertbackground="#d7e0ea", font=("Consolas", 9))
        log_sb = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        self.status = tk.StringVar(value="就绪")
        bar2 = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        bar2.pack(fill="x")
        ttk.Label(bar2, textvariable=self.status, style="Sub.TLabel").pack(side="left")
        self.prog = ttk.Progressbar(bar2, mode="indeterminate", length=180)
        self.prog.pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _make_preview(self, parent, col, title, hint):
        box = ttk.LabelFrame(parent, text=f" {title} ", padding=8)
        box.grid(row=0, column=col, sticky="nsew", padx=(0, 8) if col == 0 else (8, 0))
        holder = tk.Label(box, text="（无）", background="#11161c", foreground="#5c6b7a",
                          width=PREVIEW_BOX[0], height=0,
                          font=("Microsoft YaHei UI", 10))
        holder.pack(fill="both", expand=True)
        ttk.Label(box, text=hint, style="Hint.TLabel").pack(pady=(6, 0))
        holder.bind("<Button-1>", lambda _e, c=col: self.on_preview_click(c))
        return holder

    # ------------------------------------------------------------ helpers
    def _set_info(self, text: str):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", text)
        self.info.configure(state="disabled")

    def log(self, msg: str):
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_text.see("end")

    def set_status(self, s: str):
        self.status.set(s)

    def _refresh_buttons(self):
        busy = self._busy
        has_bios = self.src_data is not None
        has_slot = self.slot is not None
        has_new = self.new_image is not None

        def cfg(btn, cond):
            btn.configure(state=("disabled" if (busy or not cond) else "normal"))

        cfg(self.btn_load, True)
        cfg(self.btn_upload, has_bios)
        cfg(self.btn_replace, has_slot and has_new)
        cfg(self.btn_crop, has_new)
        cfg(self.btn_fitreset, has_slot and has_new)

    def set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        if busy:
            self.prog.start(12)
            self.set_status(text or "处理中…")
        else:
            self.prog.stop()
        self._refresh_buttons()

    def _pump(self):
        """从工作线程回传结果到 UI 线程。"""
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    self.log("UI 回调异常：\n" + traceback.format_exc())
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def run_async(self, work, done, busy_text: str):
        if self._busy:
            return
        self.set_busy(True, busy_text)

        def runner():
            try:
                res = work()
            except Exception as exc:
                err = exc
                tb = traceback.format_exc()
                self._ui_queue.put((self._on_async_fail, (done, err, tb)))
            else:
                self._ui_queue.put((self._on_async_ok, (done, res)))

        threading.Thread(target=runner, daemon=True).start()

    def _on_async_ok(self, done, res):
        self.set_busy(False)
        self.set_status("就绪")
        done(res)

    def _on_async_fail(self, done, err, tb):
        self.set_busy(False)
        self.set_status("出错")
        self.log("发生错误：\n" + tb)
        if isinstance(err, B.BiosLogoError):
            messagebox.showerror(APP_NAME, str(err), parent=self.root)
        else:
            messagebox.showerror(APP_NAME, f"发生未预期错误：\n{err}", parent=self.root)
        # 让调用方也能收尾
        try:
            done(None)
        except Exception:
            pass

    # ------------------------------------------------------------- ① 载入
    def on_load(self):
        p = filedialog.askopenfilename(parent=self.root, title="选择 BIOS 文件",
                                       filetypes=BIOS_TYPES)
        if not p:
            return
        path = Path(p)
        try:
            size = path.stat().st_size
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法读取文件：{exc}", parent=self.root)
            return

        self.src_path = path
        self.src_data = None
        self.slots = []
        self.slot = None
        self.orig_image = None
        self._orig_photo = None
        self.pv_orig.configure(image="", text="（无）")
        self.slot_box.configure(values=["—"])
        self.slot_var.set("—")
        self.last_output = None
        self._refresh_buttons()

        self.log(f"载入 BIOS：{path}")
        self.log(f"文件大小：{size:,} 字节 ({human(size)})")
        self._set_info(
            f"BIOS 文件 : {path}\n"
            f"文件大小 : {size:,} 字节 ({human(size)})\n\n"
            f"请点击「② Logo 解析」从固件中解出开机 Logo。")

        self.run_async(lambda: B.parse_bios(str(path)),
                       self._after_parse, "正在解析固件…")

    def _after_parse(self, res):
        if res is None:
            self.src_data = None
            self._refresh_buttons()
            return
        data, slots = res
        self.src_data = data
        self.slots = slots
        if not slots:
            self.slot = None
            self.log("未在固件中找到可替换的开机 Logo（GUID 7BB28B99-…）。")
            self._set_info(
                f"BIOS 文件 : {self.src_path}\n"
                f"文件大小 : {len(data):,} 字节\n\n"
                f"⚠ 未找到可替换的开机 Logo。\n\n"
                f"本工具目前支持的开机图类型：\n"
                f"  • 标准 UEFI BMP Logo，GUID 7BB28B99-61BB-11D5-9A5D-0090273FC14D\n"
                f"  • 存放于 GUID_DEFINED(LZMA) 压缩段内\n"
                f"  • 或直接以未压缩 Freeform 段存放")
            messagebox.showwarning(
                APP_NAME,
                "未在该固件中找到可替换的开机 Logo。\n\n"
                "可能该机型使用了其它存放方式（例如整卷压缩的内部 Logo）。",
                parent=self.root)
            self._refresh_buttons()
            return

        self.slot_box.configure(values=[f"#{s.index}  {s.bmp_info.width}x{s.bmp_info.rows}"
                                        for s in slots])
        self.slot_var.set(f"#{slots[0].index}  {slots[0].bmp_info.width}x{slots[0].bmp_info.rows}")
        self.log(f"解析成功：找到 {len(slots)} 处 Logo。")
        self._select_slot(slots[0])

    # ------------------------------------------------------------- 槽位
    def on_slot_change(self, _evt=None):
        idx = self.slot_box.current()
        if 0 <= idx < len(self.slots):
            self._select_slot(self.slots[idx])

    def _select_slot(self, slot: B.LogoSlot):
        self.slot = slot
        try:
            img = slot.to_image()
        except Exception as exc:
            self.log(f"渲染原 Logo 失败：{exc}")
            img = None
        self.orig_image = img
        self._show_preview(self.pv_orig, img, "orig")

        text = (
            f"BIOS 文件  : {self.src_path}\n"
            f"文件大小   : {len(self.src_data):,} 字节 ({human(len(self.src_data))})\n"
            f"Logo 数量  : {len(self.slots)}\n"
            f"\n"
            f"──── 槽位 #{slot.index}：{slot.name} ────\n"
            f"{slot.path_text()}\n"
            f"\n"
            f"解出的 Logo 可点击左侧图片备份到工具目录。\n"
            f"备份目录：{self.backup_dir}"
        )
        if img is not None:
            px = img.load()
            corners = [px[0, 0], px[img.width - 1, 0], px[0, img.height - 1],
                       px[img.width - 1, img.height - 1]]
            text += f"\n图像四角像素：{corners}（替换时以纯黑为底色居中缩放）"
        self._set_info(text)
        self.log(f"槽位 #{slot.index}：{slot.bmp_info.describe()}")
        self._refresh_buttons()
        self._update_result_preview()

    # -------------------------------------------------------- 输出尺寸
    def _size_mode(self) -> str:
        lab = self.size_label.get()
        for m, l in SIZE_LABELS.items():
            if l == lab:
                return m
        return SIZE_SOURCE

    def _default_size(self) -> tuple:
        """兜底输出尺寸：优先用上传图片自己的尺寸，没有图时给 720×480。"""
        src = self.new_image
        if src is not None and src.width > 0 and src.height > 0:
            return src.width, src.height
        return SIZE_FIXED_WH

    def _source_for_size(self):
        """算「原图」尺寸时要用的那张图（勾了去黑边就是去完黑边的）。"""
        src = self.new_image
        if src is not None and self.auto_trim.get():
            try:
                src = B.trim_black_border(src, TRIM_THRESHOLD)
            except Exception:
                pass
        return src

    def _out_size(self):
        """一定要返回一个 (w, h)；「原图」= 上传图片自己的像素尺寸。"""
        mode = self._size_mode()
        if mode == SIZE_ORIG:
            if self.slot is None:
                return SIZE_FIXED_WH
            info = self.slot.bmp_info
            return info.width, info.rows
        if mode == SIZE_SOURCE:
            src = self._source_for_size()
            if src is None:
                return SIZE_FIXED_WH
            w, h = src.width, src.height
            if not (SIZE_MIN <= w <= SIZE_MAX and SIZE_MIN <= h <= SIZE_MAX):
                self.log(f"⚠ 原图尺寸 {w}×{h} 超出 {SIZE_MIN}..{SIZE_MAX}，"
                         f"按 {SIZE_FIXED_WH[0]}×{SIZE_FIXED_WH[1]} 处理。")
                return SIZE_FIXED_WH
            return w, h
        if mode == SIZE_FIXED:
            return SIZE_FIXED_WH
        try:
            w = int(float(self.size_w.get()))
            h = int(float(self.size_h.get()))
        except (TypeError, ValueError):
            self.log(f"⚠ 自定义尺寸填的有问题，先按 {SIZE_FIXED_WH[0]}×"
                     f"{SIZE_FIXED_WH[1]} 处理。")
            return SIZE_FIXED_WH
        if not (SIZE_MIN <= w <= SIZE_MAX and SIZE_MIN <= h <= SIZE_MAX):
            self.log(f"⚠ 自定义尺寸 {w}×{h} 超出 {SIZE_MIN}..{SIZE_MAX}，"
                     f"先按 {SIZE_FIXED_WH[0]}×{SIZE_FIXED_WH[1]} 处理。")
            return SIZE_FIXED_WH
        return w, h

    def _out_size_arg(self):
        """传给核心库的 out_size 参数。

        「跟原 Logo 一样」时返回 ``None`` —— 核心库会走「复用原 BMP 头」那条
        路径，产出的明文与原来逐字节等长，是最不可能出问题的一档。
        """
        if self._size_mode() == SIZE_ORIG:
            return None
        return self._out_size()

    def _sync_size_widgets(self):
        """按当前模式启用/禁用 W×H 输入框，并刷新最终尺寸提示。"""
        mode = self._size_mode()
        state = "normal" if mode == SIZE_CUSTOM else "disabled"
        for sp in (self.sp_w, self.sp_h):
            try:
                sp.configure(state=state)
            except Exception:
                pass
        if mode == SIZE_CUSTOM:
            if not self.size_w.get().strip():
                self.size_w.set(str(SIZE_FIXED_WH[0]))
            if not self.size_h.get().strip():
                self.size_h.set(str(SIZE_FIXED_WH[1]))
        else:
            # 非自定义模式：把框里的数字显示成「实际会输出」的尺寸（只读）
            try:
                w, h = self._out_size()
            except Exception:
                w, h = SIZE_FIXED_WH
            self.size_w.set(str(w))
            self.size_h.set(str(h))
        self._update_size_now()

    def _update_size_now(self):
        try:
            sz = self._out_size()
        except Exception:
            sz = SIZE_FIXED_WH
        mode = self._size_mode()
        if mode == SIZE_ORIG:
            txt = f"→ 输出 {sz[0]}×{sz[1]}（与原 Logo 完全一致，最保险）"
        elif mode == SIZE_SOURCE:
            if self.new_image_raw is None:
                txt = "→ 输出「原图」尺寸（上传图片后确定）"
                for sp in (self.sp_w, self.sp_h):
                    try:
                        sp.configure(state="disabled")
                    except Exception:
                        pass
                self.size_w.set("—")
                self.size_h.set("—")
                self.size_now.configure(text=txt)
                return
            src = self._source_for_size()
            note = "原图" + ("·去黑边后" if (self.auto_trim.get() and src is not None) else "")
            if src is not None:
                note += f"，来自 {self.new_image.width}×{self.new_image.height}"
            txt = f"→ 输出 {sz[0]}×{sz[1]}（{note}）"
        elif mode == SIZE_FIXED:
            txt = f"→ 输出 {sz[0]}×{sz[1]}（固定）"
        else:
            txt = f"→ 输出 {sz[0]}×{sz[1]}（比例 {sz[0] / sz[1]:.2f}）"
        self.size_now.configure(text=txt)

    def _set_size_mode(self, mode: str):
        self.size_label.set(SIZE_LABELS[mode])
        if mode == SIZE_CUSTOM:
            self.size_w.set(str(SIZE_FIXED_WH[0]))
            self.size_h.set(str(SIZE_FIXED_WH[1]))
        self._sync_size_widgets()
        self._update_result_preview()

    def _on_size_change(self):
        self._sync_size_widgets()
        self._update_result_preview()

    # -------------------------------------------------------- 图片适配
    def _fit_mode(self) -> str:
        lab = self.fit_label.get()
        for m, l in B.FIT_LABELS.items():
            if l == lab:
                return m
        return B.FIT_CONTAIN

    def _update_fit_tip(self):
        mode = self._fit_mode()
        self.fit_tip.configure(text=f"● {B.FIT_LABELS[mode]}：{B.FIT_TIPS[mode]}")

    def _on_fit_change(self):
        self._update_fit_tip()
        self._sync_size_widgets()
        self._update_result_preview()

    # ------------------------------------------------------ 颜色位数 / 自动缩小
    def _out_bpp(self) -> int:
        lab = self.bpp_label.get()
        for m, l in BPP_LABELS.items():
            if l == lab:
                return m
        return BPP_24

    def _update_bpp_now(self):
        bpp = self._out_bpp()
        fit = "自动缩小：开" if self.auto_fit.get() else "自动缩小：关"
        self.bpp_now.configure(text=f"→ {bpp} 位 · {fit}")

    def _on_bpp_change(self):
        self._update_bpp_now()
        bpp = self._out_bpp()
        self.log(f"颜色位数：{bpp} 位　{BPP_TIPS.get(bpp, '')}")
        self.log(f"放不下时自动缩小：{'开（任意大小的图片都能用）' if self.auto_fit.get() else '关（放不下会直接报错）'}")
        self._update_result_preview()

    def _render_result(self):
        """按当前设置算出「真正会写进固件」的那张图。"""
        if self.slot is None or self.new_image is None:
            return None
        w, h = self._out_size()
        src = self.new_image
        if self.auto_trim.get():
            src = B.trim_black_border(src, TRIM_THRESHOLD)
        return B.fit_image(src, w, h, mode=self._fit_mode(),
                           zoom=self.zoom_var.get())

    def _update_result_preview(self):
        self.fitted_image = None
        if self.slot is None or self.new_image is None:
            self._show_preview(self.pv_new, None, "new")
            self._update_size_now()
            return
        try:
            out = self._render_result()
        except Exception as exc:
            self.log(f"生成预览失败：{exc}")
            self._update_size_now()
            return
        self._show_preview(self.pv_new, out, "new")

        trimmed = self.auto_trim.get()
        cov = B.coverage_ratio(out, threshold=TRIM_THRESHOLD)
        mode_cn = B.FIT_LABELS.get(self._fit_mode(), self._fit_mode())
        self._update_size_now()
        self.log(f"适配预览：{mode_cn}"
                 f"{' + 自动去黑边' if trimmed else ''}"
                 f" + {self.zoom_var.get():.2f}×  →  内容填充率 {cov * 100:.1f}%"
                 f"（画布 {out.width}x{out.height}）")
        if cov < 0.35:
            self.log("⚠ 内容只占画面一小块，开机时 Logo 会显得很小 —— "
                     "建议勾选「自动去黑边」，或调大「缩放」，或点「裁剪图片…」手动框选。")
        if self._fitted_exceeds_canvas():
            self.log("⚠ 画面已被放大到超出画布并被裁切，边缘可能缺失 —— "
                     "可调小「缩放」或改用「完整显示」。")
        ow = self.slot.bmp_info.width
        oh = self.slot.bmp_info.rows
        if (out.width, out.height) != (ow, oh):
            self.log(f"⚠ 输出尺寸 {out.width}×{out.height} 与固件原 Logo 的 {ow}×{oh} 不同："
                     f"Logo 在开机画面上的实际大小与位置由固件决定，"
                     f"刷入后若显示异常请改回「原图」或换 720×480。")
        self._schedule_fit_probe(out)

    def _schedule_fit_probe(self, out):
        """尺寸可能有变化时，稍后试算一次「能不能压进 Logo 段」。"""
        if self._probe_job is not None:
            try:
                self.root.after_cancel(self._probe_job)
            except Exception:
                pass
        self._probe_job = self.root.after(700, lambda: self._run_fit_probe(out))

    def _run_fit_probe(self, out):
        self._probe_job = None
        if self.slot is None or self.new_image is None:
            return
        if self._probe_running:
            # 上一次预检还在算，等它结束再补一次（避免拖着拖出十几个线程）
            self._probe_job = self.root.after(700, lambda: self._run_fit_probe(out))
            return
        slot = self.slot
        bpp = self._out_bpp()
        auto_fit = self.auto_fit.get()
        raw = out.width * out.height * 3
        if raw > PROBE_RAW_LIMIT and not auto_fit:
            self.log(f"⚠ 输出 {out.width}×{out.height} 的未压缩像素有 "
                     f"{raw / 1e6:.1f} MB，而 Logo 段只有 "
                     f"{slot.stream_budget:,} 字节可用 —— 通常放不下，已跳过容量预检；"
                     f"点「③ Logo 替换」时会正式校验。"
                     f"建议改用「720×480」，或勾上「放不下时自动缩小」，"
                     f"或把「颜色位数」降到 4 位。")
            return

        img = self.new_image
        mode = self._fit_mode()
        zoom = self.zoom_var.get()
        trim = self.auto_trim.get()
        size = self._out_size_arg()
        self._probe_gen += 1
        gen = self._probe_gen
        self._probe_running = True

        def work():
            try:
                if auto_fit:
                    bmp, dims, scale, n, plain_len = B.fit_to_budget(
                        slot, img, mode=mode, zoom=zoom, auto_trim=trim,
                        out_size=size, out_bpp=bpp)
                    shown = B.bmp_to_image(B.parse_bmp(bmp))
                    return "ok", (n, plain_len, dims, scale, bpp, shown)
                bmp = B.render_bmp(slot, img, mode=mode, zoom=zoom,
                                   auto_trim=trim, out_size=size, out_bpp=bpp)
                n, plain_len = B.probe_fit(slot, bmp)
            except B.BiosLogoError as exc:
                return "bad", str(exc)
            except Exception as exc:                      # pragma: no cover
                return "err", str(exc)
            return "ok", (n, plain_len, None, 1.0, bpp, None)

        def done(res):
            self._probe_running = False
            if gen != self._probe_gen:                    # 已经有更新的预检了
                return
            kind, payload = res
            if kind == "ok":
                n, plain_len, dims, scale, used_bpp, shown = payload
                extra = ""
                if dims is not None and scale < 0.999:
                    extra = (f"（已自动缩小到 {dims[0]}×{dims[1]}，"
                             f"是你要求尺寸的 {scale * 100:.0f}%）")
                    self.log(f"自动缩小：你要求的尺寸放不下，已收窄到 "
                             f"{dims[0]}×{dims[1]} —— 这是 {used_bpp} 位下能放下的最大尺寸。")
                self.log(f"容量预检：明文 {plain_len:,} 字节 → 压缩 {n:,} / "
                         f"{slot.stream_budget:,} 字节，放得下 ✓ {used_bpp} 位{extra}")
                if shown is not None:
                    # 自动缩小后的画面才是真正会写进固件的那张图
                    self.fitted_image = shown
                    self._show_preview(self.pv_new, shown, "new")
            elif kind == "bad":
                self.log(f"⚠ 容量预检不通过：{payload}")
            else:
                self.log(f"容量预检出错：{payload}")

        def runner():
            res = work()
            self._ui_queue.put((done, (res,)))

        threading.Thread(target=runner, daemon=True).start()

    def _fitted_exceeds_canvas(self) -> bool:
        """等比缩放后的图是否比画布大（会被裁掉一部分）。"""
        if self.slot is None or self.new_image is None:
            return False
        w, h = self._out_size()
        src = self.new_image
        if self.auto_trim.get():
            src = B.trim_black_border(src, TRIM_THRESHOLD)
        if src.width <= 0 or src.height <= 0:
            return False
        zoom = self.zoom_var.get()
        if self._fit_mode() == B.FIT_STRETCH:
            return zoom > 1.0 + 1e-6
        sx = w / src.width
        sy = h / src.height
        r = max(sx, sy) if self._fit_mode() == B.FIT_COVER else min(sx, sy)
        return (src.width * r * zoom > w + 0.5 or
                src.height * r * zoom > h + 0.5)

    def _on_zoom(self, _v=None):
        self.zoom_lab.configure(text=f"{self.zoom_var.get():.2f}×")
        if self._preview_job is not None:
            try:
                self.root.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.root.after(140, self._update_result_preview)

    def on_reset_fit(self):
        self.zoom_var.set(1.0)
        self.zoom_lab.configure(text="1.00×")
        self.auto_trim.set(False)
        self.fit_label.set(B.FIT_LABELS[B.FIT_CONTAIN])
        self.size_label.set(SIZE_LABELS[SIZE_ORIG])
        self.size_w.set("")
        self.size_h.set("")
        self._update_fit_tip()
        self._sync_size_widgets()
        self._update_result_preview()

    def on_crop(self):
        if self.new_image is None:
            messagebox.showinfo(APP_NAME, "请先上传新的 Logo 图片。", parent=self.root)
            return
        aspect = self._out_size() if self.slot is not None else None
        dlg = CropDialog(self.root, self.new_image, aspect=aspect)
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        self.new_image = dlg.result
        self.log(f"已裁剪图片：{dlg.result.width}x{dlg.result.height}"
                 f"（原始上传图 {self.new_image_raw.width}x{self.new_image_raw.height}）")
        self._update_result_preview()

    # ------------------------------------------------------------ ③ 上传
    def on_upload(self):
        p = filedialog.askopenfilename(parent=self.root, title="选择新的 Logo 图片",
                                       filetypes=IMAGE_TYPES)
        if not p:
            return
        path = Path(p)
        try:
            size = path.stat().st_size
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法读取文件：{exc}", parent=self.root)
            return

        if size > MAX_LOGO_BYTES:
            messagebox.showerror(
                APP_NAME,
                f"图片异常巨大：{size:,} 字节 ({human(size)})\n\n"
                f"超过保护上限 {human(MAX_LOGO_BYTES)}，不予载入。\n"
                f"（Logo 生成的文件大小与输入图片大小无关，正常图片不会被拒。）",
                parent=self.root)
            self.log(f"已拒绝 {path.name}：{size:,} 字节，超过保护上限 "
                     f"{human(MAX_LOGO_BYTES)}")
            return

        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:
            messagebox.showerror(
                APP_NAME,
                f"无法识别为图片文件：\n{exc}\n\n只支持图片格式（PNG / JPG / BMP / GIF / WebP / TIFF）。",
                parent=self.root)
            return

        self.new_path = path
        self.new_image_raw = img
        self.new_image = img
        self.log(f"载入新 Logo：{path.name}  {img.width}x{img.height} {img.mode}  "
                 f"{size:,} 字节 ({human(size)})")

        # 每次上传都回到最稳的默认：不去黑边 + 完整显示（不裁切）+ 原图尺寸
        self.auto_trim.set(False)
        self.fit_label.set(B.FIT_LABELS[B.FIT_CONTAIN])
        self.zoom_var.set(1.0)
        self.zoom_lab.configure(text="1.00×")
        self._update_fit_tip()
        self._sync_size_widgets()
        self._update_result_preview()
        self._refresh_buttons()

    # ------------------------------------------------------------ ④ 替换
    def on_replace(self):
        if self.slot is None:
            messagebox.showinfo(APP_NAME, "请先解析出 BIOS 中的 Logo。", parent=self.root)
            return
        if self.new_image is None:
            messagebox.showinfo(APP_NAME, "请先上传新的 Logo 图片。", parent=self.root)
            return

        stem = self.src_path.stem
        suffix = self.src_path.suffix
        default = str(self.src_path.with_name(f"{stem}_newlogo{suffix}"))
        out = filedialog.asksaveasfilename(
            parent=self.root, title="保存新的 BIOS 文件",
            initialdir=str(self.src_path.parent), initialfile=Path(default).name,
            defaultextension=suffix, filetypes=[("BIOS 固件", f"*{suffix}"),
                                                ("所有文件", "*.*")])
        if not out:
            return

        slot = self.slot
        data = self.src_data
        img = self.new_image
        out_path = Path(out)
        mode = self._fit_mode()
        zoom = self.zoom_var.get()
        auto_trim = self.auto_trim.get()
        out_size = self._out_size_arg()
        out_bpp = self._out_bpp()
        auto_fit = self.auto_fit.get()
        if out_size is not None:
            self.log(f"输出尺寸不是原尺寸：{out_size[0]}×{out_size[1]}"
                     f"（原 {self.slot.bmp_info.width}×{self.slot.bmp_info.rows}）")
        else:
            self.log(f"输出尺寸：跟原 Logo 一样（"
                     f"{self.slot.bmp_info.width}×{self.slot.bmp_info.rows}，最保险）")
        self.log(f"颜色位数：{out_bpp} 位　放不下时自动缩小：{'开' if auto_fit else '关'}")

        def work():
            rep = B.replace(data, slot, img, str(out_path), mode=mode, zoom=zoom,
                            auto_trim=auto_trim, out_size=out_size,
                            out_bpp=out_bpp, auto_fit=auto_fit)
            return rep

        self.log("开始替换并重新打包…")
        self.run_async(work, lambda rep: self._after_replace(rep, out_path),
                       "正在替换并校验…")

    def _after_replace(self, rep, out_path: Path):
        if rep is None:
            self.log("替换失败，未写出文件。")
            return
        self.last_output = out_path
        self.log("替换完成：")
        for line in rep.text().splitlines():
            self.log("    " + line)
        self.log(f"已写出：{out_path}")

        self._set_info(
            f"✔ 替换完成\n\n"
            f"输入 BIOS  : {self.src_path}\n"
            f"输出 BIOS  : {out_path}\n"
            f"输出大小   : {rep.out_size:,} 字节 ({human(rep.out_size)})\n"
            f"Logo 尺寸  : {rep.out_dims[0]}x{rep.out_dims[1]}\n"
            f"\n"
            f"──── 安全校验 ────\n"
            f"{rep.text()}\n"
            f"\n"
            f"──── 说明 ────\n"
            f"• 文件长度与原文件完全一致，固件代码部分未做任何改动。\n"
            f"• 改动仅落在 Logo 所在的压缩段区间内。\n"
            f"• 刷写请使用主板自带的 Q-Flash / Q-Flash Plus，并保留原始 BIOS 以便回退。"
        )

        msg = (f"替换完成，已写出：\n{out_path}\n\n"
               f"改动 {rep.diff_count:,} 字节，全部位于 Logo 段内："
               f"{'是' if rep.only_inside_logo else '否'}\n"
               f"回读校验：{'通过' if rep.verify_ok else '失败'}\n"
               f"文件等长：{'是' if rep.src_size == rep.out_size else '否'}\n\n"
               f"是否打开输出目录？")
        if messagebox.askyesno(APP_NAME, msg, parent=self.root):
            self._open_dir(out_path.parent)

    # ------------------------------------------------------------ 预览/备份
    def _show_preview(self, label: tk.Label, img, which: str):
        if img is None:
            label.configure(image="", text="（无）")
            if which == "orig":
                self._orig_photo = None
            else:
                self._new_photo = None
            return
        thumb = img.copy()
        thumb.thumbnail(PREVIEW_BOX, Image.LANCZOS)
        # 统一铺在黑底上，方便观察
        canvas = Image.new("RGB", PREVIEW_BOX, (17, 22, 28))
        canvas.paste(thumb, ((PREVIEW_BOX[0] - thumb.width) // 2,
                             (PREVIEW_BOX[1] - thumb.height) // 2))
        photo = ImageTk.PhotoImage(canvas)
        label.configure(image=photo, text="", width=0, height=0)
        if which == "orig":
            self._orig_photo = photo
        else:
            self._new_photo = photo

    def on_preview_click(self, col: int):
        if col == 0:
            img, src_desc, tag = self.orig_image, "从 BIOS 解出的原 Logo", "原Logo"
        else:
            # 开了自动缩小时，备份真正写进固件的那张（可能已被缩小）
            img = self.fitted_image or self._render_result()
            src_desc, tag = "实际写入固件的替换结果", "新Logo"
        if img is None:
            messagebox.showinfo(APP_NAME, "还没有可备份的图片。", parent=self.root)
            return
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            name = f"{tag}_{stamp()}.png"
            dst = self.backup_dir / name
            img.save(dst, "PNG")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"备份失败：\n{exc}", parent=self.root)
            return
        self.log(f"已备份{src_desc} → {dst}")
        self.set_status(f"已备份：{name}")
        messagebox.showinfo(APP_NAME,
                            f"已备份{src_desc}到工具目录：\n\n{dst}",
                            parent=self.root)

    # ------------------------------------------------------------ 杂项
    def on_open_dir(self):
        self._open_dir(self.tool_dir)

    def _open_dir(self, p: Path):
        try:
            os.startfile(str(p))          # noqa: S606  (Windows 专用)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法打开目录：\n{exc}", parent=self.root)


# --------------------------------------------------------------------------
def main():
    # 高 DPI 下让界面清晰一些
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
