"""毛玻璃（glassmorphism）视觉工具箱 —— 纯 Qt（PySide6）+ Pillow 实现。

整体思路和旧 tkinter 版一致，分两层：

1. **底层**：整个窗口铺一张用 Pillow 生成的背景图 —— 深色渐变 + 几团
   高斯模糊的彩色光斑 + 轻微噪点，视觉上就是一块磨砂玻璃。
2. **卡片层**：每张卡片是一个 ``GlassCard``（``QWidget``）。它在
   ``paintEvent`` 里从背景图上**裁出自己那块区域**，再叠上「半透明圆角
   面板 + 1px 描边 + 顶部高光 + 投影/外发光」，于是卡片是真正"压在玻璃
   上"的，而不是贴了张灰底图。

与 tkinter 版的关键差别：Qt 原生支持逐像素 alpha，所以卡片内部的控件
（标签 / 下拉 / 输入框 / 文本框）都能做成**真透明**，玻璃感比 tkinter
版更"透"。

窗口用「无边框 + 全透明」实现，标题直接画在背景上（真正透明），右上角
放一个自绘关闭按钮，标题区可拖动移动窗口。
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

from PySide6.QtCore import (Qt, QEvent, QPoint, QRect, QRectF, QSize,
                            QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage,
                           QGuiApplication, QLinearGradient, QPen,
                           QPainter, QPixmap)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QPlainTextEdit,
                               QProgressBar, QPushButton, QSizePolicy,
                               QSlider, QSpinBox, QVBoxLayout, QWidget,
                               QMessageBox)

# --------------------------------------------------------------------------
# 调色板（与旧版保持一致）
# --------------------------------------------------------------------------
BG0 = "#070b12"          # 窗口最底色（背景图没铺满时露出）
PANEL = "#1a2540"        # 卡片基色（半透明叠加用）
PANEL_IN = "#151f35"     # 卡片内部实色 = PANEL 以 ~0.62 叠在背景上的结果
PANEL_SOLID = PANEL_IN   # 别名：子控件统一用它当底色
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

# 字体
FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# 字号（Qt 的 setPointSize 是磅值，随系统 DPI 缩放）。
# 集中在这里，改一处就能整体放大/缩小界面，不用满代码找字面量。
SIZE_BASE = 11        # 正文、控件、标签
SIZE_SMALL = 10       # 提示 / 说明行
SIZE_TITLE = 26       # 窗口大标题（原 38，用户反馈过大）
SIZE_SUB = 11         # 副标题
SIZE_CARD = 12        # 卡片标题
SIZE_BTN = 11         # 按钮
SIZE_CHECK = 11       # 勾选框
SIZE_MONO = 11        # 信息区 / 日志区（等宽）
CARD_TITLE_H = 26     # 卡片标题占的高度（跟着 SIZE_CARD 一起调）

# 卡片填充的竖向渐变 alpha（上淡下实）。
# 2026-10-04：从 216/142 降到 180/120，让背景光斑能透出来，
# 恢复"玻璃透明感"；同时保留 60 的上下差，渐变依旧可见。
CARD_ALPHA_TOP = 180
CARD_ALPHA_BOT = 120

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
def hex2rgb(s: str):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(t) -> str:
    return "#%02x%02x%02x" % (int(t[0]), int(t[1]), int(t[2]))


def _blend(rgb, overlay_rgba):
    """把 RGBA 叠到 RGB 上，返回 RGB（用于算"子控件该用什么实色"）。"""
    r, g, b = rgb[:3]
    orr, og, ob, oa = overlay_rgba
    a = oa / 255.0
    return (round(r * (1 - a) + orr * a),
            round(g * (1 - a) + og * a),
            round(b * (1 - a) + ob * a))


def font_ui(size: int = SIZE_BASE, bold: bool = False) -> QFont:
    f = QFont(FONT_UI, size)
    if bold:
        f.setBold(True)
    return f


def font_mono(size: int = SIZE_MONO) -> QFont:
    return QFont(FONT_MONO, size)


def _qcolor(hx: str, alpha: int = 255) -> QColor:
    r, g, b = hex2rgb(hx)
    return QColor(r, g, b, alpha)


def _load_font(px: int):
    """按像素字号加载一个 TrueType 字体；都失败就退回 PIL 默认字体。"""
    from PIL import ImageFont
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, int(px))
        except Exception:
            continue
    return ImageFont.load_default()


# --------------------------------------------------------------------------
# 背景（磨砂玻璃）
# --------------------------------------------------------------------------
def _pil_to_qimage(img: Image.Image) -> QImage:
    """PIL Image → QImage（深拷贝，脱离 Python 字节缓冲，避免 GC 后失效）。"""
    img = img.convert("RGB")
    w, h = img.size
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, w, h, w * 3, QImage.Format.Format_RGB888)
    return qimg.copy()


def build_backdrop(w: int, h: int) -> Image.Image:
    """生成整窗背景：深色渐变 + 模糊光斑 + 噪点。"""
    w, h = max(8, int(w)), max(8, int(h))
    img = Image.new("RGB", (w, h))
    px = img.load()
    top = hex2rgb("#131e3a")
    bot = hex2rgb(BG0)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)

    # 光斑：先在低分辨率上画再放大 + 高斯模糊，比直接在大图上糊快得多
    sw, sh = max(8, w // 10), max(8, h // 10)
    blobs = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    for cx, yf, rad, col in _BLOBS:
        c = int(cx * sw)
        cy = int(yf * sh)
        rr = max(2, int(rad * min(sw, sh)))
        bd.ellipse([c - rr, cy - rr, c + rr, cy + rr],
                   fill=hex2rgb(col) + (150,))
    blobs = blobs.filter(ImageFilter.GaussianBlur(max(1.0, sw * 0.04)))
    blobs = blobs.resize((w, h), Image.BICUBIC)
    img = Image.alpha_composite(img.convert("RGBA"), blobs).convert("RGB")

    # 竖向明暗：上亮、中间最暗、下再略亮，避免整片死黑
    shade = Image.new("L", (1, h))
    spx = shade.load()
    for y in range(h):
        t = y / max(1, h - 1)
        v = 232 - int(96 * abs(t - 0.42) / 0.58)
        spx[0, y] = v
    shade = shade.resize((w, h), Image.BILINEAR)
    img = Image.composite(img, Image.new("RGB", (w, h), (0, 0, 0)),
                           shade.point(lambda v: 255 - v))

    # 极轻噪点：std 降到 6（原 18），保留一丝磨砂质感但不出现明显颗粒。
    # 做法：把噪点缩放到 0..31 的窄带，作为"压暗蒙版"的抖动量，
    # 蒙版基线取 224（≈ 88% 原图 + 12% 黑），整体只压暗 ~12%，
    # 噪点再在这个基线上做 ±15 的抖动，形成极细的明暗颗粒。
    noise = Image.effect_noise((w, h), 6).convert("L")
    veil = Image.new("RGB", (w, h), (150, 170, 205))
    img = Image.blend(img, veil, 0.03)
    nmask = noise.point(lambda v: max(0, min(255, 224 + (v - 128) // 4)))
    img = Image.composite(img, Image.new("RGB", (w, h), (0, 0, 0)),
                           nmask)
    return img


_BACKDROP: QPixmap | None = None
_BACKDROP_SIZE = (0, 0)


def init_backdrop(w: int, h: int) -> QPixmap:
    """按窗口尺寸生成（并缓存）背景 QPixmap。"""
    global _BACKDROP, _BACKDROP_SIZE
    if _BACKDROP is not None and _BACKDROP_SIZE == (w, h):
        return _BACKDROP
    img = build_backdrop(w, h)
    _BACKDROP = QPixmap.fromImage(_pil_to_qimage(img))
    _BACKDROP_SIZE = (w, h)
    return _BACKDROP


def backdrop() -> QPixmap | None:
    return _BACKDROP


def window_image() -> QPixmap | None:
    """兼容旧接口名。"""
    return _BACKDROP


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------
class GlassCard(QWidget):
    """一块"压在玻璃上"的圆角卡片。

    ``body`` 是卡片内部的透明容器，往里加控件即可；``body_lay`` 是它的
    纵向布局。卡片标题（可选）画在玻璃上，真正透明。
    """

    SHADOW = 7

    def __init__(self, parent: QWidget | None = None, title: str = "",
                 glow: str = ACCENT, radius: int = 14,
                 pad: tuple = (16, 12, 16, 14)):
        super().__init__(parent)
        self._title = title
        self._glow = glow
        self._radius = radius
        self._pad = pad
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 内部透明容器
        self.body = QWidget(self)
        self.body.setAttribute(Qt.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.SHADOW, self.SHADOW,
                                 self.SHADOW, self.SHADOW)
        outer.setSpacing(0)
        outer.addWidget(self.body)

        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setSpacing(8)
        if title:
            self.body_lay.setContentsMargins(
                pad[0], pad[1] + CARD_TITLE_H, pad[2], pad[3])
        else:
            self.body_lay.setContentsMargins(*pad)

        self.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self.update()
        return super().eventFilter(obj, ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w < 2 or h < 2:
            p.end()
            return
        SH = self.SHADOW
        pw, ph = w - 2 * SH, h - 2 * SH

        # 1) 裁出背景对应区域
        win = self.window()
        pos = self.mapTo(win, QPoint(0, 0))
        x, y = pos.x(), pos.y()
        bd = backdrop()
        if (bd is not None and not bd.isNull() and x >= 0 and y >= 0
                and x + w <= bd.width() and y + h <= bd.height()):
            p.drawPixmap(0, 0, bd, x, y, w, h)
        else:
            p.fillRect(self.rect(), _qcolor(BG0))

        if pw > 0 and ph > 0:
            # 2) 投影（先画，垫在面板下面）
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qcolor("#000000", 110))
            p.drawRoundedRect(SH, SH + 2, pw, ph, self._radius, self._radius)
            # 外发光
            if self._glow:
                p.setBrush(_qcolor(self._glow, 46))
                p.drawRoundedRect(SH - 1, SH, pw + 2, ph,
                                  self._radius + 1, self._radius + 1)
            # 3) 面板本体：竖向微渐变，读起来才像玻璃
            grad = QLinearGradient(0, 0, 0, ph)
            c_top = _qcolor(PANEL, CARD_ALPHA_TOP)
            c_bot = _qcolor(PANEL, CARD_ALPHA_BOT)
            grad.setColorAt(0.0, c_top)
            grad.setColorAt(1.0, c_bot)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(SH, SH, pw, ph, self._radius, self._radius)
            # 4) 1px 描边 + 顶部高光
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qcolor(BORDER, 190), 1))
            p.drawRoundedRect(SH + 0.5, SH + 0.5, pw - 1, ph - 1,
                              self._radius, self._radius)
            r = max(2, self._radius)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.drawLine(SH + r, SH + 1, SH + pw - r, SH + 1)
            p.setPen(QPen(QColor(255, 255, 255, 24), 1))
            p.drawLine(SH + 1, SH + r, SH + 1, SH + ph - r)

        # 5) 标题（画在玻璃上，真正透明）
        if self._title:
            gx, gy = SH + 15, SH + 12
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qcolor(self._glow or ACCENT))
            p.drawRoundedRect(gx, gy + 1, 4, 15, 2, 2)
            p.setFont(font_ui(SIZE_CARD))
            p.setPen(_qcolor(INK2))
            p.drawText(QRectF(gx + 12, gy - 2, 320, 22),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._title)
        p.end()


# --------------------------------------------------------------------------
# 按钮
# --------------------------------------------------------------------------
class GlassButton(QPushButton):
    """自绘玻璃按钮：normal / hover / pressed / disabled 四态。"""

    def __init__(self, text: str = "", parent: QWidget | None = None,
                 kind: str = "primary", height: int = 34,
                 font_size: int = SIZE_BTN):
        super().__init__(text, parent)
        self._kind = kind
        self._height = height
        self._font_size = font_size
        self._hover = False
        self._pressed = False
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._apply_font()

    def _apply_font(self):
        self.setFont(font_ui(self._font_size,
                             bold=(self._kind == "primary")))

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._pressed = False
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._pressed = True
            self.update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._pressed:
            self._pressed = False
            self.update()
        super().mouseReleaseEvent(ev)

    def _colors(self):
        if not self.isEnabled():
            return ("#1a2331", "#26313f", "#54637a")
        if self._kind == "primary":
            top = ACCENT_HI if self._hover else ACCENT
            bot = ACCENT_LO
            if self._pressed:
                top, bot = ACCENT_LO, ACCENT_LO
            return (top, bot, "#ffffff")
        if self._kind == "ghost":
            return ("#1a2537" if self._hover else "#151d2b",
                    "#151d2b", INK2)
        return ("#23304a" if self._hover else "#1b2433",
                "#161e2b", INK)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        top, bot, fg = self._colors()
        r = max(6, h // 2 - 2)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, _qcolor(top))
        grad.setColorAt(1.0, _qcolor(bot))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(0.5, 0.5, w - 1, h - 1, r, r)
        # 1px 内描边 + 顶部高光
        if self.isEnabled():
            p.setPen(QPen(QColor(255, 255, 255, 44), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(0.5, 0.5, w - 1, h - 1, r, r)
            p.setPen(QPen(QColor(255, 255, 255, 52), 1))
            p.drawLine(r, 1, w - r, 1)
        # 文字
        p.setFont(font_ui(self._font_size, bold=(self._kind == "primary")))
        p.setPen(_qcolor(fg))
        p.drawText(self.rect(),
                   Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


# --------------------------------------------------------------------------
# 勾选框
# --------------------------------------------------------------------------
class GlassCheck(QCheckBox):
    """自绘勾选框：17px 圆角方框 + 对勾，右侧跟文字。"""

    BOX = 17
    GAP = 8
    PAD = 1

    def __init__(self, text: str = "", parent: QWidget | None = None,
                 font_size: int = SIZE_CHECK):
        super().__init__(text, parent)
        self._font_size = font_size
        self._hover = False
        # 部分 PySide6 版本没暴露 setIndicatorSize；自绘框不依赖它，
        # 缺了就跳过（宽度由下面的 sizeHint 兜底）。
        try:
            self.setIndicatorSize(QSize(self.BOX, self.BOX))
        except AttributeError:
            pass
        self.setFixedHeight(26)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_font()

    def _apply_font(self):
        self.setFont(font_ui(self._font_size))

    def sizeHint(self):
        """自绘框的宽度 = 左内边 + 方框 + 间隔 + 文字 + 右内边。"""
        fm = QFontMetrics(self.font())
        tw = fm.horizontalAdvance(self.text()) if self.text() else 0
        w = self.PAD + self.BOX + self.GAP + tw + 4
        return QSize(max(w, self.BOX + 8), 26)

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        b = self.BOX
        x0 = self.PAD
        y0 = (h - b) // 2
        sel = self.isChecked()

        if sel:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qcolor(ACCENT))
            p.drawRoundedRect(x0, y0, b, b, 4, 4)
            p.setPen(QPen(QColor(255, 255, 255, 255), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolyline([QPoint(x0 + 4, y0 + 9),
                            QPoint(x0 + 7, y0 + 12),
                            QPoint(x0 + 13, y0 + 5)])
        else:
            p.setPen(QPen(_qcolor(BORDER_HI if self._hover else BORDER), 1))
            p.setBrush(_qcolor(INSET))
            p.drawRoundedRect(x0, y0, b, b, 4, 4)

        if self.text():
            col = INK if (sel or self._hover) else INK2
            p.setFont(font_ui(self._font_size))
            p.setPen(_qcolor(col))
            tx = x0 + b + self.GAP
            p.drawText(QRectF(tx, 0, w - tx, h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self.text())
        p.end()


# --------------------------------------------------------------------------
# 下拉框
# --------------------------------------------------------------------------
class GlassCombo(QComboBox):
    """玻璃风格下拉框（只读选择用）。"""

    def __init__(self, parent: QWidget | None = None,
                 font_size: int = SIZE_BASE):
        super().__init__(parent)
        self._font_size = font_size
        self.setFont(font_ui(font_size))
        self.setFixedHeight(30)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(self._sheet())

    def _sheet(self):
        return f"""
        QComboBox {{
            background-color: {INSET};
            color: {INK};
            border: 1px solid {BORDER};
            border-radius: 8px;
            padding: 2px 10px 2px 10px;
            selection-background-color: {ACCENT};
        }}
        QComboBox:disabled {{ color: {INK3}; }}
        QComboBox::drop-down {{
            border: none;
            width: 20px;
        }}
        QComboBox::down-arrow {{
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 6px solid {INK2};
            margin-right: 6px;
        }}
        QComboBox QAbstractItemView {{
            background-color: #141d2c;
            color: {INK};
            border: 1px solid {BORDER};
            selection-background-color: {ACCENT};
            selection-color: #ffffff;
            outline: 0;
        }}
        """

    def set_values(self, values):
        self.blockSignals(True)
        self.clear()
        for v in values:
            self.addItem(v)
        self.blockSignals(False)


# --------------------------------------------------------------------------
# 数字输入框
# --------------------------------------------------------------------------
class GlassSpin(QSpinBox):
    """玻璃风格数字输入框。"""

    def __init__(self, parent: QWidget | None = None,
                 font_size: int = SIZE_BASE):
        super().__init__(parent)
        self.setFont(font_ui(font_size))
        self.setFixedHeight(28)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(f"""
            QSpinBox {{
                background-color: {INSET};
                color: {INK};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 1px 6px;
            }}
            QSpinBox:disabled {{ color: {INK3}; background-color: #0a0f18; }}
            QSpinBox::up-button, QSpinBox::down-button {{
                border: none; width: 14px; background: transparent;
            }}
            QSpinBox::up-arrow {{
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-bottom: 5px solid {INK2};
            }}
            QSpinBox::down-arrow {{
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {INK2};
            }}
        """)


# --------------------------------------------------------------------------
# 滑杆
# --------------------------------------------------------------------------
class GlassSlider(QSlider):
    """玻璃风格滑杆（横向）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setFixedHeight(24)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: #0a1220;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {ACCENT};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 14px;
                height: 14px;
                margin: -5px 0 -5px -5px;
                border-radius: 7px;
                background: {ACCENT_HI};
                border: 1px solid {ACCENT_LO};
            }}
            QSlider::handle:horizontal:hover {{
                background: #ffffff;
            }}
        """)


# --------------------------------------------------------------------------
# 文本区（信息 / 日志）
# --------------------------------------------------------------------------
class GlassText(QPlainTextEdit):
    """透明底文本区，玻璃感更透。"""

    def __init__(self, parent: QWidget | None = None, kind: str = "inset",
                 font_size: int = SIZE_MONO):
        super().__init__(parent)
        self._kind = kind
        mono = kind != "inset"
        self.setFont(font_mono(font_size) if mono else font_ui(font_size))
        self.setFrameStyle(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setAttribute(Qt.WA_TranslucentBackground)
        bg = INSET if kind != "log" else "#080e18"
        fg = INK if kind != "log" else "#c7d6ea"
        self.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {bg};
                color: {fg};
                border: 1px solid {BORDER};
                border-radius: 10px;
                padding: 8px 10px;
                selection-background-color: {ACCENT_LO};
                selection-color: #ffffff;
            }}
            QScrollBar:vertical {{
                background: {INSET}; width: 10px;
                border-radius: 5px; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: #1d2739; border-radius: 5px; min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{ background: #2b3950; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)


# --------------------------------------------------------------------------
# 进度条（忙碌指示）
# --------------------------------------------------------------------------
class GlassProgress(QProgressBar):
    """玻璃风格进度条；set_busy(True) 进入不定长忙碌动画。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self.setTextVisible(False)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(f"""
            QProgressBar {{
                background: {INSET};
                border: none;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {ACCENT};
                border-radius: 4px;
            }}
        """)

    def set_busy(self, busy: bool):
        if busy:
            self.setRange(0, 0)
        else:
            self.setRange(0, 100)
            self.setValue(0)


# --------------------------------------------------------------------------
# 背景画布（铺满窗口 + 可选标题）
# --------------------------------------------------------------------------
class BackgroundWidget(QWidget):
    """铺满窗口的背景。可选在背景上直接画标题（真正"透明"）。

    ``header``: ``[(text, x, y, px, color), ...]``，px 是像素字号。
    """

    def __init__(self, parent: QWidget | None = None,
                 minsize: tuple = (1020, 820), header=()):
        super().__init__(parent)
        self._minsize = minsize
        self.header = list(header)
        self._size = (0, 0)
        self._job: QTimer | None = None
        self._pixmap: QPixmap | None = None
        self.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize:
            self._schedule_render()
        return super().eventFilter(obj, ev)

    def _schedule_render(self):
        if self._job is not None:
            self._job.stop()
        self._job = QTimer(self)
        self._job.setSingleShot(True)
        self._job.timeout.connect(self._render)
        self._job.start(90)

    def _render(self):
        w, h = self.width(), self.height()
        w = max(self._minsize[0], w)
        h = max(self._minsize[1], h)
        if w < 8 or h < 8:
            return
        img = build_backdrop(w, h)
        # 标题直接画在背景图上 —— 没有底色，天然"透明"
        d = ImageDraw.Draw(img)
        for text, hx, hy, px, color in self.header:
            try:
                d.text((hx, hy), text, font=_load_font(px),
                       fill=hex2rgb(color))
            except Exception:
                pass
        self._pixmap = QPixmap.fromImage(_pil_to_qimage(img))
        # 同步全局背景缓存，让卡片裁切用同一张
        init_backdrop(w, h)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        if self._pixmap is not None and not self._pixmap.isNull():
            p.drawPixmap(0, 0, self._pixmap)
        else:
            p.fillRect(self.rect(), _qcolor(BG0))
        p.end()


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
def _restore_foreground(owner) -> None:
    """把 owner 拉回前台：同步抢一次，再延后补一次。

    顶层无边框对话框（例如裁剪框）关闭后，主窗口**不会**自动拿回前台。实测
    关闭瞬间 QApplication.activeWindow() 直接变成 None、主窗口
    isActiveWindow() 为 False，此后所有点击都落到别的程序上——桌面上常开着
    资源管理器，用户看到的就是"点确定后焦点跳到文件管理器、然后卡死"（程序其实
    还活着、事件循环照常跑，所以看门狗抓不到）。

    必须同步先抢：等 Windows 把前台交给别的进程之后再调 SetForegroundWindow，
    非前台进程的请求会被系统直接拒绝。
    """
    def bump():
        try:
            owner.raise_()
            owner.activateWindow()
        except Exception:
            pass
    bump()
    try:
        QTimer.singleShot(0, bump)
    except Exception:
        pass


class GlassWindow(QWidget):
    """无边框 + 全透明主窗口。

    ``content_lay`` 是放卡片的纵向布局；标题画在背景上，右上角自绘关闭
    按钮，标题区可拖动移动窗口。
    """

    # 窗口关闭时发出。GlassWindow 继承的是 QWidget（不是 QDialog），**没有**
    # QDialog 的 finished 信号；需要"等它关掉再继续"的调用方（例如裁剪对话框）
    # 必须连这个信号，否则会抛 AttributeError（打包版会因此直接闪退）。
    closed = Signal()

    def __init__(self, title: str = "", subtitle: str = "",
                 minsize: tuple = (1200, 900)):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(title)
        self._minsize = minsize
        self._drag_pos = None

        # 背景（含标题）
        self.bg = BackgroundWidget(self, minsize=minsize, header=[
            (title, 28, 16, SIZE_TITLE, INK),
            (subtitle, 28, 60, 22, INK2),
        ])
        # 内容容器（透明，放卡片）
        self.content = QWidget(self)
        self.content.setAttribute(Qt.WA_TranslucentBackground)
        self.content_lay = QVBoxLayout(self.content)
        self.content_lay.setContentsMargins(20, 88, 20, 18)
        self.content_lay.setSpacing(20)

        # 关闭按钮
        self.btn_close = GlassButton("✕", self, kind="ghost", height=30,
                                     font_size=SIZE_BASE)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)

        # 背景与内容都铺满窗口、互相重叠（内容在背景之上）；
        # 不用布局管理器（那会把两者上下堆叠），而是在 resizeEvent 里
        # 手动把两者的几何设为窗口矩形。
        self.content.raise_()

        # 标题区拖动：监听 content 的鼠标事件，只有点在 content 本体
        # （空白处）才触发拖动，点在卡片上则归卡片自己处理
        self.content.installEventFilter(self)

        self.resizeEvent(None)

    def resizeEvent(self, ev):
        self.bg.setGeometry(self.rect())
        self.content.setGeometry(self.rect())
        # 关闭按钮贴右上角
        bw, bh = 34, 30
        self.btn_close.setGeometry(self.width() - bw - 14, 12, bw, bh)
        self.btn_close.raise_()
        if ev is not None:
            super().resizeEvent(ev)

    def eventFilter(self, obj, ev):
        if obj is self.content and ev.type() == QEvent.Type.MouseButtonPress \
                and ev.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (ev.globalPosition().toPoint()
                              - self.frameGeometry().topLeft())
        elif obj is self.content and ev.type() == QEvent.Type.MouseButtonDblClick \
                and ev.button() == Qt.MouseButton.LeftButton:
            # 双击标题区：最大化/还原
            if self.isMaximized():
                self.showNormal()
            else:
                self.showMaximized()
        return super().eventFilter(obj, ev)

    def mouseMoveEvent(self, ev):
        if self._drag_pos is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_pos = None
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape and not self.isMaximized():
            self.close()
        super().keyPressEvent(ev)

    def closeEvent(self, ev):
        super().closeEvent(ev)
        self.closed.emit()
        # 只有"被当成对话框用的"顶层窗口才有父窗口；主窗口自身 parentWidget()
        # 为 None，不会走到这里。见 _restore_foreground 的说明。
        owner = self.parentWidget()
        if owner is not None:
            _restore_foreground(owner)


# --------------------------------------------------------------------------
# 原生窗口美化（保留给"带原生标题栏"的场景；无边框窗口用不到）
# --------------------------------------------------------------------------
def polish_window(win: QWidget):
    """Win11：深色标题栏 + 圆角窗口。失败就静默跳过。"""
    try:
        import sys
        from ctypes import byref, c_int, sizeof, windll
        if sys.platform != "win32":
            return
        win.update()
        hwnd = int(win.winId())
        hwnd = windll.user32.GetParent(hwnd) or hwnd
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
