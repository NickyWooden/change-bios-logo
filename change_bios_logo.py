# -*- coding: utf-8 -*-
"""
change-bios-logo —— BIOS 开机 Logo 修改工具（Windows 桌面版）
=============================================================

界面流程：
  1. 【载入 BIOS 文件】 选择固件镜像
  2. 【Logo 解析】     从固件里解出开机 Logo，显示位置 / 规格 / 预览；
                       点击预览图即可把 Logo 备份保存到工具所在目录
  3. 【上传新 Logo】   选择图片（限图片格式，≤ 256 MB）
  4. 【Logo 替换】     等长替换并重新打包成新的 BIOS 文件

硬性保证：只重写 Logo 所在压缩段的字节，段头 / FFS 头 / FV 头 /
其余全部内容逐字节保留，文件总长不变，不触碰固件代码。

界面用 Qt（PySide6）重写，毛玻璃风格见 ``glass.py``。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

from PIL import Image

import bioslogo as B
import glass as G

from PySide6.QtCore import Qt, QEvent, QTimer, Signal, QRectF, QSize
from PySide6.QtGui import (QFont, QImage, QIcon, QPixmap,
                           QGuiApplication)
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QFrame,
                               QHBoxLayout, QVBoxLayout, QSizePolicy,
                               QPlainTextEdit)

APP_NAME = "change-bios-logo"
APP_TITLE = "change-bios-logo — BIOS 开机 Logo 修改工具"
PREVIEW_BOX = (220, 140)                  # 预览缩略图默认尺寸（空态占位图；实际按预览框实时尺寸渲染）

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
    "图片文件;*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff",
    "PNG;*.png", "JPEG;*.jpg *.jpeg", "BMP;*.bmp",
    "GIF;*.gif", "WebP;*.webp", "所有文件;*.*",
]
BIOS_TYPES = [
    "BIOS 固件;*.F44d *.f44d *.bin *.rom *.cap *.fd *.BIN *.ROM *.CAP",
    "所有文件;*.*",
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


def _pil_to_qpixmap(img) -> QPixmap | None:
    """PIL Image → QPixmap（转 RGB）。"""
    if img is None:
        return None
    img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3,
                  QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg)


def _label(text: str = "", color: str = G.INK,
           size: int = G.SIZE_BASE) -> QLabel:
    """玻璃上的文字标签（透明底，只设颜色）。"""
    lab = QLabel(text)
    lab.setStyleSheet(f"color: {color}; background: transparent;")
    lab.setFont(G.font_ui(size))
    return lab


# --------------------------------------------------------------------------
class PreviewLabel(QLabel):
    """预览图承载框：可点击（备份），尺寸变化时发信号触发重绘。"""

    clicked = Signal(int)
    resized = Signal(int, int, int)      # (col, w, h)

    def __init__(self, col: int, parent=None):
        super().__init__(parent)
        self.col = col
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.col)
        super().mousePressEvent(ev)

    def resizeEvent(self, ev):
        w, h = self.width(), self.height()
        if w >= 40 and h >= 40:
            self.resized.emit(self.col, w, h)
        super().resizeEvent(ev)


# --------------------------------------------------------------------------
class CropCanvas(QFrame):
    """裁剪画布：显示缩放后的图，拖拽框选，画选区。

    画布本身随窗口伸缩（Expanding），不再写死 dw×dh 的固定尺寸——
    否则窗口比图小时图会整块溢出窗口外（用户看到的"图片超出窗口"）。
    内部把 dw×dh 的图按「等比缩放 + 居中 + 黑边」铺到画布当前实际尺寸上，
    图片因此永远落在画布（= 窗口内容区）内。

    选区坐标始终记在 dw×dh 逻辑空间里，与画布实际像素无关；鼠标事件先
    从实际像素映射回逻辑空间再参与框选，所以窗口缩放后框选依旧准确。
    """

    selChanged = Signal()

    def __init__(self, pixmap: QPixmap, dw: int, dh: int, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.dw, self.dh = dw, dh
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            f"background: #000000; border: 1px solid {G.BORDER};")
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.sel = (0, 0, dw, dh)
        self._ax = self._ay = 0.0
        self._dragging = False
        # 「逻辑(dw×dh) → 实际像素」映射参数：等比缩放系数 + 居中偏移
        self._s = 1.0
        self._ox = self._oy = 0.0

    def sizeHint(self):
        # 首选尺寸 = 逻辑显示尺寸，让外层布局据此把窗口撑到放得下图
        return QSize(self.dw, self.dh)

    def _update_map(self):
        w, h = self.width(), self.height()
        if w < 2 or h < 2:
            self._s, self._ox, self._oy = 1.0, 0.0, 0.0
            return
        self._s = min(w / self.dw, h / self.dh)
        self._ox = (w - self.dw * self._s) / 2.0
        self._oy = (h - self.dh * self._s) / 2.0

    def resizeEvent(self, ev):
        self._update_map()
        self.update()
        super().resizeEvent(ev)

    def _to_logical(self, x: float, y: float):
        """实际像素坐标 → dw×dh 逻辑坐标。"""
        return (x - self._ox) / self._s, (y - self._oy) / self._s

    def paintEvent(self, ev):
        self._update_map()
        p = G.QPainter(self)
        p.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        # 1) 图：等比缩放 + 居中铺到画布（黑边由背景色兜底）
        pw, ph = self.dw * self._s, self.dh * self._s
        p.drawPixmap(QRectF(self._ox, self._oy, pw, ph),
                     self.pixmap, QRectF(0, 0, self.dw, self.dh))
        # 2) 选区：逻辑坐标 → 实际像素
        x0, y0, x1, y1 = self.sel
        mx0 = self._ox + x0 * self._s
        my0 = self._oy + y0 * self._s
        mx1 = self._ox + x1 * self._s
        my1 = self._oy + y1 * self._s
        px0, py0 = self._ox, self._oy
        px1, py1 = self._ox + pw, self._oy + ph
        dim = G.QColor(0, 0, 0, 128)
        # 选区外、图范围内的区域压暗
        p.fillRect(QRectF(px0, py0, px1 - px0, my0 - py0), dim)   # 上
        p.fillRect(QRectF(px0, my1, px1 - px0, py1 - my1), dim)   # 下
        p.fillRect(QRectF(px0, my0, mx0 - px0, my1 - my0), dim)   # 左
        p.fillRect(QRectF(mx1, my0, px1 - mx1, my1 - my0), dim)   # 右
        p.setPen(G.QPen(G.QColor("#00c2ff"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(mx0, my0, mx1 - mx0, my1 - my0))
        p.end()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            lx, ly = self._to_logical(ev.position().x(), ev.position().y())
            lx = min(max(lx, 0.0), float(self.dw))
            ly = min(max(ly, 0.0), float(self.dh))
            self._ax, self._ay = lx, ly
            self.sel = (lx, ly, lx, ly)
            self._dragging = True
            self.update()
            self.selChanged.emit()
            # 接受事件并提前返回：若走 super()（QWidget 默认 ignore），
            # 事件会向父级 content 传播，触发 GlassWindow 的标题区拖动，
            # 导致"画裁剪框时窗口跟着跑"。
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            lx, ly = self._to_logical(ev.position().x(), ev.position().y())
            lx = min(max(lx, 0.0), float(self.dw))
            ly = min(max(ly, 0.0), float(self.dh))
            r = (min(self._ax, lx), min(self._ay, ly),
                 max(self._ax, lx), max(self._ay, ly))
            self.sel = r
            self.update()
            self.selChanged.emit()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._dragging:
            self._dragging = False
            r = self.sel
            if r[2] - r[0] < 4 or r[3] - r[1] < 4:
                self.sel = (0, 0, self.dw, self.dh)
            self.update()
            self.selChanged.emit()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)


# --------------------------------------------------------------------------
class CropDialog(G.GlassWindow):
    """手动框选裁剪对话框。关闭后读 self.result（全分辨率 PIL 图；取消为 None）。"""

    MAX_W, MAX_H = 860, 560

    def __init__(self, parent, image: Image.Image, aspect=None):
        super().__init__(title="裁剪图片", subtitle="", minsize=(420, 420))
        self.parent_win = parent
        self.src = image.convert("RGB")
        self.aspect = aspect                 # (aw, ah) 或 None
        self.result: Image.Image | None = None

        self.scale = min(self.MAX_W / self.src.width,
                         self.MAX_H / self.src.height, 1.0)
        self.dw = max(1, round(self.src.width * self.scale))
        self.dh = max(1, round(self.src.height * self.scale))

        lay = self.content_lay
        lay.setContentsMargins(16, 64, 16, 16)
        lay.setSpacing(10)

        # 画布
        pm = _pil_to_qpixmap(self.src.resize((self.dw, self.dh), Image.LANCZOS))
        self.canvas = CropCanvas(pm, self.dw, self.dh)
        lay.addWidget(self.canvas)
        self.canvas.selChanged.connect(self._on_sel_change)

        # 控制条
        bar = QHBoxLayout()
        bar.setSpacing(6)
        tip = "锁定比例"
        if aspect:
            tip += f" {aspect[0]}:{aspect[1]}"
        self.lock = G.GlassCheck(tip)
        self.lock.setChecked(bool(aspect))
        bar.addWidget(self.lock)
        for txt, cmd in (("全选", self._sel_all),
                         ("按目标比例", self._sel_aspect),
                         ("去黑边", self._sel_content),
                         ("还原", self._sel_all)):
            b = G.GlassButton(txt, kind="ghost", height=34)
            b.clicked.connect(cmd)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.size_lab = _label("", G.INK3, G.SIZE_SMALL)
        lay.addWidget(self.size_lab)

        # 底部按钮
        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.btn_cancel = G.GlassButton("取消", kind="ghost", height=36)
        self.btn_cancel.clicked.connect(self._cancel)
        foot.addWidget(self.btn_cancel)
        self.btn_ok = G.GlassButton("确定", kind="primary", height=36)
        self.btn_ok.clicked.connect(self._ok)
        foot.addWidget(self.btn_ok)
        lay.addLayout(foot)

        self._draw()
        self._fit_window_to_content()
        self._center_on_parent()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

    def _fit_window_to_content(self):
        """把窗口撑到放得下内容（图 + 控制条 + 按钮），并限制在屏幕内。

        GlassWindow 是无边框、无顶层布局的裸 QWidget，sizeHint 无效
        （(-1,-1)），Qt 会退回默认 640×480——比裁剪内容小，图就溢出
        窗口（用户看到的"图片超出窗口范围"）。这里按 content_lay 的
        sizeHint 显式 resize，窗口初始尺寸就正确；同时把窗口最小尺寸
        设为内容最小尺寸（至少 minsize），缩窗时画布会等比缩小兜底。
        """
        hint = self.content_lay.sizeHint()
        m = self.content_lay.minimumSize()
        try:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            sw, sh = scr.width(), scr.height()
        except Exception:
            sw = sh = 10 ** 6
        min_w = max(self._minsize[0], m.width())
        min_h = max(self._minsize[1], m.height())
        w = max(min_w, min(hint.width(), sw - 24))
        h = max(min_h, min(hint.height(), sh - 24))
        self.setMinimumSize(min_w, min_h)
        self.resize(w, h)

    def _center_on_parent(self):
        try:
            pg = self.parent_win.frameGeometry()
            cg = self.frameGeometry()
            x = pg.x() + max(0, (pg.width() - cg.width()) // 2)
            y = pg.y() + max(0, (pg.height() - cg.height()) // 3)
            self.move(x, y)
        except Exception:
            pass

    # ---------------------------------------------------------- 选择框
    def _on_sel_change(self):
        self._draw()

    def _fit_aspect(self, r, bx, by):
        ar = self.aspect[0] / self.aspect[1]
        w = max(4.0, float(r[2] - r[0]))
        h = max(4.0, float(r[3] - r[1]))
        if w / h > ar:
            h = w / ar
        else:
            w = h * ar
        # 锚点存在 CropCanvas 上，对话框自己并没有 _ax / _ay
        # （原来写成 self._ax 会抛 AttributeError，打包版表现为点一下直接闪退）
        ax, ay = self.canvas._ax, self.canvas._ay
        x0 = ax if bx >= ax else ax - w
        y0 = ay if by >= ay else ay - h
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
        self.canvas.sel = (0, 0, self.dw, self.dh)
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
        self.canvas.sel = (x0, y0, x0 + w, y0 + h)
        self._draw()

    def _sel_content(self):
        bb = B.content_bbox(self.src, TRIM_THRESHOLD)
        if bb is None:
            self._sel_all()
            return
        s = self.scale
        self.canvas.sel = (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s)
        if self.lock.isChecked() and self.aspect:
            r = self.canvas.sel
            self.canvas._ax, self.canvas._ay = r[0], r[1]
            self.canvas.sel = self._fit_aspect(r, r[2], r[3])
        self._draw()

    def _draw(self):
        x0, y0, x1, y1 = self.canvas.sel
        sw = max(1, round((x1 - x0) / self.scale))
        sh = max(1, round((y1 - y0) / self.scale))
        # 目标画布尺寸来自当前输出尺寸（self.aspect 实为 (w, h)），
        # 不再写死旧 tkinter 时代的 293×400。
        if self.aspect:
            target = f" → 会等比缩放进 {self.aspect[0]}×{self.aspect[1]} 的画布"
        else:
            target = " → 会缩放进目标画布"
        self.size_lab.setText(
            f"裁剪区域：{sw} × {sh} 像素（原图 {self.src.width} × "
            f"{self.src.height}）{target}")
        self.canvas.update()

    # ---------------------------------------------------------- 结果
    def _source_rect(self):
        x0, y0, x1, y1 = self.canvas.sel
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
        self.close()

    # 注意：**不要**在 close() 之前把模态清成 NonModal（也不要在这里兜底清）。
    # 实测那样做会让 Qt 走"普通顶层控件"的关闭路径，不再把前台交还主窗口：
    # 关闭后 QApplication.activeWindow() 直接变成 None、主窗口 isActiveWindow()
    # 为 False，之后点击就落到别的程序（桌面上常开着资源管理器）上，表现为
    # "点确定后焦点跳到文件管理器、然后卡死"。模态由 Qt 在关闭时自行解除——
    # 确定 / 取消 / ✕ / Escape 四条路径都验过，关闭后 activeModalWidget() 均为 None。
    def closeEvent(self, ev):
        super().closeEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self._cancel()
        elif ev.key() == Qt.Key.Key_Return:
            self._ok()
        super().keyPressEvent(ev)


# --------------------------------------------------------------------------
class App(G.GlassWindow):
    def __init__(self):
        super().__init__(
            title="change bios logo",
            # 2026-10-05：用户反馈副标题太长占地方，整行删掉；标题区只留大标题，
            # 顶边距相应从 88 缩到 58（见下方 setContentsMargins）。
            subtitle="",
            # 2026-10-05：用户反馈窗口太大。原 logo / 替换结果 / 运行日志三个模块
            # 高度都已压缩，内容实际最小高度降到 ≈1150px；minsize 只作下限，
            # _fit_window 会取 max(minsize, 内容最小尺寸)，窗口按内容实际需要开。
            minsize=(1280, 1000))
        # 副标题删了，标题区只需要大标题（26pt ≈ 35px + 16 顶距），顶边距 88 → 58
        self.content_lay.setContentsMargins(20, 58, 20, 18)
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
        self._probe_job: QTimer | None = None
        self._probe_gen = 0
        self._probe_running = False
        self._crop_dlg = None              # 打开中的裁剪对话框（异步回调要持有引用）
        self.last_output: Path | None = None

        self._orig_photo = None
        self._new_photo = None
        self._busy = False
        self._ui_queue: queue.Queue = queue.Queue()
        self._preview_job: QTimer | None = None
        self._pv_src: dict = {0: None, 1: None}     # 两个预览框的源图
        self._pv_size: dict = {0: PREVIEW_BOX, 1: PREVIEW_BOX}
        self._pv_jobs: dict = {0: None, 1: None}    # 尺寸变化的重绘防抖

        self._build_ui()
        self._refresh_buttons()
        self.log(f"{APP_NAME} 已启动")
        self.log(f"工具目录：{self.tool_dir}")

        # UI 线程泵：从工作线程回传结果
        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(60)
        self._pump_timer.timeout.connect(self._pump)
        self._pump_timer.start()

        self._fit_window()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        # 图标
        try:
            ico = self.tool_dir / "app.ico"
            if ico.exists():
                self.setWindowIcon(QIcon(str(ico)))
        except Exception:
            pass

        # ================================================== ① 操作卡片
        tools = G.GlassCard(self.content, title="操作", glow=G.ACCENT)
        # 操作卡片行多（按钮条 + 槽位/适配/尺寸/位深/说明），父布局在窗口偏矮时
        # 会把它压到比内容还小，导致固定高度的控件互相叠压。给个最小高度兜底。
        # 2026-10-04：400 → 480，高 DPI 显示器（150%/150% 缩放）下 400 仍不够，
        # 输出尺寸行会被切掉一半、颜色位数行和说明文字完全看不见。
        # 2026-10-05：480 → 440，用户反馈窗口太大；窗口现在按内容实际需要开
        # （_fit_window 保证窗口 ≥ 内容最小高度），行与行不会叠压。
        tools.setMinimumHeight(440)
        self.content_lay.addWidget(tools)

        bar = QHBoxLayout()
        bar.setSpacing(9)
        self.btn_load = G.GlassButton("① 载入 BIOS 文件", kind="primary",
                                      height=42)
        self.btn_load.clicked.connect(self.on_load)
        self.btn_upload = G.GlassButton("② 上传新 Logo", kind="primary",
                                        height=42)
        self.btn_upload.clicked.connect(self.on_upload)
        self.btn_replace = G.GlassButton("③ Logo 替换", kind="primary",
                                         height=42)
        self.btn_replace.clicked.connect(self.on_replace)
        bar.addWidget(self.btn_load)
        bar.addWidget(self.btn_upload)
        bar.addWidget(self.btn_replace)
        bar.addStretch(1)
        self.btn_open_dir = G.GlassButton("打开工具目录", kind="ghost",
                                          height=42)
        self.btn_open_dir.clicked.connect(self.on_open_dir)
        bar.addWidget(self.btn_open_dir)
        tw_lay = tools.body_lay
        tw_lay.addLayout(bar)

        # ---- 槽位选择 ----
        bar_slot = QHBoxLayout()
        bar_slot.setSpacing(6)
        bar_slot.addWidget(_label("Logo 槽位："))
        self.slot_box = G.GlassCombo()
        self.slot_box.setFixedWidth(180)
        self.slot_box.addItems(["—"])
        self.slot_box.currentIndexChanged.connect(self.on_slot_change)
        bar_slot.addWidget(self.slot_box)
        bar_slot.addWidget(_label(
            "（一个固件里可能有多份 Logo，可分别查看与替换）", G.INK3,
            G.SIZE_SMALL))
        bar_slot.addStretch(1)
        tw_lay.addLayout(bar_slot)

        # ---- 图片适配控制 ----
        bar_fit = QHBoxLayout()
        bar_fit.setSpacing(6)
        self.auto_trim = G.GlassCheck("自动去黑边")
        self.auto_trim.toggled.connect(self._update_result_preview)
        bar_fit.addWidget(self.auto_trim)
        bar_fit.addWidget(_label("适配方式："))
        self.fit_box = G.GlassCombo()
        self.fit_box.setFixedWidth(130)
        self.fit_box.addItems([B.FIT_LABELS[m] for m in B.FIT_MODES])
        self.fit_box.setCurrentIndex(0)
        self.fit_box.currentIndexChanged.connect(self._on_fit_change)
        bar_fit.addWidget(self.fit_box)
        bar_fit.addWidget(_label("缩放："))
        self.zoom_slider = G.GlassSlider()
        self.zoom_slider.setRange(int(ZOOM_MIN * 100), int(ZOOM_MAX * 100))
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(130)
        self.zoom_slider.valueChanged.connect(self._on_zoom)
        bar_fit.addWidget(self.zoom_slider)
        self.zoom_lab = _label("1.00×", G.INK2)
        bar_fit.addWidget(self.zoom_lab)
        self.btn_crop = G.GlassButton("裁剪图片…", kind="ghost", height=34)
        self.btn_crop.clicked.connect(self.on_crop)
        bar_fit.addWidget(self.btn_crop)
        self.btn_fitreset = G.GlassButton("重置", kind="ghost", height=34)
        self.btn_fitreset.clicked.connect(self.on_reset_fit)
        bar_fit.addWidget(self.btn_fitreset)
        bar_fit.addStretch(1)
        tw_lay.addLayout(bar_fit)

        # ---- 输出尺寸（可任意，不要求与原 Logo 一致）----
        bar_size = QHBoxLayout()
        bar_size.setSpacing(6)
        bar_size.addWidget(_label("输出尺寸："))
        self.size_box = G.GlassCombo()
        self.size_box.setFixedWidth(230)
        self.size_box.addItems([SIZE_LABELS[m] for m in SIZE_MODES])
        self.size_box.setCurrentIndex(0)
        self.size_box.currentIndexChanged.connect(self._on_size_change)
        bar_size.addWidget(self.size_box)
        self.sp_w = G.GlassSpin()
        self.sp_w.setRange(SIZE_MIN, SIZE_MAX)
        self.sp_w.setFixedWidth(72)
        self.sp_w.valueChanged.connect(self._on_size_change)
        bar_size.addWidget(self.sp_w)
        bar_size.addWidget(_label("×"))
        self.sp_h = G.GlassSpin()
        self.sp_h.setRange(SIZE_MIN, SIZE_MAX)
        self.sp_h.setFixedWidth(72)
        self.sp_h.valueChanged.connect(self._on_size_change)
        bar_size.addWidget(self.sp_h)
        self.size_now = _label("", G.INK3, G.SIZE_SMALL)
        bar_size.addWidget(self.size_now)
        bar_size.addStretch(1)
        tw_lay.addLayout(bar_size)
        self._sync_size_widgets()

        # ---- 颜色位数 / 自动缩小（决定「一张图能放多大」）----
        bar_bpp = QHBoxLayout()
        bar_bpp.setSpacing(6)
        bar_bpp.addWidget(_label("颜色位数："))
        self.bpp_box = G.GlassCombo()
        self.bpp_box.setFixedWidth(300)
        self.bpp_box.addItems([BPP_LABELS[m] for m in BPP_MODES])
        self.bpp_box.setCurrentIndex(0)
        self.bpp_box.currentIndexChanged.connect(self._on_bpp_change)
        bar_bpp.addWidget(self.bpp_box)
        self.auto_fit = G.GlassCheck("放不下时自动缩小")
        self.auto_fit.setChecked(True)
        self.auto_fit.toggled.connect(self._on_bpp_change)
        bar_bpp.addWidget(self.auto_fit)
        self.bpp_now = _label("", G.INK3, G.SIZE_SMALL)
        bar_bpp.addWidget(self.bpp_now)
        bar_bpp.addStretch(1)
        tw_lay.addLayout(bar_bpp)
        self._update_bpp_now()

        # ---- 适配方式说明 ----
        self.fit_tip = _label("", G.INK3, G.SIZE_SMALL)
        self.fit_tip.setWordWrap(True)
        tw_lay.addWidget(self.fit_tip)
        self._update_fit_tip()

        # ================================================== ② 信息卡片
        info_card = G.GlassCard(self.content, title="BIOS / Logo 信息",
                                glow=G.ACCENT2)
        self.content_lay.addWidget(info_card)
        self.info = G.GlassText(self.content, kind="inset")
        self.info.setReadOnly(True)
        self.info.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.info.setFixedHeight(150)
        info_lay = info_card.body_lay
        info_lay.addWidget(self.info)
        self._set_info("尚未载入 BIOS 文件。\n\n请先点击「① 载入 BIOS 文件」。")

        # ================================================== ④ 预览卡片（占满中间）
        self.pv_orig_card, self.pv_orig = self._make_preview_card(
            0, "原 Logo（解析自 BIOS）", "点击图片 → 备份到工具目录")
        self.pv_new_card, self.pv_new = self._make_preview_card(
            1, "替换结果（实际写入固件的画面）", "点击图片 → 备份到工具目录")
        pv_hbox = QHBoxLayout()
        pv_hbox.setSpacing(14)
        pv_hbox.addWidget(self.pv_orig_card, 1)
        pv_hbox.addWidget(self.pv_new_card, 1)
        self.content_lay.addLayout(pv_hbox, 1)

        # ================================================== ③ 日志卡片（贴底）
        log_card = G.GlassCard(self.content, title="运行日志", glow=G.OK)
        self.content_lay.addWidget(log_card)
        self.log_text = G.GlassText(self.content, kind="log")
        self.log_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        # 2026-10-05：用户反馈运行日志模块太高，110 → 80（约 4 行，仍可滚动看全部）
        self.log_text.setFixedHeight(80)
        log_lay = log_card.body_lay
        log_lay.addWidget(self.log_text)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.status = _label("就绪", G.INK)
        srow.addWidget(self.status)
        srow.addStretch(1)
        self.prog = G.GlassProgress()
        self.prog.setFixedWidth(180)
        srow.addWidget(self.prog)
        log_lay.addLayout(srow)

    def _make_preview_card(self, col, title, hint):
        card = G.GlassCard(self.content, title=title,
                           glow=G.ACCENT if col == 0 else G.ACCENT2)
        holder = PreviewLabel(col)
        # 2026-10-05：用户反馈预览区太高。给预览框限高 220，窗口放大/最大化时
        # 两张预览卡也不会被拉得过高；默认窗口下预览框就按最小高度显示。
        holder.setMaximumHeight(220)
        holder.setPixmap(_pil_to_qpixmap(self._placeholder(PREVIEW_BOX)))
        hint_lab = _label(hint, G.INK3, G.SIZE_SMALL)
        lay = card.body_lay
        lay.addWidget(holder, 1)
        lay.addWidget(hint_lab)
        holder.clicked.connect(lambda c=col: self.on_preview_click(c))
        holder.resized.connect(self._on_preview_resize)
        self._pv_src[col] = None
        self._pv_size[col] = PREVIEW_BOX
        return card, holder

    def _fit_window(self):
        """按内容实际需求 + 屏幕可用范围定窗口大小。

        2026-10-04 修正：原先用 self.width()/self.height() 反推，但窗口在
        show() 之前尺寸还是 Qt 默认的 640×480 —— max(1320, 664)=1320、
        max(960, 488)=960，窗口被定成 1320×960，而内容实际需要约 1224px，
        于是底部的运行日志卡被切掉；紧接着的
        setMinimumSize(min(1200, w), min(900, h)) 又把最小尺寸压回 1200×900，
        所以只把 minsize 调到 (1600, 1400) 完全不起作用。
        现在直接读内容布局的最小尺寸（已含 20/58/20/18 边距；2026-10-05 副标题
        删除后顶边距 88 → 58）。
        """
        self.update()
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        ag = screen.availableGeometry()
        sw, sh = ag.width(), ag.height()

        # 内容真实需求：布局总最小尺寸（已含边距）
        self.content_lay.activate()
        need = self.content.minimumSizeHint()
        need_w = max(self._minsize[0], need.width())
        need_h = max(self._minsize[1], need.height())

        # 屏幕放不下时以屏幕可用区域为准
        avail_w = max(880, sw - 40)
        avail_h = max(640, sh - 60)
        w = min(need_w, avail_w)
        h = min(need_h, avail_h)
        self.resize(w, h)
        self.setMinimumSize(min(need_w, avail_w), min(need_h, avail_h))
        if os.environ.get("CBL_DEBUG_UI"):
            print(f"DEBUG screen={sw}x{sh} avail={avail_w}x{avail_h} "
                  f"need={need_w}x{need_h} -> geometry {w}x{h}", flush=True)

    # ------------------------------------------------------- 预览图渲染
    def _placeholder(self, box, text="（无）"):
        """空态占位图：深色底 + 居中灰字。"""
        from PIL import ImageDraw, ImageFont
        img = Image.new("RGB", box, G.hex2rgb(G.INSET))
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("msyh.ttc", 20)
        except Exception:
            font = ImageFont.load_default()
        try:
            l, t, r, b = d.textbbox((0, 0), text, font=font)
            tw, th = r - l, b - t
        except Exception:
            tw, th = d.textsize(text, font=font)
        d.text(((box[0] - tw) / 2, (box[1] - th) / 2), text, font=font,
               fill=G.hex2rgb(G.INK3))
        return img

    def _on_preview_resize(self, col, w, h):
        """预览区尺寸变了 → 防抖后按新尺寸重画（图片始终完整可见）。"""
        if w < 40 or h < 40:
            return
        self._pv_size[col] = (w, h)
        job = self._pv_jobs.get(col)
        if job is not None:
            job.stop()
        self._pv_jobs[col] = QTimer(self)
        self._pv_jobs[col].setSingleShot(True)
        self._pv_jobs[col].timeout.connect(lambda c=col: self._redraw_preview(c))
        self._pv_jobs[col].start(140)

    def _redraw_preview(self, col):
        self._pv_jobs[col] = None
        holder = self.pv_orig if col == 0 else self.pv_new
        self._show_preview(holder, self._pv_src.get(col),
                           "orig" if col == 0 else "new")

    def _preview_size(self, col, holder):
        """预览框的实时像素尺寸；布局还没成型时退回缓存/默认值。"""
        w, h = holder.width(), holder.height()
        if w < 60 or h < 60:
            w, h = self._pv_size.get(col) or PREVIEW_BOX
        self._pv_size[col] = (w, h)
        return (max(60, w), max(60, h))

    # ------------------------------------------------------------ helpers
    def _set_info(self, text: str):
        self.info.setPlainText(text)

    def log(self, msg: str):
        self.log_text.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def set_status(self, s: str):
        self.status.setText(s)

    def _refresh_buttons(self):
        busy = self._busy
        has_bios = self.src_data is not None
        has_slot = self.slot is not None
        has_new = self.new_image is not None

        def cfg(btn, cond):
            btn.setEnabled(not (busy or not cond))

        cfg(self.btn_load, True)
        cfg(self.btn_upload, has_bios)
        cfg(self.btn_replace, has_slot and has_new)
        cfg(self.btn_crop, has_new)
        cfg(self.btn_fitreset, has_slot and has_new)

    def set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        self.prog.set_busy(busy)
        self.set_status(text or "处理中…" if busy else "就绪")
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
            QMessageBox = G.QMessageBox
            QMessageBox.critical(self, APP_NAME, str(err))
        else:
            QMessageBox = G.QMessageBox
            QMessageBox.critical(self, APP_NAME, f"发生未预期错误：\n{err}")
        # 让调用方也能收尾
        try:
            done(None)
        except Exception:
            pass

    # ------------------------------------------------------------- ① 载入
    def on_load(self):
        _trace("① 载入 BIOS：正在打开文件对话框…")
        p, _ = QFileDialog.getOpenFileName(
            self, "选择 BIOS 文件", str(self.tool_dir),
            "\n".join(BIOS_TYPES))
        if not p:
            _trace("① 载入 BIOS：用户取消")
            return
        _trace(f"① 载入 BIOS：已选 {p}")
        self._load_path(Path(p))

    def _load_path(self, path):
        """载入并解析指定路径的 BIOS（按钮与命令行参数共用此入口）。"""
        _trace(f"① 载入 BIOS：解析 {path}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            G.QMessageBox.critical(self, APP_NAME, f"无法读取文件：{exc}")
            return

        self.src_path = path
        self.src_data = None
        self.slots = []
        self.slot = None
        self.orig_image = None
        self._orig_photo = None
        self._show_preview(self.pv_orig, None, "orig")
        self.slot_box.blockSignals(True)
        self.slot_box.clear()
        self.slot_box.addItem("—")
        self.slot_box.blockSignals(False)
        self.last_output = None
        self._refresh_buttons()

        self.log(f"载入 BIOS：{path}")
        self.log(f"文件大小：{size:,} 字节 ({human(size)})")
        self._set_info(
            f"BIOS 文件 : {path}\n"
            f"文件大小 : {size:,} 字节 ({human(size)})\n\n"
            f"正在自动解析固件，完成后会显示 Logo 槽位。")

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
            G.QMessageBox.warning(
                self, APP_NAME,
                "未在该固件中找到可替换的开机 Logo。\n\n"
                "可能该机型使用了其它存放方式（例如整卷压缩的内部 Logo）。")
            self._refresh_buttons()
            return

        self.slot_box.blockSignals(True)
        self.slot_box.clear()
        for s in slots:
            self.slot_box.addItem(
                f"#{s.index}  {s.bmp_info.width}x{s.bmp_info.rows}")
        self.slot_box.blockSignals(False)
        self.log(f"解析成功：找到 {len(slots)} 处 Logo。")
        self._select_slot(slots[0])

    # ------------------------------------------------------------- 槽位
    def on_slot_change(self, _idx=0):
        if 0 <= _idx < len(self.slots):
            self._select_slot(self.slots[_idx])

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
        lab = self.size_box.currentText()
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
        if src is not None and self.auto_trim.isChecked():
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
            w = self.sp_w.value()
            h = self.sp_h.value()
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
        enabled = mode == SIZE_CUSTOM
        for sp in (self.sp_w, self.sp_h):
            sp.setEnabled(enabled)
        if mode == SIZE_CUSTOM:
            if self.sp_w.value() == 0:
                self.sp_w.setValue(SIZE_FIXED_WH[0])
            if self.sp_h.value() == 0:
                self.sp_h.setValue(SIZE_FIXED_WH[1])
        else:
            # 非自定义模式：把框里的数字显示成「实际会输出」的尺寸（只读）
            try:
                w, h = self._out_size()
            except Exception:
                w, h = SIZE_FIXED_WH
            self.sp_w.blockSignals(True)
            self.sp_h.blockSignals(True)
            self.sp_w.setValue(w)
            self.sp_h.setValue(h)
            self.sp_w.blockSignals(False)
            self.sp_h.blockSignals(False)
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
                    sp.setEnabled(False)
                self.sp_w.blockSignals(True)
                self.sp_h.blockSignals(True)
                self.sp_w.setValue(SIZE_MIN)
                self.sp_h.setValue(SIZE_MIN)
                self.sp_w.blockSignals(False)
                self.sp_h.blockSignals(False)
                self.size_now.setText(txt)
                return
            src = self._source_for_size()
            note = "原图" + ("·去黑边后" if (self.auto_trim.isChecked()
                                              and src is not None) else "")
            if src is not None:
                note += f"，来自 {self.new_image.width}×{self.new_image.height}"
            txt = f"→ 输出 {sz[0]}×{sz[1]}（{note}）"
        elif mode == SIZE_FIXED:
            txt = f"→ 输出 {sz[0]}×{sz[1]}（固定）"
        else:
            txt = f"→ 输出 {sz[0]}×{sz[1]}（比例 {sz[0] / sz[1]:.2f}）"
        self.size_now.setText(txt)

    def _set_size_mode(self, mode: str):
        idx = list(SIZE_LABELS.values()).index(SIZE_LABELS[mode])
        self.size_box.blockSignals(True)
        self.size_box.setCurrentIndex(idx)
        self.size_box.blockSignals(False)
        if mode == SIZE_CUSTOM:
            self.sp_w.setValue(SIZE_FIXED_WH[0])
            self.sp_h.setValue(SIZE_FIXED_WH[1])
        self._sync_size_widgets()
        self._update_result_preview()

    def _on_size_change(self, *_a):
        self._sync_size_widgets()
        self._update_result_preview()

    # -------------------------------------------------------- 图片适配
    def _fit_mode(self) -> str:
        lab = self.fit_box.currentText()
        for m, l in B.FIT_LABELS.items():
            if l == lab:
                return m
        return B.FIT_CONTAIN

    def _update_fit_tip(self):
        mode = self._fit_mode()
        self.fit_tip.setText(f"● {B.FIT_LABELS[mode]}：{B.FIT_TIPS[mode]}")

    def _on_fit_change(self, *_a):
        self._update_fit_tip()
        self._sync_size_widgets()
        self._update_result_preview()

    # ------------------------------------------------------ 颜色位数 / 自动缩小
    def _out_bpp(self) -> int:
        lab = self.bpp_box.currentText()
        for m, l in BPP_LABELS.items():
            if l == lab:
                return m
        return BPP_24

    def _update_bpp_now(self):
        bpp = self._out_bpp()
        fit = "自动缩小：开" if self.auto_fit.isChecked() else "自动缩小：关"
        self.bpp_now.setText(f"→ {bpp} 位 · {fit}")

    def _on_bpp_change(self, *_a):
        self._update_bpp_now()
        bpp = self._out_bpp()
        self.log(f"颜色位数：{bpp} 位　{BPP_TIPS.get(bpp, '')}")
        self.log(f"放不下时自动缩小：{'开（任意大小的图片都能用）' if self.auto_fit.isChecked() else '关（放不下会直接报错）'}")
        self._update_result_preview()

    def _render_result(self):
        """按当前设置算出「真正会写进固件」的那张图。"""
        if self.slot is None or self.new_image is None:
            return None
        w, h = self._out_size()
        src = self.new_image
        if self.auto_trim.isChecked():
            src = B.trim_black_border(src, TRIM_THRESHOLD)
        return B.fit_image(src, w, h, mode=self._fit_mode(),
                           zoom=self._zoom())

    def _zoom(self) -> float:
        return self.zoom_slider.value() / 100.0

    def _update_result_preview(self, *_a):
        # 这条链路上的活儿（PIL 缩放 / 去黑边 / QPixmap 转换 / numpy 覆盖率）全在
        # 主线程同步跑。图片或输出尺寸很大时会耗时几秒，期间消息泵停下，Windows
        # 就会判定"程序未响应"。超过 1s 记一笔，事后能直接对上系统事件的时间线。
        _t0 = time.time()
        try:
            self._update_result_preview_inner(*_a)
        finally:
            dt = time.time() - _t0
            if dt >= 1.0:
                self.log(f"⚠ 预览重算耗时 {dt:.1f}s（这一步跑在主线程，"
                         f"期间界面不响应）—— 图片或输出尺寸偏大。")

    def _update_result_preview_inner(self, *_a):
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

        trimmed = self.auto_trim.isChecked()
        cov = B.coverage_ratio(out, threshold=TRIM_THRESHOLD)
        mode_cn = B.FIT_LABELS.get(self._fit_mode(), self._fit_mode())
        self._update_size_now()
        self.log(f"适配预览：{mode_cn}"
                 f"{' + 自动去黑边' if trimmed else ''}"
                 f" + {self._zoom():.2f}×  →  内容填充率 {cov * 100:.1f}%"
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
            self._probe_job.stop()
        self._probe_job = QTimer(self)
        self._probe_job.setSingleShot(True)
        self._probe_job.timeout.connect(lambda: self._run_fit_probe(out))
        self._probe_job.start(700)

    def _run_fit_probe(self, out):
        self._probe_job = None
        if self.slot is None or self.new_image is None:
            return
        if self._probe_running:
            # 上一次预检还在算，等它结束再补一次（避免拖着拖出十几个线程）
            self._probe_job = QTimer(self)
            self._probe_job.setSingleShot(True)
            self._probe_job.timeout.connect(lambda: self._run_fit_probe(out))
            self._probe_job.start(700)
            return
        slot = self.slot
        bpp = self._out_bpp()
        auto_fit = self.auto_fit.isChecked()
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
        zoom = self._zoom()
        trim = self.auto_trim.isChecked()
        size = self._out_size_arg()
        _trace(f"容量预检：开始 out_size={size} auto_fit={auto_fit}")
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
            _trace("容量预检：结束")
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
        if self.auto_trim.isChecked():
            src = B.trim_black_border(src, TRIM_THRESHOLD)
        if src.width <= 0 or src.height <= 0:
            return False
        zoom = self._zoom()
        if self._fit_mode() == B.FIT_STRETCH:
            return zoom > 1.0 + 1e-6
        sx = w / src.width
        sy = h / src.height
        r = max(sx, sy) if self._fit_mode() == B.FIT_COVER else min(sx, sy)
        return (src.width * r * zoom > w + 0.5 or
                src.height * r * zoom > h + 0.5)

    def _on_zoom(self, _v=None):
        self.zoom_lab.setText(f"{self._zoom():.2f}×")
        if self._preview_job is not None:
            self._preview_job.stop()
        self._preview_job = QTimer(self)
        self._preview_job.setSingleShot(True)
        self._preview_job.timeout.connect(self._update_result_preview)
        self._preview_job.start(140)

    def on_reset_fit(self):
        self.zoom_slider.setValue(100)
        self.zoom_lab.setText("1.00×")
        self.auto_trim.setChecked(False)
        self.fit_box.blockSignals(True)
        self.fit_box.setCurrentIndex(0)
        self.fit_box.blockSignals(False)
        self._set_size_mode(SIZE_ORIG)
        self._update_fit_tip()
        self._sync_size_widgets()
        self._update_result_preview()

    def on_crop(self):
        if self.new_image is None:
            G.QMessageBox.information(self, APP_NAME, "请先上传新的 Logo 图片。")
            return
        aspect = self._out_size() if self.slot is not None else None
        # 2026-10-05：裁剪对话框始终用「上传原图」(new_image_raw) 作底图，而非
        # 「当前工作图」(new_image，可能已被上次裁剪改小)。否则第二次点裁剪时面板
        # 显示的是上次裁剪结果，再框选只会得到其子区域，且下方「替换结果」预览常
        # 与第一次相同、看似没变。用原图作底图后每次裁剪都是对原图的独立框选。
        # (new_image_raw 与 new_image 上传时同时赋值，上面的 None 守卫同样适用。)
        dlg = CropDialog(self, self.new_image_raw, aspect=aspect)
        # 不用"嵌套事件循环等它关闭"的写法：那种写法一旦 closed 没有发出（或关闭路径
        # 出异常），主界面就会永远停在那层循环里——用户看到的就是"点确定后整个程序
        # 卡死、点什么都没反应、也关不掉"。改成纯异步：关闭时回调，主循环一直活着。
        self._crop_dlg = dlg
        _trace("裁剪：对话框已打开")
        dlg.closed.connect(lambda: self._finish_crop(dlg))
        dlg.show()

    def _finish_crop(self, dlg):
        self._crop_dlg = None
        res = dlg.result
        try:
            dlg.deleteLater()
        except Exception:
            pass
        if res is None:
            _trace("裁剪：已取消")
            return
        self.new_image = res
        _trace(f"裁剪：套用结果 {res.width}x{res.height}")
        raw = self.new_image_raw
        note = f"{raw.width}x{raw.height}" if raw is not None else "未知"
        self.log(f"已裁剪图片：{res.width}x{res.height}（原始上传图 {note}）")
        self._update_result_preview()
        _trace("裁剪：预览已刷新")

    # ------------------------------------------------------------ ③ 上传
    def on_upload(self):
        _trace("② 上传新 Logo：正在打开文件对话框…")
        p, _ = QFileDialog.getOpenFileName(
            self, "选择新的 Logo 图片", str(self.tool_dir),
            "\n".join(IMAGE_TYPES))
        if not p:
            _trace("② 上传新 Logo：用户取消")
            return
        _trace(f"② 上传新 Logo：已选 {p}")
        path = Path(p)
        try:
            size = path.stat().st_size
        except OSError as exc:
            G.QMessageBox.critical(self, APP_NAME, f"无法读取文件：{exc}")
            return

        if size > MAX_LOGO_BYTES:
            G.QMessageBox.critical(
                self, APP_NAME,
                f"图片异常巨大：{size:,} 字节 ({human(size)})\n\n"
                f"超过保护上限 {human(MAX_LOGO_BYTES)}，不予载入。\n"
                f"（Logo 生成的文件大小与输入图片大小无关，正常图片不会被拒。）")
            self.log(f"已拒绝 {path.name}：{size:,} 字节，超过保护上限 "
                     f"{human(MAX_LOGO_BYTES)}")
            return

        # 解码在主线程上：大图 + 慢盘 + 杀毒扫描时这一步可能耗时数秒，期间消息泵
        # 是停的，Windows 会判定"程序未响应"。把耗时写进日志，下次再卡就能立刻
        # 分辨是不是它。
        _t0 = time.time()
        _trace(f"② 上传新 Logo：开始解码 {path.name}（{human(size)}）")
        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:
            G.QMessageBox.critical(
                self, APP_NAME,
                f"无法识别为图片文件：\n{exc}\n\n只支持图片格式（PNG / JPG / BMP / GIF / WebP / TIFF）。")
            return
        _trace(f"② 上传新 Logo：解码完成 {img.width}x{img.height} {img.mode}，"
               f"耗时 {time.time() - _t0:.2f}s")

        self.new_path = path
        self.new_image_raw = img
        self.new_image = img
        self.log(f"载入新 Logo：{path.name}  {img.width}x{img.height} {img.mode}  "
                 f"{size:,} 字节 ({human(size)})")

        # 每次上传都回到最稳的默认：不去黑边 + 完整显示（不裁切）+ 原图尺寸
        self.auto_trim.setChecked(False)
        self.fit_box.blockSignals(True)
        self.fit_box.setCurrentIndex(0)
        self.fit_box.blockSignals(False)
        self.zoom_slider.setValue(100)
        self.zoom_lab.setText("1.00×")
        # 「原图尺寸」= SIZE_SOURCE（用上传图片自己的像素尺寸）。
        # 之前漏了这一步，上传后尺寸模式会停留在用户上次的选择。
        self._set_size_mode(SIZE_SOURCE)
        self._update_fit_tip()
        self._sync_size_widgets()
        self._update_result_preview()
        self._refresh_buttons()

    # ------------------------------------------------------------ ④ 替换
    def on_replace(self):
        if self.slot is None:
            G.QMessageBox.information(self, APP_NAME, "请先解析出 BIOS 中的 Logo。")
            return
        if self.new_image is None:
            G.QMessageBox.information(self, APP_NAME, "请先上传新的 Logo 图片。")
            return

        stem = self.src_path.stem
        suffix = self.src_path.suffix
        default = str(self.src_path.with_name(f"{stem}_newlogo{suffix}"))
        _trace("③ Logo 替换：正在打开保存对话框…")
        out, _ = QFileDialog.getSaveFileName(
            self, "保存新的 BIOS 文件", default,
            f"BIOS 固件;*{suffix}\n所有文件;*.*")
        if not out:
            _trace("③ Logo 替换：用户取消")
            return
        _trace(f"③ Logo 替换：输出到 {out}")

        slot = self.slot
        data = self.src_data
        img = self.new_image
        out_path = Path(out)
        mode = self._fit_mode()
        zoom = self._zoom()
        auto_trim = self.auto_trim.isChecked()
        out_size = self._out_size_arg()
        out_bpp = self._out_bpp()
        auto_fit = self.auto_fit.isChecked()
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
        ret = G.QMessageBox.question(
            self, APP_NAME, msg,
            G.QMessageBox.StandardButton.Yes | G.QMessageBox.StandardButton.No)
        if ret == G.QMessageBox.StandardButton.Yes:
            self._open_dir(out_path.parent)

    # ------------------------------------------------------------ 预览/备份
    def _show_preview(self, holder, img, which: str):
        col = 0 if which == "orig" else 1
        self._pv_src[col] = img
        box = self._preview_size(col, holder)
        if img is None:
            pm = _pil_to_qpixmap(self._placeholder(box))
        else:
            thumb = img.copy()
            thumb.thumbnail((box[0] - 10, box[1] - 10), Image.LANCZOS)
            canvas = Image.new("RGB", box, G.hex2rgb(G.INSET))
            canvas.paste(thumb, ((box[0] - thumb.width) // 2,
                                 (box[1] - thumb.height) // 2))
            pm = _pil_to_qpixmap(canvas)
        holder.setPixmap(pm)
        if which == "orig":
            self._orig_photo = pm
        else:
            self._new_photo = pm

    def on_preview_click(self, col: int):
        if col == 0:
            img, src_desc, tag = self.orig_image, "从 BIOS 解出的原 Logo", "原Logo"
        else:
            # 开了自动缩小时，备份真正写进固件的那张（可能已被缩小）
            img = self.fitted_image or self._render_result()
            src_desc, tag = "实际写入固件的替换结果", "新Logo"
        if img is None:
            G.QMessageBox.information(self, APP_NAME, "还没有可备份的图片。")
            return
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            name = f"{tag}_{stamp()}.png"
            dst = self.backup_dir / name
            img.save(dst, "PNG")
        except Exception as exc:
            G.QMessageBox.critical(self, APP_NAME, f"备份失败：\n{exc}")
            return
        self.log(f"已备份{src_desc} → {dst}")
        self.set_status(f"已备份：{name}")
        G.QMessageBox.information(
            self, APP_NAME,
            f"已备份{src_desc}到工具目录：\n\n{dst}")

    # ------------------------------------------------------------ 杂项
    def on_open_dir(self):
        self._open_dir(self.tool_dir)

    def _open_dir(self, p: Path):
        """在**后台线程**里调 ShellExecute。

        os.startfile 会同步等待外壳（explorer）以及与目录关联的外壳扩展；被杀毒
        软件、网络盘或失效的缩略图处理器拖住时，它会一直占着主线程不放，用户看到
        的就是"点一下整个程序不动了"。打开目录本来就是"发起后就不管"的动作，
        丢进 daemon 线程最安全；失败的提示再经 _ui_queue 回到主线程。
        """
        _trace(f"打开目录：{p}")

        def work():
            try:
                os.startfile(str(p))      # noqa: S606  (Windows 专用)
            except Exception as exc:
                self._ui_queue.put((self.log, (f"无法打开目录：{exc}",)))

        threading.Thread(target=work, daemon=True).start()


_error_fh = None


def _error_log_handle():
    """返回错误日志的文件句柄（只开一次，之后复用）。

    给 faulthandler 用：它需要真实的 ``fileno()``，而且要在解释器已经无法正常
    调度 Python 线程的那一刻（也就是转储栈的时候）依然写得进去。
    """
    global _error_fh
    if _error_fh is not None:
        return _error_fh
    try:
        _error_fh = open(tool_dir() / "change-bios-logo-error.log",
                         "a", encoding="utf-8", buffering=1)
    except Exception:
        return None
    return _error_fh


def _install_ui_watchdog(stall_after: float = 2.5):
    """UI 线程停摆超过 stall_after 秒时，把**所有**线程的栈写进错误日志。

    为什么不用"Python 看门狗线程 + 心跳计时器"：一旦主线程卡在**不放 GIL** 的
    C 调用里（Qt、PIL 解码器、文件对话框、ShellExecute 都可能这样），解释器连
    那个看门狗线程都调度不了，它自己的 sleep、计时和 sys._current_frames() 会
    一起失效 —— 这正是之前 6 次真实卡死（Windows 事件日志 Application Hang /
    AppHangB1）一次栈都没留下的原因。

    faulthandler.dump_traceback_later 的定时器在 C 层：到期就写全部线程栈，
    **不需要 GIL**。主线程健康时每秒 cancel 再重新 arm，所以正常运行永不转储；
    只有它真的停摆，已 armed 的定时器才会到期落盘（repeat=True，卡住期间反复
    转储，不会像原来那样"只报一次"就永久失效）。

    阈值 2.5s 特意压在 Windows 判定"程序停止响应"（约 5s）之下，保证日志里
    一定先看到栈，而不是只看到系统事件。
    """
    import faulthandler

    fh = _error_log_handle()
    if fh is None:
        return

    try:
        # 硬崩溃（0xc0000409 这类 abort / 段错误）也留下 Python 栈，
        # 别再只有 Windows 事件日志里的一行 BEX64。
        faulthandler.enable(file=fh, all_threads=True)
    except Exception:
        pass

    def arm(secs):
        try:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.dump_traceback_later(secs, repeat=True, file=fh)
        except Exception:
            pass

    def refresh():
        arm(stall_after)                   # 事件循环已在跑，此后按 2.5s 判定停摆

    timer = QTimer()
    timer.setInterval(1000)
    timer.timeout.connect(refresh)
    timer.start()
    globals()["_watchdog_timer"] = timer   # 持有引用，别让定时器被回收

    # 启动期给足余量：app.exec() 之前没有事件循环，心跳续不了期，而冷启动
    # （onefile 解包 + 杀毒扫描 + App() 构造控件）本来就可能偏慢，别把正常的
    # 冷启动误报成卡死。第一拍只在这里 arm 一次；第一声心跳之后就收紧到 2.5s。
    arm(20.0)
    _trace(f"看门狗已就位：主线程停摆 >{stall_after:.1f}s 就把全部线程栈写进本日志"
           f"（faulthandler，不依赖 GIL）")


def _trace(msg: str):
    """把关键步骤写到 stderr（窗口版由 _ensure_std_streams 接到 exe 同目录的
    change-bios-logo-error.log），用于定位"到底卡在哪一步"。"""
    try:
        print(f"[trace {time.strftime('%H:%M:%S')}] {msg}",
              file=sys.stderr, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
def _ensure_std_streams():
    """让 --windowed 冻结版也有可写的 stdout / stderr。

    PyInstaller 的窗口版没有控制台，sys.stdout / sys.stderr 是 None；此时任何槽函数
    抛出未捕获异常，PySide6 打印 traceback 的动作本身就会失败并把进程 abort
    （0xc0000409，用户看到的就是"点一下直接闪退"）。接到 exe 同目录的日志文件后，
    异常至少留下可查痕迹，也不会再变成致命错误。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        fh = open(tool_dir() / "change-bios-logo-error.log",
                  "a", encoding="utf-8", buffering=1)
    except Exception:
        return None
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh
    return fh


class _GuardedApplication(QApplication):
    """兜住槽函数 / 事件处理里的未捕获异常：记日志 + 提示一次，而不是整个进程死掉。"""

    def __init__(self, argv):
        super().__init__(argv)
        self._reported: set[str] = set()

    def notify(self, receiver, event):
        try:
            return super().notify(receiver, event)
        except Exception:
            self._report(traceback.format_exc())
            return False

    def _report(self, tb: str):
        try:
            print(tb, file=sys.stderr, flush=True)
        except Exception:
            pass
        lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
        last = lines[-1] if lines else "未知错误"
        if last in self._reported or len(self._reported) >= 3:
            return
        self._reported.add(last)
        # 延后到本次事件派发之后弹框，避免在绘制 / 布局过程中嵌套弹窗
        QTimer.singleShot(0, lambda m=last: G.QMessageBox.critical(
            None, APP_NAME, f"程序内部出错（已写入日志，未退出）：\n\n{m}"))


def main():
    _ensure_std_streams()          # 必须在 QApplication 之前，Qt 初始化就可能写 stderr
    # 高 DPI：不再手动 SetProcessDpiAwareness(1)（SYSTEM_AWARE 会把 Qt6 默认的
    # PER_MONITOR_AWARE_V2 降级，导致文字按系统 DPI 渲染再放大而发糊）。
    # 交给 Qt6 自己按显示器逐屏感知，文字最清晰。
    # 可选：命令行直接指定 BIOS 文件路径，跳过文件选择对话框
    cli_args = [a for a in sys.argv[1:] if a]

    app = _GuardedApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # 主线程停摆 >2.5s 就把全部线程栈写进错误日志（faulthandler，不依赖 GIL）。
    # 阈值压在 Windows 判定"未响应"（约 5s）之下，保证日志先于系统事件留下线索。
    _install_ui_watchdog()
    win = App()
    win.show()
    if cli_args:
        win._load_path(Path(cli_args[0]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
