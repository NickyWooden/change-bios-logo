# -*- coding: utf-8 -*-
"""
change-bios-logo 核心库
=======================

在 AMI / Gigabyte UEFI 固件（32MB F44d 之类的镜像）中定位开机 Logo，
并在 **完全不动其它任何字节** 的前提下做等长替换。

原理
----
开机图是标准 UEFI Logo（GUID ``7BB28B99-61BB-11D5-9A5D-0090273FC14D``），
存放在 Freeform 文件 ``MyOemLogo1`` 里，外层套一个 **GUID 定义段（GUID_DEFINED,
type=0x02）**，压缩算法是 **Raw LZMA1**（GUID ``EE4E5898-3914-4259-9D6E-DC7BD79403CF``）。

段结构（实测 Gigabyte B650E AORUS ELITE X ICE, B650EAELITEXICE.F44d）::

    0x010002B0  EFI_COMMON_SECTION_HEADER   Size=0x6808  Type=0x02 (GUID_DEFINED)
    0x010002B4  SectionDefinitionGuid       EE4E5898-3914-4259-9D6E-DC7BD79403CF (LZMA)
    0x010002C4  DataOffset=0x0018  Attributes=0x0001
    0x010002C8  LZMA 头  props=0x5D  dictSize=0x01000000  uncompressedSize=352082
    0x010002D5  压缩流本体  ……  到 段尾 0x010006AB8

替换时只重写 ``[stream_off, section_end)`` 这一段区间内的字节，
**段头、FFS 头、FV 头、后续所有内容一律原样保留**，文件总长不变，
因此不需要重算任何校验和，也不改变固件的代码部分。
"""

from __future__ import annotations

import io
import lzma
import struct
from dataclasses import dataclass, field
from typing import Iterable, Optional

__all__ = [
    "LOGO_GUID", "LZMA_GUID", "GUID_DEFINED", "SECTION_FREEFORM_SUBTYPE_GUID",
    "LogoSlot", "BmpInfo", "BiosLogoError", "parse_bios", "scan", "replace",
    "guid_to_bytes", "bytes_to_guid",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
LOGO_GUID = "7BB28B99-61BB-11D5-9A5D-0090273FC14D"      # EFI_SECTION / Logo
LZMA_GUID = "EE4E5898-3914-4259-9D6E-DC7BD79403CF"      # EDK2 LZMA 自定义压缩

SECTION_GUID_DEFINED = 0x02
SECTION_FREEFORM_SUBTYPE_GUID = 0x18

FRIENDLY_NAMES = {
    LOGO_GUID: "开机 Logo (UEFI BMP Logo)",
    LZMA_GUID: "LZMA 压缩段 (EDK2)",
    "86EE84E1-3375-41A1-AFBA-847BD29663AA": "MyOemLogo1",
    "86EE84E2-3375-41A1-AFBA-847BD29663AA": "MyOemLogo2",
    "4C91B810-A28D-4BBC-BDF0-30A9C6C7EEC2": "OemLOGO",
}


class BiosLogoError(Exception):
    """可预期的用户级错误。"""


# --------------------------------------------------------------------------
# GUID 工具（UEFI 混合字节序：前 3 段小端，后 2 段原样）
# --------------------------------------------------------------------------
def guid_to_bytes(text: str) -> bytes:
    h = text.replace("{", "").replace("}", "").replace("-", "").strip()
    if len(h) != 32:
        raise ValueError(f"GUID 长度错误: {text}")
    raw = bytes.fromhex(h)
    return (raw[0:4][::-1] + raw[4:6][::-1] + raw[6:8][::-1] + raw[8:16])


def bytes_to_guid(raw: bytes) -> str:
    p = raw[0:4][::-1] + raw[4:6][::-1] + raw[6:8][::-1] + raw[8:16]
    h = p.hex().upper()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# --------------------------------------------------------------------------
# LZMA 参数
# --------------------------------------------------------------------------
def _lzma_filters(props: int, dict_size: int) -> list:
    """EDK2 的 LZMA 属性字节还原成 Python 的 filter 描述。"""
    if not 0 <= props <= 224:
        raise BiosLogoError(f"LZMA 属性字节非法: 0x{props:02X}")
    lc = props % 9
    rest = props // 9
    lp = rest % 5
    pb = rest // 5
    return [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
             "lc": lc, "lp": lp, "pb": pb}]


def _parse_compressed_section(data: bytes, section_off: int) -> tuple:
    """解析 GUID_DEFINED(LZMA) 段头，返回 (section_size, section_end, data_start, props, dict_size, usize, stream_off)。"""
    if section_off + 24 > len(data):
        raise BiosLogoError("段头超出文件范围")
    size = int.from_bytes(data[section_off:section_off + 3], "little")
    stype = data[section_off + 3]
    if stype != SECTION_GUID_DEFINED:
        raise BiosLogoError(f"不是 GUID_DEFINED 段 (type=0x{stype:02X})")
    if size < 24:
        raise BiosLogoError(f"段长度异常: 0x{size:X}")
    sec_end = section_off + size
    if sec_end > len(data):
        raise BiosLogoError("段尾超出文件范围")

    guid = bytes_to_guid(data[section_off + 4:section_off + 20])
    if guid != LZMA_GUID:
        raise BiosLogoError(f"压缩算法不是 LZMA，而是 {guid}")

    data_off = struct.unpack_from("<H", data, section_off + 20)[0]
    attributes = struct.unpack_from("<H", data, section_off + 22)[0]
    data_start = section_off + data_off
    if data_start + 13 > sec_end:
        raise BiosLogoError("压缩数据起点越界")

    props = data[data_start]
    dict_size = struct.unpack_from("<I", data, data_start + 1)[0]
    usize = struct.unpack_from("<Q", data, data_start + 5)[0]
    stream_off = data_start + 13

    if dict_size == 0 or dict_size > (1 << 30):
        raise BiosLogoError(f"LZMA 字典大小异常: 0x{dict_size:X}")
    if not (0 < usize <= (1 << 28)):
        raise BiosLogoError(f"解压后长度异常: {usize}")

    return size, sec_end, data_start, props, dict_size, usize, stream_off, attributes


# --------------------------------------------------------------------------
# BMP 结构
# --------------------------------------------------------------------------
@dataclass
class BmpInfo:
    width: int
    height: int          # 正数 = 自下而上（BMP 常规），负数 = 自上而下
    bpp: int
    pix_off: int         # 像素数组在 BMP 内的偏移
    row_stride: int      # 每行字节数（含 4 字节对齐填充）
    compr: int
    header: bytes        # 原始 BMP 头（含调色板），逐字节保留
    pixels: bytes
    file_size: int       # BMP 头里声明的文件大小

    @property
    def rows(self) -> int:
        return abs(self.height)

    @property
    def top_down(self) -> bool:
        return self.height < 0

    def describe(self) -> str:
        return (f"{self.width}x{self.rows} {self.bpp}bpp, "
                f"行距 {self.row_stride}, 像素偏移 0x{self.pix_off:X}, "
                f"{len(self.pixels)} 字节")


def _palette_rgb(info: "BmpInfo"):
    """从 BMP 头里取出调色板，返回 PIL 需要的 RGB 列表（长度 3×N）。"""
    dib = struct.unpack_from("<I", info.header, 14)[0]
    pal_off = 14 + dib
    n = 1 << info.bpp
    pal = info.header[pal_off:pal_off + n * 4]
    if len(pal) < n * 4:
        raise BiosLogoError("BMP 调色板数据不足")
    out = []
    for i in range(n):
        b, g, r = pal[i * 4], pal[i * 4 + 1], pal[i * 4 + 2]
        out += [r, g, b]
    return out


def _expand_indices(raw: bytes, info: "BmpInfo") -> bytes:
    """把 1/4/8bpp 的打包像素展开成「每像素一字节」的索引。

    BMP 每行都按 4 字节对齐补零，所以不能直接把 ``raw`` 当成紧凑数组用 ——
    必须逐行去掉行尾填充，否则整张图会错位。
    """
    import numpy as np

    w, rows, stride, bpp = info.width, info.rows, info.row_stride, info.bpp
    a = np.frombuffer(raw, dtype=np.uint8)[:stride * rows].reshape(rows, stride)
    if bpp == 8:
        a = a[:, :w]
    elif bpp == 4:
        half = (w + 1) // 2
        out = np.empty((rows, w), dtype=np.uint8)
        out[:, 0::2] = a[:, :half] >> 4
        if w > 1:
            out[:, 1::2] = a[:, :w // 2] & 0x0F
        a = out
    else:                                                # 1bpp
        a = np.unpackbits(a, axis=1)[:, :w]
    return np.ascontiguousarray(a).tobytes()


def parse_bmp(bmp: bytes) -> BmpInfo:
    if len(bmp) < 54 or bmp[:2] != b"BM":
        raise BiosLogoError("不是合法的 BMP（缺少 BM 标识）")
    file_size = struct.unpack_from("<I", bmp, 2)[0]
    pix_off = struct.unpack_from("<I", bmp, 10)[0]
    dib_size = struct.unpack_from("<I", bmp, 14)[0]
    width = struct.unpack_from("<i", bmp, 18)[0]
    height = struct.unpack_from("<i", bmp, 22)[0]
    bpp = struct.unpack_from("<H", bmp, 28)[0]
    compr = struct.unpack_from("<I", bmp, 30)[0]

    if dib_size < 40:
        raise BiosLogoError(f"不支持的 BMP 头大小: {dib_size}（仅支持 BITMAPINFOHEADER 及以后）")
    if width <= 0 or height == 0:
        raise BiosLogoError(f"BMP 尺寸异常: {width}x{height}")
    if compr != 0:
        raise BiosLogoError(f"不支持的 BMP 压缩方式: {compr}（仅支持 BI_RGB）")
    if bpp not in (1, 4, 8, 24, 32):
        raise BiosLogoError(f"暂不支持的位深: {bpp}bpp（当前支持 1 / 4 / 8 / 24 / 32）")
    if pix_off >= len(bmp):
        raise BiosLogoError("BMP 像素偏移越界")

    row_stride = ((width * bpp + 31) // 32) * 4
    rows = abs(height)
    need = row_stride * rows
    pixels = bmp[pix_off:pix_off + need]
    if len(pixels) < need:
        raise BiosLogoError(f"BMP 像素数据不足: {len(pixels)} < {need}")

    return BmpInfo(width=width, height=height, bpp=bpp, pix_off=pix_off,
                   row_stride=row_stride, compr=compr,
                   header=bmp[:pix_off], pixels=pixels,
                   file_size=file_size if file_size else len(bmp))


# --------------------------------------------------------------------------
# 槽位
# --------------------------------------------------------------------------
@dataclass
class LogoSlot:
    """固件里的一处 Logo。"""
    logo_guid_off: int       # Logo GUID 在文件中的偏移
    section_off: int         # 外层 GUID_DEFINED 段头偏移
    section_size: int
    section_end: int
    stream_off: int          # 压缩流起点（LZMA 13 字节头之后）
    stream_budget: int       # 压缩流可用字节数 = section_end - stream_off
    data_start: int
    props: int
    dict_size: int
    usize: int
    plaintext: bytes
    bmp_off: int             # BMP 在明文中的偏移
    bmp: bytes
    bmp_info: BmpInfo
    attributes: int = 0
    index: int = 0

    # ---- 展示用 ----
    @property
    def name(self) -> str:
        return FRIENDLY_NAMES.get(LOGO_GUID, "开机 Logo")

    def path_text(self) -> str:
        return (f"Logo GUID  : {LOGO_GUID}  (文件偏移 0x{self.logo_guid_off:08X})\n"
                f"所在容器   : LZMA 压缩段 @ 0x{self.section_off:08X}, "
                f"长度 0x{self.section_size:X} ({self.section_size} 字节)\n"
                f"压缩流区间 : 0x{self.stream_off:08X} .. 0x{self.section_end:08X} "
                f"({self.stream_budget} 字节可用)\n"
                f"压缩算法   : LZMA1  props=0x{self.props:02X}  dict=0x{self.dict_size:X}  "
                f"attributes=0x{self.attributes:04X}\n"
                f"解压后大小 : {self.usize} 字节\n"
                f"图标位置   : 明文偏移 +{self.bmp_off}\n"
                f"图像规格   : {self.bmp_info.describe()}")

    def to_image(self):
        """返回 Pillow Image（RGB）。"""
        return bmp_to_image(self.bmp_info)


def bmp_to_image(info: "BmpInfo"):
    """把 BmpInfo 的像素还原成 Pillow Image（RGB）。"""
    from PIL import Image
    raw = info.pixels
    if info.bpp == 24:
        img = Image.frombytes("RGB", (info.width, info.rows), raw,
                              "raw", "BGR", info.row_stride, 1)
    elif info.bpp == 32:
        img = Image.frombytes("RGBA", (info.width, info.rows), raw,
                              "raw", "BGRA", info.row_stride, 1).convert("RGB")
    elif info.bpp in (1, 4, 8):
        img = Image.frombytes("P", (info.width, info.rows),
                              _expand_indices(raw, info),
                              "raw", "P", info.width, 1)
        img.putpalette(_palette_rgb(info))
        img = img.convert("RGB")
    else:                                                # pragma: no cover
        raise BiosLogoError(f"暂不支持的位深: {info.bpp}bpp")
    if not info.top_down:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    return img


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
def _try_build_slot(data: bytes, logo_off: int, index: int) -> Optional[LogoSlot]:
    """从 Logo GUID 出发，向前后寻找承载它的 LZMA 段并试解压。"""
    lzma_pat = guid_to_bytes(LZMA_GUID)

    # 1) 同一段内：Logo GUID 之后 96 字节内出现 LZMA GUID
    for delta in range(0, 96):
        p = logo_off + delta
        if p + 16 > len(data):
            break
        if data[p:p + 16] != lzma_pat:
            continue
        # GUID 位于 EFI_COMMON_SECTION_HEADER(4 字节) 之后
        section_off = p - 4
        if section_off < 0:
            continue
        try:
            (size, sec_end, data_start, props, dict_size, usize,
             stream_off, attributes) = _parse_compressed_section(data, section_off)
        except BiosLogoError:
            continue

        budget = sec_end - stream_off
        if budget <= 0:
            continue
        try:
            plain = lzma.LZMADecompressor(
                format=lzma.FORMAT_RAW,
                filters=_lzma_filters(props, dict_size),
            ).decompress(data[stream_off:sec_end], max_length=usize)
        except lzma.LZMAError:
            continue
        if len(plain) != usize:
            continue

        bmp_off = plain.find(b"BM")
        if bmp_off < 0 or bmp_off > 64:
            continue
        try:
            info = parse_bmp(plain[bmp_off:])
        except BiosLogoError:
            continue

        bmp_end = bmp_off + max(info.file_size, info.pix_off + len(info.pixels))
        bmp = plain[bmp_off:bmp_end]
        return LogoSlot(
            logo_guid_off=logo_off, section_off=section_off, section_size=size,
            section_end=sec_end, stream_off=stream_off, stream_budget=budget,
            data_start=data_start, props=props, dict_size=dict_size, usize=usize,
            plaintext=plain, bmp_off=bmp_off, bmp=bmp, bmp_info=info,
            attributes=attributes, index=index)

    # 2) 回退：Logo GUID 前面的段头（未压缩 freeform 段）
    sec = logo_off - 4
    if sec >= 0:
        size = int.from_bytes(data[sec:sec + 3], "little")
        stype = data[sec + 3]
        if stype == SECTION_FREEFORM_SUBTYPE_GUID and 24 < size <= len(data) - sec:
            payload = data[sec + 24:sec + size]
            bmp_off = payload.find(b"BM")
            if 0 <= bmp_off <= 64:
                try:
                    info = parse_bmp(payload[bmp_off:])
                    bmp_end = bmp_off + max(info.file_size,
                                            info.pix_off + len(info.pixels))
                    return LogoSlot(
                        logo_guid_off=logo_off, section_off=sec, section_size=size,
                        section_end=sec + size, stream_off=sec + 24,
                        stream_budget=size - 24, data_start=sec + 24,
                        props=0, dict_size=0, usize=len(payload),
                        plaintext=payload, bmp_off=bmp_off,
                        bmp=payload[bmp_off:bmp_end], bmp_info=info,
                        attributes=0, index=index)
                except BiosLogoError:
                    pass
    return None


def scan(data: bytes) -> list:
    """扫描固件，返回所有可替换的 Logo 槽位。"""
    pat = guid_to_bytes(LOGO_GUID)
    slots, idx, pos = [], 0, data.find(pat)
    seen = set()
    while pos != -1:
        if pos not in seen:
            seen.add(pos)
            slot = _try_build_slot(data, pos, idx)
            if slot is not None:
                slots.append(slot)
                idx += 1
        pos = data.find(pat, pos + 1)
    return slots


def parse_bios(path: str):
    """读取 BIOS 文件，返回 (原始字节, 槽位列表)。"""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 1 << 20:
        raise BiosLogoError(f"文件太小（{len(data)} 字节），不像是完整 BIOS")
    slots = scan(data)
    return data, slots


# --------------------------------------------------------------------------
# 适配方式 / 图片预处理
# --------------------------------------------------------------------------
FIT_CONTAIN = "contain"     # 完整显示：整张图放进画布，可能留边
FIT_COVER = "cover"         # 铺满裁剪：放大到铺满画布，超出的部分裁掉
FIT_STRETCH = "stretch"     # 拉伸填满：直接拉成画布尺寸（会变形）

FIT_MODES = (FIT_CONTAIN, FIT_COVER, FIT_STRETCH)

FIT_LABELS = {
    FIT_CONTAIN: "完整显示",
    FIT_COVER: "铺满裁剪",
    FIT_STRETCH: "拉伸填满",
}

FIT_TIPS = {
    FIT_CONTAIN: "整张图等比放进画面，不裁切任何内容，可能留黑边（最稳，推荐）",
    FIT_COVER: "等比放大到铺满画面，超出画布的部分被裁掉（最大，可能切掉边缘）",
    FIT_STRETCH: "强行拉成画面比例，画面会变形（一般不推荐）",
}


def _flatten(img):
    """把带透明通道的图合成到纯黑底上，返回 RGB 图。"""
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (0, 0, 0))
        base.paste(rgba, (0, 0), rgba)
        return base
    return img.convert("RGB")


def content_bbox(img, threshold: int = 10):
    """返回图片里"非黑内容"的外接矩形 (l, t, r, b)；全黑则返回 None。

    开机图几乎都是黑底，去掉这些黑边往往是"Logo 太小"的根本原因。
    """
    rgb = _flatten(img)
    lum = rgb.convert("L")
    mask = lum.point(lambda v: 255 if v > threshold else 0)
    return mask.getbbox()


def trim_black_border(img, threshold: int = 10, margin: int = 0):
    """裁掉四周的纯黑边（保留 margin 像素余量）。找不到内容时原样返回。"""
    bbox = content_bbox(img, threshold)
    if bbox is None:
        return img
    l, t, r, b = bbox
    l = max(0, l - margin)
    t = max(0, t - margin)
    r = min(img.width, r + margin)
    b = min(img.height, b + margin)
    if r - l < 2 or b - t < 2:
        return img
    return img.crop((l, t, r, b))


def crop_to_aspect(img, aw: float, ah: float):
    """按 aw:ah 的比例，从图片中心取最大的矩形（用于"按目标比例"裁剪）。"""
    if aw <= 0 or ah <= 0:
        return img
    w, h = img.size
    target = aw / ah
    if w / h > target:                 # 原图偏宽 → 裁左右
        nw = max(1, round(h * target))
        nh = h
    else:                              # 原图偏高 → 裁上下
        nw = w
        nh = max(1, round(w / target))
    left = (w - nw) // 2
    top = (h - nh) // 2
    return img.crop((left, top, left + nw, top + nh))


def coverage_ratio(img, background=(0, 0, 0), threshold: int = 10) -> float:
    """画面里"非背景"像素的占比，用来告诉用户 Logo 到底填了多大一块。"""
    bbox = content_bbox(img, threshold)
    if bbox is None:
        return 0.0
    l, t, r, b = bbox
    return ((r - l) * (b - t)) / float(img.width * img.height)


def fit_image(src, w: int, h: int, mode: str = FIT_CONTAIN, zoom: float = 1.0,
              background=(0, 0, 0)):
    """把 src 按 mode / zoom 放到 w×h 的画布上，返回恰好 w×h 的 RGB 图。

    zoom 是额外的缩放倍数：>1 放大（超出画布的部分会被裁掉），<1 缩小。
    """
    from PIL import Image

    src = src.convert("RGBA")
    zoom = max(0.02, float(zoom))
    if src.width < 1 or src.height < 1:
        raise BiosLogoError("图片尺寸非法。")

    if mode == FIT_STRETCH:
        nw = max(1, round(w * zoom))
        nh = max(1, round(h * zoom))
    else:
        if mode not in FIT_MODES:
            mode = FIT_CONTAIN
        base = (max if mode == FIT_COVER else min)(w / src.width, h / src.height)
        nw = max(1, round(src.width * base * zoom))
        nh = max(1, round(src.height * base * zoom))

    resized = src.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), tuple(background))
    # 居中放置；PIL 的 paste 会自动裁掉落在画布外的部分
    canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2), resized)
    return canvas


# --------------------------------------------------------------------------
# 渲染替换图
# --------------------------------------------------------------------------
def make_bmp_header(w: int, h: int, bpp: int, stride: int) -> bytes:
    """生成标准的 54 字节 BMP 头（BITMAPINFOHEADER + BI_RGB，自下而上）。"""
    pixels = stride * h
    return (struct.pack("<2sIHHI", b"BM", 14 + 40 + pixels, 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, bpp, 0, pixels,
                          2835, 2835, 0, 0))


def bmp_bytes_for(w: int, h: int, bpp: int, canvas,
                  top_down: bool = False,
                  header: bytes | None = None) -> bytes:
    """把 PIL 图（w×h，RGB）编成 BMP 字节。

    ``top_down=True`` 时像素按自上而下排列（BMP 高度为负数的那种）；
    ``header`` 不为 None 时复用它（长度必须与像素数据匹配）。
    """
    if not (1 <= w <= 16384 and 1 <= h <= 16384):
        raise BiosLogoError(f"尺寸超出允许范围：{w}x{h}（单边 1..16384）")
    stride = ((w * bpp + 31) // 32) * 4

    if bpp == 24:
        raw = canvas.tobytes()                       # RGB 自上而下
        row = bytearray(w * 3)
        out = bytearray()
        for y in range(h):
            base = y * w * 3
            row[0::3] = raw[base + 2:base + w * 3:3]
            row[1::3] = raw[base + 1:base + w * 3:3]
            row[2::3] = raw[base + 0:base + w * 3:3]
            out += row + b"\x00" * (stride - w * 3)
    else:                                            # 32bpp
        rgba = canvas.convert("RGBA").tobytes()
        row = bytearray(w * 4)
        out = bytearray()
        for y in range(h):
            base = y * w * 4
            row[0::4] = rgba[base + 2:base + w * 4:4]
            row[1::4] = rgba[base + 1:base + w * 4:4]
            row[2::4] = rgba[base + 0:base + w * 4:4]
            row[3::4] = b"\xff" * w
            out += row + b"\x00" * (stride - w * 4)

    if not top_down:
        # BMP 常规是自下而上，把行序倒过来
        rows = [bytes(out[i * stride:(i + 1) * stride]) for i in range(h)]
        out = bytearray(b"".join(reversed(rows)))

    head = header if header is not None else make_bmp_header(w, h, bpp, stride)
    return head + bytes(out)


PALETTED_BPPS = (1, 4, 8)


def bmp_bytes_paletted(canvas, bpp: int) -> bytes:
    """把 PIL 图编成 1/4/8bpp 的调色板 BMP（自下而上，BI_RGB）。

    两个关键选择，都是为了让 LZMA 压得动：

    * **不抖动** —— 抖动的噪点会让压缩结果大一大截，而开机 Logo 不需要照片级还原；
    * **调色板按亮度重排** —— 量化出来的索引值只代表「第几个颜色」，和明暗无关，
      于是平滑渐变会变成跳来跳去的索引、熵很高。把颜色按亮度排序后重新编号，
      渐变就变成单调递增的索引段，压缩率明显变好。（这只改编号，不改画面。）

    注意：Pillow 的 BMP 写出并不支持 ``bits=4``（会静默按 8bpp 落盘），所以
    位打包和文件头这里全部自己写。
    """
    import numpy as np
    from PIL import Image

    if bpp not in PALETTED_BPPS:
        raise BiosLogoError(f"不是调色板位深: {bpp}")
    n = 1 << bpp
    w, h = canvas.size
    q = canvas.convert("RGB").quantize(colors=n,
                                       method=Image.Quantize.MEDIANCUT,
                                       dither=Image.Dither.NONE)
    pal = list(q.getpalette() or [])[:n * 3]
    if len(pal) < n * 3:
        pal += [0] * (n * 3 - len(pal))

    # 按亮度（Rec.601）升序重排调色板，并同步重映射每个像素的索引
    order = sorted(range(n), key=lambda i: (pal[i * 3] * 299 + pal[i * 3 + 1] * 587
                                            + pal[i * 3 + 2] * 114, i))
    remap = [0] * 256
    for new_i, old_i in enumerate(order):
        remap[old_i] = new_i
    new_pal = b"".join(bytes((pal[o * 3 + 2], pal[o * 3 + 1], pal[o * 3], 0))
                       for o in order)

    idx = np.frombuffer(q.point(remap).tobytes(), dtype=np.uint8)
    idx = idx.reshape(h, w)                      # 自上而下

    if bpp == 8:
        packed = idx
    elif bpp == 4:
        if w % 2:
            idx = np.pad(idx, ((0, 0), (0, 1)))
        packed = (idx[:, 0::2].astype(np.uint16) << 4) | idx[:, 1::2]
        packed = packed.astype(np.uint8)
    else:                                        # 1bpp
        if w % 8:
            idx = np.pad(idx, ((0, 0), (0, 8 - w % 8)))
        packed = np.packbits(idx, axis=1)

    stride = ((w * bpp + 31) // 32) * 4
    if packed.shape[1] < stride:
        packed = np.pad(packed, ((0, 0), (0, stride - packed.shape[1])))
    pixels = np.ascontiguousarray(packed[::-1]).tobytes()   # 行序倒过来

    head_size = 14 + 40 + len(new_pal)
    file_size = head_size + len(pixels)
    head = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, head_size)
    head += struct.pack("<IiiHHIIiiII", 40, w, h, 1, bpp, 0, len(pixels),
                        2835, 2835, n, 0)
    return head + new_pal + pixels


def render_bmp(slot: LogoSlot, new_image, background=(0, 0, 0),
               mode: str = FIT_CONTAIN, zoom: float = 1.0,
               auto_trim: bool = False, trim_threshold: int = 10,
               out_size=None, out_bpp: int | None = None) -> bytes:
    """把用户图片渲染成 BMP。

    ``out_size=None``（默认）→ 使用**原 Logo 的尺寸**并逐字节复用原 BMP 头，
    产出的 BMP 与原来完全等长。

    ``out_size=(w, h)`` → 生成 **w×h** 的新 BMP（含新头）。尺寸可以与原 Logo
    完全不同、比例也可以不同；只要重新压缩后能放进段内即可。
    """
    info = slot.bmp_info

    src = new_image
    if auto_trim:
        src = trim_black_border(src, trim_threshold)

    if out_size is None:
        w, h = info.width, info.rows
        canvas = fit_image(src, w, h, mode=mode, zoom=zoom,
                           background=background)
        # 只有「原尺寸 + 原颜色位数」才复用原头，保证与改造前逐字节一致
        if out_bpp in (None, info.bpp) and info.bpp not in PALETTED_BPPS:
            new_bmp = bmp_bytes_for(w, h, info.bpp, canvas,
                                    top_down=info.top_down, header=info.header)
            if len(new_bmp) != len(slot.bmp):
                raise BiosLogoError(
                    f"内部错误：新 BMP {len(new_bmp)} 字节 != 原 BMP {len(slot.bmp)} 字节")
            return new_bmp
        bpp = info.bpp if out_bpp is None else int(out_bpp)
        if bpp in PALETTED_BPPS:
            return bmp_bytes_paletted(canvas, bpp)
        return bmp_bytes_for(w, h, bpp, canvas)

    w, h = int(out_size[0]), int(out_size[1])
    canvas = fit_image(src, w, h, mode=mode, zoom=zoom, background=background)
    bpp = info.bpp if out_bpp is None else int(out_bpp)
    if bpp in PALETTED_BPPS:
        return bmp_bytes_paletted(canvas, bpp)
    if bpp not in (24, 32):
        raise BiosLogoError(f"不支持的输出颜色位数: {bpp}")
    return bmp_bytes_for(w, h, bpp, canvas)


#: 段类型 -> 「段头 + 段内自带字段」的长度（EFI section header size）。
#: 只有这两种类型在通用 4 字节头之后还有自己的字段。
_EXTRA_HDR = {0x02: 24, 0x18: 24}


def _section_payload_start(plain: bytes, off: int) -> int:
    return off + _EXTRA_HDR.get(plain[off + 3], 4)


def _walk_sections(plain: bytes):
    """按 UEFI 规则遍历明文里的 section 序列，产出 (偏移, 长度, 类型)。"""
    off = 0
    while off + 4 <= len(plain):
        size = int.from_bytes(plain[off:off + 3], "little")
        if size < 4 or off + size > len(plain):
            break
        yield off, size, plain[off + 3]
        off = (off + size + 3) & ~3          # section 之间按 4 字节对齐


def check_plaintext(plain: bytes, bmp_off: int, bmp_len: int) -> str:
    """检查明文里的 section 链是否自洽。通过返回空串，否则返回原因。

    这是「换图之后段 size 字段忘了改」这类错误的守门人：一旦 BMP 尺寸变了
    而某个 size 字段没跟上，section 链就会走不通，这里会立刻报出来。
    """
    off, n, bmp_sec = 0, 0, None
    while off + 4 <= len(plain):
        size = int.from_bytes(plain[off:off + 3], "little")
        if size < 4 or off + size > len(plain):
            return f"section @+{off} 的 size={size} 越界（明文共 {len(plain)} 字节）"
        if _section_payload_start(plain, off) == bmp_off:
            bmp_sec = (off, size)
        n += 1
        off = (off + size + 3) & ~3
        if n > 64:
            return "section 数量异常（>64）"
    # 允许 off 比明文长度少 0..3 字节：段尾的 4 字节对齐填充可能比
    # 「上一段结束位置向上取整」多 1..3 字节，此时 off 会落在明文末尾之前，
    # 仍属自洽；只有偏差超过 3 字节才说明某段 size 字段错了。
    if not -3 <= off - len(plain) <= 3:
        return f"section 链走到 +{off}，与明文长度 {len(plain)} 不吻合"
    if bmp_sec is None:
        return "没有找到承载 BMP 的 section"
    _start, size = bmp_sec
    want = bmp_off + bmp_len
    if size != want:
        return (f"Logo section 的 size 字段是 {size}，应为 {want}"
                f"（段头 {bmp_off} + BMP {bmp_len}）—— 段长度字段没有同步")
    return ""


def build_plaintext(slot: LogoSlot, new_bmp: bytes) -> bytes:
    """把新 BMP 装回明文。

    解压出来的明文不是裸 BMP，而是一串 EFI section：
    ``[4 字节 RAW 段头: 3 字节 size + 1 字节 type=0x19][BMP][4 字节对齐填充]``
    ``[USER_INTERFACE 段: "Logo.bmp"]``。

    所以换图时**必须同步改写那个 RAW 段的 size 字段**——它的值等于
    ``4 + BMP 字节数``。以前这里把段头原样拷过去，只要输出尺寸和原来不同，
    段 size 就是过期值，固件按段表走就会解析失败，开机看不到 Logo。
    同时 BMP 之后的 4 字节对齐填充长度也会随新长度变化，需要重新计算。
    """
    plain = slot.plaintext
    sec_start = None
    for off, size, _type in _walk_sections(plain):
        if _section_payload_start(plain, off) == slot.bmp_off:
            sec_start, sec_size = off, size
            break
    if sec_start is None:
        # 裸 BMP 明文（没有 UEFI section 链）：明文就是 BMP 本身，
        # 此时没有段 size 字段要同步，直接返回新 BMP 即可。
        if slot.bmp_off == 0 and slot.bmp_off + len(slot.bmp) == len(plain):
            return new_bmp
        raise BiosLogoError("定位 Logo 所在的 EFI section 失败，无法重建明文")

    # BMP 必须正好占满该段的载荷，否则这里就不是「换张图」能解决的事
    if slot.bmp_off + len(slot.bmp) != sec_start + sec_size:
        raise BiosLogoError("Logo 段内 BMP 之后仍有数据，暂不支持改尺寸")

    head = bytearray(plain[:slot.bmp_off])
    new_sec_size = slot.bmp_off + len(new_bmp)
    head[sec_start:sec_start + 3] = new_sec_size.to_bytes(3, "little")

    pad_old = (-(sec_start + sec_size)) % 4
    trailer = plain[sec_start + sec_size + pad_old:]
    pad_new = b"\x00" * ((-new_sec_size) % 4)
    out = bytes(head) + new_bmp + pad_new + trailer
    problem = check_plaintext(out, slot.bmp_off, len(new_bmp))
    if problem:
        raise BiosLogoError(f"内部错误：重建后的明文结构不自洽 —— {problem}")
    return out


def probe_fit(slot: LogoSlot, new_bmp: bytes) -> tuple:
    """试算这份 BMP 装回段里要多少压缩字节，返回 (压缩后长度, 明文长度)。

    放不下时抛 :class:`BiosLogoError`，错误信息里带可操作的建议。
    """
    plain = build_plaintext(slot, new_bmp)
    _stream, raw_len, _props = compress_equal_length(plain, slot)
    return raw_len, len(plain)


def size_for_aspect(aspect_w: float, aspect_h: float,
                    area: int | None = None) -> tuple:
    """按给定比例算出「与原画布像素量相当」的尺寸，避免改尺寸后压不下。"""
    import math
    if aspect_w <= 0 or aspect_h <= 0:
        raise BiosLogoError("图片尺寸异常，无法计算比例")
    if area is None:
        # 兜底值：调用方没给目标像素量时用「典型 BIOS Logo 画布」的像素量。
        # 实际代码路径（CLI --size-ratio）总是显式传 slot 的 BMP 像素量，
        # 这里只是给独立调用 / 测试留的默认。
        area = 293 * 400
    a = aspect_w / aspect_h
    h = max(1, int(round(math.sqrt(area / a))))
    w = max(1, int(round(a * h)))
    return w, h


PROPS_CANDIDATES = (0x5D, 0x04, 0x00, 0x01, 0x02)

#: 默认**不**去动 LZMA 属性字节。实测整份 B650E 固件里几十个 LZMA 段的
#: props 全是 0x5D，没有一个例外；而改属性只省约 5% 却要赌固件解码器会读
#: 段里那个字节（部分 AMI 版本是按 0x5D 写死的）。收益太小、风险太大，
#: 所以默认只复用原属性，``tune_props=True`` 仅作为显式实验开关保留。
TUNE_PROPS_DEFAULT = False


def _compress_raw(plain: bytes, props: int, dict_size: int, preset: int) -> bytes:
    c = lzma.LZMACompressor(
        format=lzma.FORMAT_RAW,
        filters=[dict(_lzma_filters(props, dict_size)[0], preset=preset)])
    return c.compress(plain) + c.flush()


def compress_equal_length(plain: bytes, slot: LogoSlot,
                          tune_props: bool = TUNE_PROPS_DEFAULT) -> tuple:
    """把明文压回 LZMA 流，并填 0xFF 到段内可用长度。

    返回 ``(填充后的段内容, 真实压缩流长度, 实际使用的 props 字节)``。

    LZMA 的属性字节（lc/lp/pb）对固件图片这种大片纯色的数据影响不小 —— 实测
    同一张 720x480 的图，props 从 0x5D 换成 0x04 能省 5%。所以除了原属性，
    再试几个常见组合，取最小的那个。字典大小和位深都不变，段头结构不变。
    """
    candidates = [slot.props]
    if tune_props:
        candidates += [p for p in PROPS_CANDIDATES if p != slot.props]

    best = None                       # (长度, 流, props)
    first_len = None
    ranked = []                       # 按 preset 6 的结果排序，供慢档位复筛
    for props in candidates:
        try:
            stream = _compress_raw(plain, props, slot.dict_size, 6)
        except lzma.LZMAError:                       # pragma: no cover
            continue
        if first_len is None:
            first_len = len(stream)
            if first_len > slot.stream_budget * 3:
                # 连第一个属性都差 3 倍以上，别的组合最多再好百分之几，直接放弃，
                # 免得为一张几 MB 的图白等好几分钟。
                break
        ranked.append((len(stream), stream, props))
        if best is None or len(stream) < len(best[1]):
            best = (len(stream), stream, props)

    # 慢档位（preset 9）只在前两名上收尾 —— 属性字节的最优解不一定和快档位
    # 一致，取两名能拿回大部分收益，又不至于把耗时翻几倍。
    if best is not None and len(ranked) > 1:
        ranked.sort(key=lambda t: t[0])
        for _n, _s, cand in ranked[:2]:
            try:
                slow = _compress_raw(plain, cand, slot.dict_size, 9)
            except lzma.LZMAError:                   # pragma: no cover
                continue
            if len(slow) < best[0]:
                best = (len(slow), slow, cand)

    if best is not None and best[0] <= slot.stream_budget:
        stream, props = best[1], best[2]
        # 自检：解回来必须一模一样
        try:
            back = lzma.LZMADecompressor(
                format=lzma.FORMAT_RAW,
                filters=_lzma_filters(props, slot.dict_size),
            ).decompress(stream, max_length=len(plain))
        except lzma.LZMAError:
            back = None
        if back == plain:
            pad = b"\xff" * (slot.stream_budget - len(stream))
            return stream + pad, len(stream), props

    need = best[0] if best is not None else (first_len or 0)
    raise BiosLogoError(
        f"放不下：Logo 段内只有 {slot.stream_budget:,} 字节可用，"
        f"这张图最小的压缩结果仍需 {need:,} 字节（超出 "
        f"{need - slot.stream_budget:,} 字节）。\n"
        f"可以试试：① 勾选「放不下时自动缩小」，让工具自己找能放下的最大尺寸；"
        f"② 把「输出尺寸」改小（同一张图，尺寸越小压缩后越小）；"
        f"③ 把「颜色位数」降到 8 位或 4 位（实测能省一半以上）；"
        f"④ 用「裁剪图片…」只保留需要的部分；"
        f"⑤ 切换「自动去黑边」——画面留白多时反而压得更小。")


# --------------------------------------------------------------------------
# 放不下就自动缩小
# --------------------------------------------------------------------------
def fit_to_budget(slot: LogoSlot, new_image, mode: str = FIT_CONTAIN,
                  zoom: float = 1.0, auto_trim: bool = False,
                  out_size=None, out_bpp=None, background=(0, 0, 0),
                  min_side: int = 16) -> tuple:
    """渲染 + 试压；放不下就等比缩小重试，返回能放下的最大尺寸。

    返回 ``(bmp, (w, h), scale, stream_len, plain_len)``。``scale`` 是相对
    请求尺寸的缩放系数（1.0 表示原样放得下）。
    """
    info = slot.bmp_info
    req = (info.width, info.rows) if out_size is None else \
        (int(out_size[0]), int(out_size[1]))

    def attempt(w: int, h: int):
        w = max(1, int(round(w)))
        h = max(1, int(round(h)))
        if w < min_side or h < min_side:
            return None
        bmp = render_bmp(slot, new_image, background=background, mode=mode,
                         zoom=zoom, auto_trim=auto_trim,
                         out_size=(w, h) if (w, h) != req else out_size,
                         out_bpp=out_bpp)
        plain = build_plaintext(slot, bmp)
        try:
            _stream, raw_len, _props = compress_equal_length(plain, slot)
        except BiosLogoError:
            return None
        return (bmp, (w, h), raw_len, len(plain))

    hit = attempt(*req)
    if hit is not None:
        return hit[0], hit[1], 1.0, hit[2], hit[3]

    lo, hi = 0.0, 1.0                    # lo 一定放不下（0 视为放不下）
    best = None
    for _ in range(11):
        mid = (lo + hi) / 2
        got = attempt(req[0] * mid, req[1] * mid)
        if got is None:
            hi = mid
        else:
            best = got
            lo = mid
    if best is None:
        raise BiosLogoError(
            f"自动缩小也放不下：Logo 段内只有 {slot.stream_budget:,} 字节可用，"
            f"连 {min_side}x{min_side} 都塞不进去。请换一张内容更简单、"
            f"留白更多的图，或把「颜色位数」降到 8 位 / 4 位。")
    w, h = best[1]
    return best[0], (w, h), max(w / req[0], h / req[1]), best[2], best[3]



# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
@dataclass
class ReplaceReport:
    diff_count: int = 0
    diff_min: int = 0
    diff_max: int = 0
    only_inside_logo: bool = False
    verify_ok: bool = False
    stream_len: int = 0
    budget: int = 0
    out_size: int = 0
    src_size: int = 0
    out_dims: tuple = ()
    plain_len: int = 0
    out_bpp: int = 0
    props: int = 0
    scale: float = 1.0
    sha_before: str = ""
    sha_after: str = ""
    warnings: list = field(default_factory=list)

    def text(self) -> str:
        lines = [
            f"原文件大小 : {self.src_size:,} 字节",
            f"新文件大小 : {self.out_size:,} 字节"
            f"{'  (等长 ✓)' if self.src_size == self.out_size else '  (长度变化 ✗)'}",
            f"Logo 尺寸  : {self.out_dims[0]}x{self.out_dims[1]}"
            f"{f'  (自动缩小到 {self.scale * 100:.0f}%)' if self.scale < 0.999 else ''}"
            if len(self.out_dims) == 2 else "",
            f"颜色位数   : {self.out_bpp} bpp",
            f"LZMA 属性  : 0x{self.props:02X}",
            f"解压后长度 : {self.plain_len:,} 字节",
            f"改动字节数 : {self.diff_count:,} 字节",
            f"改动区间   : 0x{self.diff_min:08X} .. 0x{self.diff_max:08X}",
            f"只在 Logo 段内 : {'是 ✓' if self.only_inside_logo else '否 ✗'}",
            f"回读校验   : {'通过 ✓' if self.verify_ok else '失败 ✗'}",
            f"新压缩流   : {self.stream_len:,} 字节 "
            f"(段内可用 {self.budget:,} 字节, 余 "
            f"{(self.budget - self.stream_len) / self.budget * 100:.1f}%)",
            f"原 SHA-256 : {self.sha_before}",
            f"新 SHA-256 : {self.sha_after}",
        ]
        lines = [l for l in lines if l]
        lines += [f"警告       : {w}" for w in self.warnings]
        return "\n".join(lines)


def replace(src_data: bytes, slot: LogoSlot, new_image, out_path: str,
            mode: str = FIT_CONTAIN, zoom: float = 1.0,
            auto_trim: bool = False, out_size=None,
            out_bpp: int | None = None, auto_fit: bool = False) -> ReplaceReport:
    """用 new_image 替换指定槽位的 Logo，写出新 BIOS，并做完整校验。

    ``out_size=None`` 保持原 Logo 尺寸；``out_size=(w, h)`` 则输出该尺寸的
    Logo（可与原尺寸、原比例完全不同），同时更新段内的「解压后长度」字段。

    ``out_bpp`` 指定输出颜色位数（24 / 8 / 4 / 1），``None`` 表示跟随原 Logo。

    ``auto_fit=True`` 时，若目标尺寸放不下，会**自动等比缩小**到能放下的最大
    尺寸（异步预览里也会先算出来给用户看）。
    """
    import hashlib

    info = slot.bmp_info
    scale = 1.0
    if auto_fit:
        new_bmp, dims, scale, raw_len, plain_len = fit_to_budget(
            slot, new_image, mode=mode, zoom=zoom, auto_trim=auto_trim,
            out_size=out_size, out_bpp=out_bpp)
        new_plain = build_plaintext(slot, new_bmp)
        stream, raw_len, props = compress_equal_length(new_plain, slot)
        out_w, out_h = dims
    else:
        if out_size is None:
            out_w, out_h = info.width, info.rows
        else:
            out_w, out_h = int(out_size[0]), int(out_size[1])
        new_bmp = render_bmp(slot, new_image, mode=mode, zoom=zoom,
                             auto_trim=auto_trim, out_size=out_size,
                             out_bpp=out_bpp)
        new_plain = build_plaintext(slot, new_bmp)
        stream, raw_len, props = compress_equal_length(new_plain, slot)

    out = bytearray(src_data)
    out[slot.stream_off:slot.section_end] = stream
    # LZMA 属性字节与「解压后长度」字段都要同步刷新
    out[slot.stream_off - 13] = props
    struct.pack_into("<Q", out, slot.stream_off - 8, len(new_plain))

    # ---- 校验 ----
    rep = ReplaceReport(
        src_size=len(src_data), out_size=len(out),
        stream_len=raw_len, budget=slot.stream_budget,
        out_dims=(out_w, out_h), plain_len=len(new_plain),
        out_bpp=new_bmp[28] | (new_bmp[29] << 8), props=props, scale=scale,
        sha_before=hashlib.sha256(src_data).hexdigest(),
        sha_after=hashlib.sha256(bytes(out)).hexdigest(),
    )
    diffs = [i for i in range(len(src_data)) if src_data[i] != out[i]]
    rep.diff_count = len(diffs)
    if diffs:
        rep.diff_min, rep.diff_max = min(diffs), max(diffs)
        # 允许改到压缩流之前的那 8 字节「解压后长度」字段，但不能碰段头以外
        rep.only_inside_logo = (rep.diff_min >= slot.data_start
                                and rep.diff_max < slot.section_end)
    else:
        rep.only_inside_logo = True

    back = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=_lzma_filters(props, slot.dict_size),
    ).decompress(bytes(out[slot.stream_off:slot.section_end]),
                 max_length=len(new_plain))
    rep.verify_ok = (back == new_plain)
    # 段头里声明的长度也得和实际解密结果对得上
    declared = struct.unpack_from("<Q", out, slot.stream_off - 8)[0]
    rep.verify_ok = rep.verify_ok and declared == len(new_plain)

    if not rep.only_inside_logo:
        raise BiosLogoError("安全校验失败：改动越出了 Logo 段范围，已中止。")
    if not rep.verify_ok:
        raise BiosLogoError("安全校验失败：回读解压结果与预期不符，已中止。")
    if rep.src_size != rep.out_size:
        raise BiosLogoError("安全校验失败：文件长度发生变化，已中止。")

    with open(out_path, "wb") as f:
        f.write(bytes(out))
    return rep


# --------------------------------------------------------------------------
# 命令行（便于自动化测试）
# --------------------------------------------------------------------------
def _cli(argv: Iterable) -> int:
    import argparse
    import sys

    from PIL import Image

    # Windows 控制台默认可能是 GBK 等窄编码，报告里的 ✓ / ⚠ 等字符会触发
    # UnicodeEncodeError 直接崩掉。这里统一把编码错误降级为替换，绝不因打印失败中断。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(prog="bioslogo",
                                 description="BIOS 开机 Logo 解析 / 替换")
    ap.add_argument("--in", dest="src", required=True, help="输入 BIOS 文件")
    ap.add_argument("--list", action="store_true", help="列出所有 Logo")
    ap.add_argument("--extract", metavar="PNG", help="导出 Logo 为 PNG")
    ap.add_argument("--replace", metavar="IMAGE", help="用该图片替换 Logo")
    ap.add_argument("--slot", type=int, default=0, help="槽位序号（默认 0）")
    ap.add_argument("--out", metavar="BIOS", help="输出 BIOS 路径")
    ap.add_argument("--fit", choices=list(FIT_MODES), default=FIT_CONTAIN,
                    help="适配方式（默认 contain）")
    ap.add_argument("--zoom", type=float, default=1.0, help="额外缩放倍数（默认 1.0）")
    ap.add_argument("--trim", action="store_true", help="替换前先裁掉四周黑边")
    ap.add_argument("--size", metavar="WxH",
                    help="输出 Logo 的像素尺寸，例如 500x300")
    ap.add_argument("--size-720", action="store_true",
                    help="输出固定 720x480")
    ap.add_argument("--size-original", action="store_true",
                    help="输出用替换图片自己的像素尺寸（界面上的「原图」）；"
                         "不加任何 --size* 即为界面默认的「跟原 Logo 一样」")
    ap.add_argument("--size-ratio", action="store_true",
                    help="按图片自身比例自动算输出尺寸（不要求与原 Logo 一致）")
    ap.add_argument("--bpp", type=int, choices=[24, 8, 4, 1],
                    help="输出颜色位数：24（默认，唯一真机验证过的）/ 8 / 4 / 1"
                         "（位数越低压缩后越小，但颜色越少且未经真机验证）")
    ap.add_argument("--auto-fit", action="store_true",
                    help="放不下时自动等比缩小到能放下的最大尺寸")
    args = ap.parse_args(list(argv))

    data, slots = parse_bios(args.src)
    print(f"文件: {args.src}  ({len(data)} 字节)")
    print(f"找到 {len(slots)} 处 Logo\n")
    if not slots:
        print("未找到可替换的 Logo。")
        return 2
    for s in slots:
        print(f"--- 槽位 #{s.index} ---")
        print(s.path_text())
        print()

    slot = slots[min(args.slot, len(slots) - 1)]
    if args.extract:
        slot.to_image().save(args.extract)
        print(f"已导出: {args.extract}")
    if args.replace:
        if not args.out:
            print("--replace 需要同时指定 --out")
            return 2
        img = Image.open(args.replace)
        out_size = None
        if args.size:
            try:
                sw, sh = args.size.lower().split("x")
                out_size = (int(sw), int(sh))
            except Exception:
                print(f"--size 格式应为 WxH，例如 500x300（收到 {args.size!r}）")
                return 2
        elif args.size_720:
            out_size = (720, 480)
            print(f"输出尺寸：720x480")
        elif args.size_original:
            src = img
            if args.trim:
                src = trim_black_border(src)
            out_size = (src.width, src.height)
            print(f"输出尺寸：原图 {out_size[0]}x{out_size[1]}")
        elif args.size_ratio:
            src = img
            if args.trim:
                src = trim_black_border(src)
            info = slot.bmp_info
            out_size = size_for_aspect(src.width, src.height,
                                       area=info.width * info.rows)
            print(f"按图片比例自动选定输出尺寸：{out_size[0]}x{out_size[1]}")
        rep = replace(data, slot, img, args.out, mode=args.fit, zoom=args.zoom,
                      auto_trim=args.trim, out_size=out_size,
                      out_bpp=args.bpp, auto_fit=args.auto_fit)
        print(rep.text())
        print(f"\n已写出: {args.out}")
    return 0


def main(argv: Iterable | None = None) -> int:
    """CLI 入口：把预期内的失败变成一行提示，而不是抛栈。"""
    import sys
    if argv is None:
        argv = sys.argv[1:]
    try:
        return _cli(argv)
    except BiosLogoError as exc:
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass
        print(f"出错了：{exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        # 用户给的文件路径不存在（BIOS 文件或替换图片）——预期内的失败，
        # 给一行提示而不是抛栈。
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass
        print(f"找不到文件：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
