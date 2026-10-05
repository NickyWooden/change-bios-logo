#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构造一个带「压缩（LZMA）Logo 槽位」的合成 BIOS，用于 Web 版端到端测试。

布局（路径 1 / 压缩段）::

    [L]      16B LOGO_GUID          （裸放在文件里，供 scan() 全局搜索命中）
    [L+16]   4B  段头  3B size + 1B type=0x02 (GUID_DEFINED)
    [L+20]   16B LZMA_GUID          （在 LOGO_GUID 之后 20 字节，< 96，触发路径 1）
    [L+36]   2B data_off=24  2B attributes=0x0001
    [L+40]   13B LZMA 头  1B props + 4B dict_size + 8B usize
    [L+53]   压缩流本体（明文 = 一张 24bit BMP，"BM" 在明文偏移 0）

明文只放 BMP（不含 LOGO_GUID），避免 LOGO_GUID 在压缩流里二次命中产生多余槽位。
"""
import io
import lzma
import sys
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from bioslogo import LOGO_GUID, LZMA_GUID, guid_to_bytes, scan  # noqa: E402


def build_bmp(w: int = 200, h: int = 120) -> bytes:
    """用 Pillow 画一张简单的 24bit BMP Logo。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (30, 30, 60))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, w - 20, h - 20], outline=(255, 200, 0), width=4)
    d.ellipse([40, 30, w - 40, h - 30], fill=(0, 200, 255))
    d.text((w // 2 - 20, h // 2 - 8), "TEST", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()


def main() -> int:
    bmp_bytes = build_bmp()
    logo_pat = guid_to_bytes(LOGO_GUID)
    lzma_pat = guid_to_bytes(LZMA_GUID)

    # 明文 = 仅 BMP
    plaintext = bmp_bytes
    usize = len(plaintext)

    # 压缩（props=0x5D => lc=3,lp=0,pb=2；dict_size=16MB）
    props = 0x5D
    lc = props % 9
    rest = props // 9
    lp = rest % 5
    pb = rest // 5
    dict_size = 0x01000000
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                "lc": lc, "lp": lp, "pb": pb}]
    c = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=filters)
    stream = c.compress(plaintext) + c.flush()
    print(f"压缩: {usize} -> {len(stream)} 字节 (props=0x{props:02X})")
    if logo_pat in stream:
        print("警告: LOGO_GUID 意外出现在压缩流中")

    # 组装段
    L = 0x1000
    data_off = 24
    attributes = 0x0001
    size = 37 + len(stream)  # 4(段头)+16(GUID)+4(off/attr)+13(LZMA头)+len(stream)
    section = bytearray()
    section += size.to_bytes(3, "little")
    section += bytes([0x02])
    section += lzma_pat
    section += data_off.to_bytes(2, "little")
    section += attributes.to_bytes(2, "little")
    section += bytes([props])
    section += dict_size.to_bytes(4, "little")
    section += usize.to_bytes(8, "little")
    section += stream

    # 组装文件（>= 1MB）
    file_size = 1 << 20
    if L + 16 + len(section) > file_size:
        file_size = L + 16 + len(section) + 4096
    data = bytearray(file_size)
    data[L:L + 16] = logo_pat
    data[L + 16:L + 16 + len(section)] = section

    out_path = Path(__file__).resolve().parent / "test_bios_compressed.bin"
    out_path.write_bytes(bytes(data))
    print(f"已生成合成 BIOS: {out_path} ({len(data)} 字节)")
    print(f"  LOGO_GUID @ 0x{L:X}   段 @ 0x{L + 16:X} (size=0x{size:X})")

    # 自检：scan 应恰好命中 1 个槽位
    slots = scan(bytes(data))
    print(f"\nscan() 命中 {len(slots)} 个槽位")
    for i, s in enumerate(slots):
        print(f"  槽位{i}: logo_guid_off=0x{s.logo_guid_off:X} "
              f"section_off=0x{s.section_off:X} stream_off=0x{s.stream_off:X} "
              f"section_end=0x{s.section_end:X}")
        print(f"         props=0x{s.props:02X} dict_size=0x{s.dict_size:X} "
              f"usize={s.usize}  budget={s.stream_budget}")
        print(f"         BMP: {s.bmp_info.describe()}")
    if len(slots) != 1:
        print(f"结果: 期望 1 个槽位，实际 {len(slots)} 个")
        return 1
    print("结果: OK（1 个压缩 Logo 槽位）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
