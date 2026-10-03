"""毛玻璃（glassmorphism）视觉工具箱 —— 纯 tkinter + Pillow 实现。

tkinter 本身没有透明控件，窗口也不支持逐像素 alpha。所以这里走的是
**「自己画玻璃」** 的路线，分两层：

1. **底层**：整个窗口铺一张用 Pillow 生成的背景图 —— 深色渐变 + 几团
   高斯模糊的彩色光斑 + 轻微噪点，视觉上就是一块磨砂玻璃。
2. **卡片层**：每张卡片是一个 ``GlassCard``（``tk.Frame`` 内嵌一块
   ``place`` 满铺的 ``tk.Canvas``）。它在 ``<Configure>`` 时从背景图上
   **裁出自己那块区域**，把半透明圆角面板、1px 顶部高光、外发光/投影
   合成上去，再贴回画布。于是卡片是真正"压在玻璃上"的，而不是贴了张
   灰底图。

卡片内部的控件（Text / ttk 控件）仍然是实色的 —— 这是 tkinter 的硬限
制，无法逐像素透明。做法是让它们的底色取卡片合成后的**平均色**，差值
控制在 1~2 个色阶，肉眼看不出来。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFilter, ImageTk

# --------------------------------------------------------------------------
# 调色板
# --------------------------------------------------------------------------
BG0 = "#070b12"          # 窗口最底色（背景图没铺满时露出）
PANEL = "#131c2b"        # 卡片基色（半透明叠加用）
PANEL_IN = "#101826"     # 卡片内部实色 = PANEL 以 ~0.62 叠在背景上的结果
PANEL_SOLID = PANEL_IN   # 别名：子控件 / ttk 主题统一用它当底色
INSET = "#0a1119"        # 内嵌区域（信息框 / 日志框 / 预览底）
INSET_ALT = "#080e16"
BORDER = "#2a3a54"       # 卡片描边
BORDER_HI = "#3d5274"    # 高光描边

INK = "#e7eef9"          # 主文字
INK2 = "#93a7c1"         # 次级文字
INK3 = "#5f7391"         # 提示文字

ACCENT = "#4c8dff"
ACCENT_HI = "#6ea6ff"
ACCENT_LO = "#2f6fd8"
ACCENT2 = "#8b5cf6"
OK = "#39d98a"
WARN = "#ffb454"
ERR = "#ff6b81"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# 卡片填充的竖向渐变 alpha（上淡下实）
CARD_ALPHA_TOP = 172
CARD_ALPHA_BOT = 146

# 背景光斑：(中心x比例, 中心y比例, 半径比例, 颜色)
_BLOBS = [
    (0.06, 0.02, 0.44, "#1e46c8"),
    (0.96, 0.06, 0.38, "#7a35e8"),
    (0.78, 0.94, 0.42, "#0f7f96"),
    (0.14, 0.98, 0.36, "#a02468"),
    (0.50, 0.42, 0.34, "#1d3f7a"),
]


# --------------------------------------------------------------------------
# 基础图形
# --------------------------------------------------------------------------
def _rounded(size, radius, fill, outline=None, width=1, ss=4):
    """抗锯齿圆角矩形（4 倍超采样再缩回来）。"""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle(
        [0, 0, w * ss - 1, h * ss - 1],
        radius=max(0, int(radius * ss)),
        fill=fill,
        outline=outline,
        width=max(1, int(width * ss)),
    )
    return big.resize((w, h), Image.LANCZOS)


def _blend(rgb, overlay_rgba):
    """把 RGBA 叠到 RGB 上，返回 RGB（用于算"子控件该用什么实色"）。"""
    r, g, b = rgb[:3]
    orr, og, ob, oa = overlay_rgba
    a = oa / 255.0
    return (round(r * (1 - a) + orr * a),
            round(g * (1 - a) + og * a),
            round(b * (1 - a) + ob * a))


def hex2rgb(s: str):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(t) -> str:
    return "#%02x%02x%02x" % (int(t[0]), int(t[1]), int(t[2]))


# --------------------------------------------------------------------------
# 背景（磨砂玻璃）
# --------------------------------------------------------------------------
def build_backdrop(w: int, h: int) -> Image.Image:
    """生成整窗背景：深色渐变 + 模糊光斑 + 噪点。"""
    w, h = max(2, int(w)), max(2, int(h))

    # 1) 低分辨率画光斑，再放大 —— 又便宜又平滑
    sw, sh = max(8, w // 10), max(8, h // 10)
    small = Image.new("RGB", (sw, sh), hex2rgb(BG0))
    d = ImageDraw.Draw(small)
    for cx, cy, r, col in _BLOBS:
        X, Y, R = cx * sw, cy * sh, r * sw
        d.ellipse([X - R, Y - R, X + R, Y + R], fill=hex2rgb(col))
    small = small.filter(ImageFilter.GaussianBlur(max(2.0, sw * 0.16)))
    img = small.resize((w, h), Image.BICUBIC)

    # 2) 上下压暗，让中间内容区更沉得住
    shade = Image.new("L", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        # 顶部稍亮、底部稍暗，中间最暗
        v = 232 - int(96 * abs(t - 0.42) / 0.58)
        shade.putpixel((0, y), max(0, min(255, v)))
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    img = Image.composite(img, dark, shade.resize((w, h)))

    # 3) 轻微噪点，去掉"塑料感"
    noise = Image.effect_noise((w, h), 18).convert("L")
    veil = Image.new("RGB", (w, h), (150, 170, 205))
    img = Image.blend(img, Image.composite(veil, img, noise), 0.05)
    return img


_BACKDROP: Image.Image | None = None
_BACKDROP_SIZE = (0, 0)


def init_backdrop(w: int, h: int) -> Image.Image:
    global _BACKDROP, _BACKDROP_SIZE
    _BACKDROP = build_backdrop(w, h)
    _BACKDROP_SIZE = (w, h)
    return _BACKDROP


def backdrop() -> Image.Image:
    return _BACKDROP


def window_image() -> Image.Image:
    """窗口用的背景（确保已生成）。"""
    global _BACKDROP
    if _BACKDROP is None:
        _BACKDROP = build_backdrop(1280, 960)
    return _BACKDROP


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------
class GlassCard(tk.Frame):
    """半透明圆角卡片。

    用法与 ``ttk.LabelFrame`` 类似，但子控件要放进 ``card.body``::

        card = GlassCard(parent, title="BIOS / Logo 信息")
        card.pack(fill="x", padx=16, pady=(0, 10))
        tk.Text(card.body, ...).pack(...)

    ``card.solid_bg`` 是卡片合成后的平均色，给它里面的实色控件当底色用。
    """

    SHADOW = 7          # 画布四周留给投影/外发光的边距
    TITLE_H = 0         # 由 title 决定

    def __init__(self, parent, title: str = "", radius: int = 14,
                 pad=(14, 12, 14, 12), glow: str | None = None, **kw):
        super().__init__(parent, bg=PANEL_SOLID, bd=0, highlightthickness=0, **kw)
        self._radius = radius
        self._title = title or ""
        self._glow = glow
        self._pad = pad
        self._img_ref = None
        self.solid_bg = PANEL_SOLID

        self._cv = tk.Canvas(self, bg=PANEL_SOLID, bd=0, highlightthickness=0)
        self._cv.place(x=0, y=0, relwidth=1, relheight=1)

        top = self.SHADOW + pad[1] + (22 if self._title else 0)
        self.body = tk.Frame(self, bg=PANEL_SOLID, bd=0, highlightthickness=0)
        self.body.pack(fill="both", expand=True,
                       padx=self.SHADOW + pad[0], pady=(top, self.SHADOW + pad[3]))

        self.bind("<Configure>", self._redraw)

    # ---------------------------------------------------------------- 绘制
    def _panel_box(self):
        w = self.winfo_width()
        h = self.winfo_height()
        m = self.SHADOW
        return m, m, max(1, w - 2 * m), max(1, h - 2 * m)

    def _redraw(self, _evt=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        x, y, pw, ph = self._panel_box()

        # 从整窗背景里裁出自己这一块；拿不到就退回纯色
        img = window_image()
        try:
            rx = self.winfo_rootx() - self.winfo_toplevel().winfo_rootx()
            ry = self.winfo_rooty() - self.winfo_toplevel().winfo_rooty()
        except Exception:
            rx = ry = 0
        box = (rx, ry, rx + w, ry + h)
        if box[0] >= 0 and box[1] >= 0 and box[2] <= img.width and box[3] <= img.height:
            base = img.crop(box).convert("RGBA")
        else:
            base = Image.new("RGBA", (w, h), hex2rgb(BG0) + (255,))

        # --- 投影 / 外发光 ---
        sh = _rounded((pw, ph), self._radius, (0, 0, 0, 130))
        sh = sh.filter(ImageFilter.GaussianBlur(self.SHADOW * 0.85))
        base.alpha_composite(sh, (x, y + 2))
        if self._glow:
            gl = _rounded((pw, ph), self._radius, hex2rgb(self._glow) + (70,))
            gl = gl.filter(ImageFilter.GaussianBlur(self.SHADOW * 1.15))
            base.alpha_composite(gl, (x, y))

        # --- 面板本体：竖向微渐变，读起来才像玻璃 ---
        panel = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        pdr = ImageDraw.Draw(panel)
        base_rgb = hex2rgb(PANEL)
        for i in range(ph):
            t = i / max(1, ph - 1)
            a = int(CARD_ALPHA_TOP - (CARD_ALPHA_TOP - CARD_ALPHA_BOT) * t)
            pdr.line([(0, i), (pw, i)], fill=base_rgb + (a,))
        panel = panel.filter(ImageFilter.GaussianBlur(0.4))
        mask = _rounded((pw, ph), self._radius, (255, 255, 255, 255)).getchannel("A")
        base.paste(panel, (x, y), mask)

        # --- 1px 描边 + 顶部/左侧高光 ---
        base.alpha_composite(
            _rounded((pw, ph), self._radius, None, hex2rgb(BORDER) + (190,), 1), (x, y))
        r = max(2, self._radius)
        hi = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hi)
        hd.line([(r, 1), (pw - r, 1)], fill=(255, 255, 255, 60), width=1)
        hd.line([(1, r), (1, ph - r)], fill=(255, 255, 255, 24), width=1)
        hd.line([(pw - 2, r), (pw - 2, ph - r)], fill=(255, 255, 255, 14), width=1)
        base.alpha_composite(hi, (x, y))

        # --- 标题 ---
        if self._title:
            td = ImageDraw.Draw(base)
            # 左侧小色条
            gx, gy = x + 14, y + 13
            td.rounded_rectangle([gx, gy + 1, gx + 3, gy + 13], radius=1,
                                 fill=hex2rgb(self._glow or ACCENT) + (255,))
            base.alpha_composite(
                _text_img(self._title, FONT_UI, 10, INK2), (gx + 11, gy))

        # 子控件一律用 PANEL_IN 实色。卡片填充的 alpha 就是按"叠在背景上
        # 恰好等于 PANEL_IN"选的，所以这里不跟着采样走 —— 否则 ttk 控件的
        # 全局底色（改不动）会和 body 对不上，反而露出色块。
        self.solid_bg = PANEL_IN

        self._img_ref = ImageTk.PhotoImage(base.convert("RGB"))
        self._cv.delete("bg")
        self._cv.create_image(0, 0, image=self._img_ref, anchor="nw", tags="bg")
        # 注意：tk.Canvas.lower() 被重写成了 tag_lower，必须走 Misc.lower
        tk.Misc.lower(self._cv)


# --------------------------------------------------------------------------
# 文字贴图（给 Canvas 上用）
# --------------------------------------------------------------------------
_FONT_FILES = ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "arial.ttf")


def load_font(px: int):
    """按像素大小取一个可用的 TrueType 字体；找不到就退回 Pillow 内置位图字体。"""
    from PIL import ImageFont
    for name in _FONT_FILES:
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_img(text: str, family: str, size: int, color: str,
              weight: str = "normal") -> Image.Image:
    """画一张带 alpha 的文字贴图（2 倍超采样后缩回）。"""
    font = load_font(size * 2)
    bbox = font.getbbox(text)
    tw = max(1, bbox[2] - bbox[0]) + 4
    th = max(1, bbox[3] - bbox[1]) + 4
    img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((2 - bbox[0], 2 - bbox[1]), text, font=font, fill=hex2rgb(color) + (255,))
    return img.resize((max(1, tw // 2), max(1, th // 2)), Image.LANCZOS)


# --------------------------------------------------------------------------
# 圆角按钮
# --------------------------------------------------------------------------
class GlassButton(tk.Canvas):
    """自绘圆角按钮，兼容 ``configure(state=...)`` / ``configure(text=...)``。"""

    def __init__(self, parent, text: str = "", command=None, kind: str = "normal",
                 width: int | None = None, height: int = 34, radius: int = 9,
                 font_size: int = 9, bg: str = PANEL_SOLID):
        self._label = text
        self._command = command
        self._kind = kind                     # normal | primary | ghost
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._radius = radius
        self._font_size = font_size
        self._img_ref = None

        f = (FONT_UI, font_size, "bold" if kind == "primary" else "normal")
        probe = tk.Label(parent, text=text, font=f)
        probe.update_idletasks()
        tw = probe.winfo_reqwidth()
        th = probe.winfo_reqheight()
        probe.destroy()
        w = width or (tw + 30)
        super().__init__(parent, width=w, height=height, bg=bg,
                         bd=0, highlightthickness=0, cursor="hand2")

        self._font = f
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        # 窗口首次映射 / 被拉伸时才拿得到真实尺寸，那时要重画一次
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    # ---- 兼容 tk 的 configure/cget ----
    def configure(self, cnf=None, **kw):
        state = kw.pop("state", None)
        text = kw.pop("text", None)
        bg = kw.pop("bg", None)
        if state is not None:
            self._state = state
        if text is not None:
            self._label = text
        if bg is not None:
            super().configure(bg=bg)
        if cnf:
            super().configure(cnf)
        if kw:
            super().configure(**kw)
        if state is not None or text is not None:
            self._draw()
        return None

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._label
        return super().cget(key)

    # ---- 状态 ----
    def _enabled(self):
        return self._state != "disabled"

    def _on_enter(self, _e):
        if self._enabled():
            self._hover = True
            self._draw()

    def _on_leave(self, _e):
        self._hover = self._pressed = False
        self._draw()

    def _on_press(self, _e):
        if self._enabled():
            self._pressed = True
            self._draw()

    def _on_release(self, _e):
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and self._enabled() and self._command:
            self._command()

    # ---- 绘制 ----
    def _colors(self):
        if not self._enabled():
            return ("#1a2331", "#26313f", "#54637a")
        if self._kind == "primary":
            top = ACCENT_HI if self._hover else ACCENT
            bot = ACCENT_LO
            if self._pressed:
                top, bot = ACCENT_LO, ACCENT_LO
            return (top, bot, "#ffffff")
        if self._kind == "ghost":
            return ("#1a2537" if self._hover else "#151d2b", "#151d2b", INK2)
        return ("#23304a" if self._hover else "#1b2433",
                "#161e2b", INK)

    def _draw(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            w, h = int(self["width"]), int(self["height"])
        if w <= 1 or h <= 1:
            return

        # 整块按钮在 S 倍分辨率上画完再缩回来 —— 文字和圆角一起享受抗锯齿。
        S = 4
        W, H = w * S, h * S
        top, bot, fg = self._colors()
        c1, c2 = hex2rgb(top), hex2rgb(bot)

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for i in range(H):
            t = i / max(1, H - 1)
            d.line([(0, i), (W, i)],
                   fill=tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3)) + (255,))
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, W - 1, H - 1], radius=self._radius * S, fill=255)
        img.putalpha(mask)

        d = ImageDraw.Draw(img)
        if self._enabled():
            d.rounded_rectangle([0, 0, W - 1, H - 1], radius=self._radius * S,
                                outline=(255, 255, 255, 44), width=S)
            # 顶部高光
            d.line([(self._radius * S, S), (W - self._radius * S, S)],
                   fill=(255, 255, 255, 52), width=S)

        font = load_font(self._font_size * S)
        bb = d.textbbox((0, 0), self._label, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        d.text(((W - tw) / 2 - bb[0], (H - th) / 2 - bb[1]), self._label,
               font=font, fill=hex2rgb(fg) + (255,))

        img = img.resize((w, h), Image.LANCZOS)
        self._img_ref = ImageTk.PhotoImage(img)
        self.delete("all")
        self.create_image(0, 0, image=self._img_ref, anchor="nw")


class GlassCheck(tk.Canvas):
    """自绘勾选框，绑定一个 ``tk.BooleanVar``，风格与 GlassButton 一致。

    只负责显示与切换变量；取值一律走外部传进来的 BooleanVar，
    所以业务代码不需要知道这是自绘控件。
    """

    BOX = 15          # 方框边长
    GAP = 7           # 方框与文字间距
    PAD = 1

    def __init__(self, parent, text: str = "", variable=None, command=None,
                 bg: str = PANEL_SOLID, height: int = 22, font_size: int = 9):
        self._text = text
        self._boolvar = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._bg = bg
        self._hover = False
        self._font_size = font_size
        self._img_ref = None

        probe = tk.Label(parent, text=text, font=(FONT_UI, font_size))
        probe.update_idletasks()
        tw = probe.winfo_reqwidth()
        probe.destroy()
        w = self.PAD * 2 + self.BOX + (self.GAP + tw if text else 0)
        super().__init__(parent, width=max(24, w), height=height, bg=bg,
                         bd=0, highlightthickness=0, cursor="hand2")

        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    # ---- 兼容 tk 的 configure/cget ----
    def configure(self, cnf=None, **kw):
        text = kw.pop("text", None)
        if text is not None:
            self._text = text
        if cnf:
            super().configure(cnf)
        if kw:
            super().configure(**kw)
        if text is not None:
            self._draw()
        return None

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)

    def variable(self):
        return self._boolvar

    # ---- 交互 ----
    def _toggle(self, _e=None):
        self._boolvar.set(not bool(self._boolvar.get()))
        self._draw()
        if self._command:
            self._command()

    def _enter(self, _e=None):
        self._hover = True
        self._draw()

    def _leave(self, _e=None):
        self._hover = False
        self._draw()

    # ---- 绘制 ----
    def _draw(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            w, h = int(self["width"]), int(self["height"])
        if w <= 1 or h <= 1:
            return

        S = 4
        W, H = w * S, h * S
        img = Image.new("RGBA", (W, H), hex2rgb(self._bg) + (255,))
        d = ImageDraw.Draw(img)

        b = self.BOX * S
        x0 = self.PAD * S
        y0 = (H - b) // 2
        sel = bool(self._boolvar.get())

        if sel:
            box = _rounded((b, b), 4 * S, hex2rgb(ACCENT) + (255,))
            img.paste(box, (x0, y0), box)
            d.line([(x0 + 3.6 * S, y0 + 7.6 * S),
                    (x0 + 6.1 * S, y0 + 10.3 * S),
                    (x0 + 11.4 * S, y0 + 4.5 * S)],
                   fill=(255, 255, 255, 255),
                   width=max(1, int(1.9 * S)), joint="curve")
        else:
            box = _rounded((b, b), 4 * S, hex2rgb(INSET) + (255,),
                           outline=hex2rgb(BORDER_HI if self._hover else BORDER) + (255,),
                           width=1)
            img.paste(box, (x0, y0), box)

        if self._text:
            col = INK if (sel or self._hover) else INK2
            font = load_font(self._font_size * S)
            bb = d.textbbox((0, 0), self._text, font=font)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
            d.text((x0 + b + self.GAP * S - bb[0], (H - th) / 2 - bb[1]),
                   self._text, font=font, fill=hex2rgb(col) + (255,))

        img = img.resize((w, h), Image.LANCZOS)
        self._img_ref = ImageTk.PhotoImage(img)
        self.delete("all")
        self.create_image(0, 0, image=self._img_ref, anchor="nw")


# --------------------------------------------------------------------------
# ttk 主题
# --------------------------------------------------------------------------
def apply_theme(root: tk.Tk, panel_bg: str = PANEL_SOLID, inset_bg: str = INSET):
    """把 ttk 控件统一成深色玻璃风格。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=BG0)
    root.option_add("*Font", (FONT_UI, 9))

    style.configure(".", background=panel_bg, foreground=INK,
                    fieldbackground=inset_bg, bordercolor=BORDER,
                    lightcolor=panel_bg, darkcolor=panel_bg,
                    troughcolor=inset_bg, focuscolor=ACCENT)

    style.configure("TFrame", background=panel_bg)
    style.configure("TLabel", background=panel_bg, foreground=INK)

    style.configure("Glass.TLabel", background=panel_bg, foreground=INK)
    style.configure("Title.TLabel", background=BG0, foreground=INK,
                    font=(FONT_UI, 16, "bold"))
    style.configure("Sub.TLabel", background=BG0, foreground=INK2,
                    font=(FONT_UI, 9))
    style.configure("Hint.TLabel", background=panel_bg, foreground=INK3,
                    font=(FONT_UI, 8))
    style.configure("HintBG.TLabel", background=BG0, foreground=INK3,
                    font=(FONT_UI, 8))
    style.configure("Value.TLabel", background=panel_bg, foreground=INK2,
                    font=(FONT_UI, 9))

    # 下拉框
    style.configure("TCombobox", fieldbackground=inset_bg, background=panel_bg,
                    foreground=INK, arrowcolor=INK2, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", inset_bg), ("disabled", "#0a0f18")],
              foreground=[("disabled", INK3)],
              bordercolor=[("focus", ACCENT), ("hover", ACCENT_LO)],
              arrowcolor=[("disabled", INK3)])
    root.option_add("*TCombobox*Listbox.background", "#141d2c")
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # 输入框 / 数字框
    style.configure("TSpinbox", fieldbackground=inset_bg, background=panel_bg,
                    foreground=INK, arrowcolor=INK2, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, insertcolor=INK,
                    padding=3)
    style.map("TSpinbox",
              fieldbackground=[("disabled", "#0a0f18")],
              foreground=[("disabled", INK3)],
              bordercolor=[("focus", ACCENT)])
    style.configure("TEntry", fieldbackground=inset_bg, foreground=INK,
                    bordercolor=BORDER, insertcolor=INK)
    style.map("TEntry", bordercolor=[("focus", ACCENT)])

    # 勾选框
    style.configure("TCheckbutton", background=panel_bg, foreground=INK2,
                    focuscolor=panel_bg, indicatorcolor=inset_bg)
    style.map("TCheckbutton",
              background=[("active", panel_bg)],
              foreground=[("active", INK), ("disabled", INK3)],
              indicatorcolor=[("selected", ACCENT), ("pressed", ACCENT_LO)])

    # 滑块
    style.configure("TScale", background=panel_bg, troughcolor="#0a1220",
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT,
                    gripcount=0)
    style.map("TScale", lightcolor=[("active", ACCENT_HI)],
              darkcolor=[("active", ACCENT_HI)])

    # 滚动条
    style.configure("Vertical.TScrollbar", background="#1d2739",
                    troughcolor=inset_bg, bordercolor=inset_bg,
                    arrowcolor=INK3, lightcolor="#1d2739", darkcolor="#1d2739",
                    width=10)
    style.map("Vertical.TScrollbar",
              background=[("active", "#2b3950"), ("pressed", ACCENT_LO)])

    # 进度条
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=inset_bg, bordercolor=inset_bg,
                    lightcolor=ACCENT_HI, darkcolor=ACCENT, thickness=6)

    style.configure("TSeparator", background=BORDER)
    return style


def style_text(widget: tk.Text, kind: str = "inset", font_size: int = 9):
    """统一内嵌 Text 控件的外观。"""
    bg = INSET if kind != "log" else "#080e18"
    fg = INK if kind != "log" else "#c7d6ea"
    widget.configure(bg=bg, fg=fg, insertbackground=ACCENT,
                     relief="flat", bd=0, highlightthickness=0,
                     selectbackground=ACCENT_LO, selectforeground="#ffffff",
                     padx=10, pady=8,
                     font=(FONT_MONO, font_size),
                     spacing1=1, spacing3=2)
    return widget


# --------------------------------------------------------------------------
# 原生窗口美化
# --------------------------------------------------------------------------
def polish_window(root: tk.Tk):
    """Win11：深色标题栏 + 圆角窗口。失败就静默跳过。"""
    try:
        from ctypes import byref, c_int, sizeof, windll
        root.update_idletasks()
        hwnd = windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        v = c_int(1)
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, byref(v), sizeof(v))
        v2 = c_int(2)          # DWMWCP_ROUND
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, byref(v2), sizeof(v2))
    except Exception:
        pass


class BackdropCanvas:
    """铺满窗口的背景画布。可选在背景上直接画标题（真正"透明"）。"""

    def __init__(self, root: tk.Tk, minsize=(1020, 820), header=()):
        self.root = root
        self.cv = tk.Canvas(root, bg=BG0, bd=0, highlightthickness=0)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.lower(self.cv)
        self._ref = None
        self._head_refs = []
        self._job = None
        self._minsize = minsize
        self._size = (0, 0)
        # header: [(text, x, y, px, color), ...]
        self.header = list(header)
        root.bind("<Configure>", self._on_conf)

    def _on_conf(self, evt):
        if evt.widget is not self.root:
            return
        size = (max(self._minsize[0], evt.width), max(self._minsize[1], evt.height))
        if size == self._size:
            return
        self._size = size
        if self._job:
            self.root.after_cancel(self._job)
        self._job = self.root.after(90, self._render)

    def _render(self):
        w, h = self._size
        if w < 8 or h < 8:
            return
        img = init_backdrop(w, h)
        self._ref = ImageTk.PhotoImage(img)
        self.cv.delete("all")
        self.cv.create_image(0, 0, image=self._ref, anchor="nw", tags="bg")
        # 标题直接画在背景图上 —— 没有底色，天然"透明"
        self._head_refs = []
        for text, hx, hy, px, color in self.header:
            shot = _text_img(text, FONT_UI, px, color)
            self._head_refs.append(ImageTk.PhotoImage(shot))
            self.cv.create_image(hx, hy, image=self._head_refs[-1],
                                 anchor="nw", tags="head")
        # 标题层贴着背景图，但要在背景图之上
        self.cv.tag_raise("head")
        tk.Misc.lower(self.cv)
