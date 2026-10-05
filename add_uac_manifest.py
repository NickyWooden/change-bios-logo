#!/usr/bin/env python3
"""
add_uac_manifest.py — 给 PyInstaller onefile exe 嵌入 UAC 清单（请求管理员权限提升）。

用法:
    python add_uac_manifest.py <exe_path>

原理:
    UAC 清单是一个 PE 资源（RT_MANIFEST, type 24, name ID 1）。
    本脚本用 pefile 读取 PE 文件，找到资源目录，然后添加 UAC 清单资源。
"""

import sys
import sys
import struct
import pefile

# UAC 清单 XML（请求 requireAdministrator）
UAC_MANIFEST_XML = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="requireAdministrator" uiAccess="false" />
      </requestedPrivileges>
    </security>
  </trustInfo>
</assembly>'''

RT_MANIFEST = 24  # 资源类型：清单
NAME_ID = 1        # 资源名称 ID（1 = 主清单）


def add_uac_manifest(exe_path: str) -> bool:
    """给 exe 嵌入 UAC 清单。返回 True 表示成功。"""
    pe = pefile.PE(exe_path)

    # 资源目录在 IMAGE_DIRECTORY_ENTRY_RESOURCE (index 2)
    resource_dir_rva = pe.OPTIONAL_HEADER.DATA_DIRECTORY[2].VirtualAddress
    resource_dir_size = pe.OPTIONAL_HEADER.DATA_DIRECTORY[2].Size
    if resource_dir_rva == 0:
        print("ERROR: 资源目录 RVA 为 0")
        return False

    # 读取资源目录数据
    resource_dir_data = pe.get_data(resource_dir_rva, resource_dir_size)

    # 解析资源目录结构
    # 资源目录是一个树形结构，每个节点有：
    #   - Characteristics (4 bytes)
    #   - TimeDateStamp (4 bytes)
    #   - MajorVersion (2 bytes)
    #   - MinorVersion (2 bytes)
    #   - NumberOfNamedEntries (2 bytes)
    #   - NumberOfIdEntries (2 bytes)
    #   - Entries (variable)

    # 根节点
    offset = 0
    (char, timestamp, major, minor, num_named, num_id) = struct.unpack_from(
        '<IIHHHH', resource_dir_data, offset
    )
    offset += 16  # 跳过根节点头

    print(f"资源目录: {num_named} 个命名条目, {num_id} 个 ID 条目")

    # 遍历 ID 条目，找到 RT_MANIFEST (24)
    manifest_entry_offset = None
    for i in range(num_id):
        (entry_id, data_offset) = struct.unpack_from('<II', resource_dir_data, offset)
        offset += 8
        if entry_id == RT_MANIFEST:
            manifest_entry_offset = data_offset
            print(f"找到 RT_MANIFEST 条目: ID={entry_id}, data_offset={data_offset:#x}")
            break

    if manifest_entry_offset is None:
        print("ERROR: 没有找到 RT_MANIFEST 条目")
        return False

    # 解析 RT_MANIFEST 子目录（offset 高位是子目录标志，需掩掉）
    offset = manifest_entry_offset & 0x7FFFFFFF
    (char, timestamp, major, minor, num_named, num_id) = struct.unpack_from(
        '<IIHHHH', resource_dir_data, offset
    )
    offset += 16

    print(f"RT_MANIFEST 子目录: {num_named} 个命名条目, {num_id} 个 ID 条目")

    # 遍历 ID 条目，找到 NAME_ID (1)
    name1_entry_offset = None
    for i in range(num_id):
        (entry_id, data_offset) = struct.unpack_from('<II', resource_dir_data, offset)
        offset += 8
        if entry_id == NAME_ID:
            name1_entry_offset = data_offset
            print(f"找到 NAME_ID=1 条目: data_offset={data_offset:#x}")
            break

    if name1_entry_offset is None:
        print("ERROR: 没有找到 NAME_ID=1 条目")
        return False

    # 解析 NAME_ID=1 的数据目录（offset 高位是子目录标志，需掩掉）
    offset = name1_entry_offset & 0x7FFFFFFF
    (char, timestamp, major, minor, num_named, num_lang) = struct.unpack_from(
        '<IIHHHH', resource_dir_data, offset
    )
    offset += 16  # 数据目录头（16 字节，与资源目录头相同布局；第 6 字段才是语言数）

    print(f"NAME_ID=1 数据目录: {num_lang} 个语言条目")

    # 遍历语言条目
    for i in range(num_lang):
        (lang_id, data_rva) = struct.unpack_from('<II', resource_dir_data, offset)
        offset += 8
        print(f"  语言条目: lang_id={lang_id:#x}, data_rva={data_rva:#x}")

        # 解析数据条目
        # 数据条目有：
        #   - Offset (4 bytes) - 相对于资源目录 RVA 的偏移
        #   - Size (4 bytes)
        #   - Characteristics (4 bytes)
        data_entry_offset = data_rva & 0x7FFFFFFF
        (data_offset, data_size, data_char) = struct.unpack_from(
            '<III', resource_dir_data, data_entry_offset
        )
        print(f"    数据条目: offset={data_offset:#x}, size={data_size}, char={data_char:#x}")

        # 读取现有清单数据
        existing_manifest = pe.get_data(resource_dir_rva + data_offset, data_size)
        print(f"    现有清单大小: {len(existing_manifest)} bytes")
        print(f"    现有清单内容: {existing_manifest[:200]}...")

        # 用 UAC 清单替换现有清单
        # 注意：UAC 清单可能比现有清单大，需要调整数据大小
        if len(UAC_MANIFEST_XML) > data_size:
            print(f"    UAC 清单更大 ({len(UAC_MANIFEST_XML)} > {data_size})，需要调整")
            # 更新数据大小
            struct.pack_into('<I', resource_dir_data, data_entry_offset + 4, len(UAC_MANIFEST_XML))

        # 写入 UAC 清单
        # 注意：pefile 的 get_data 返回的是原始数据，但我们需要修改 PE 文件
        # 这里用 pefile 的 get_data 读取，然后手动修改
        pass

    # 由于 pefile 是只读的，我们需要用另一种方式修改 PE 文件
    # 这里用 struct 直接修改原始字节
    with open(exe_path, 'r+b') as f:
        # 读取整个 PE 文件（用 bytearray 以便后续可写修改）
        pe_data = bytearray(f.read())

        # 计算资源目录在文件中的偏移
        # 资源目录 RVA -> 文件偏移
        section = pe.get_section_by_rva(resource_dir_rva)
        if section is None:
            print("ERROR: 找不到资源目录所在的 section")
            return False

        file_offset = resource_dir_rva - section.VirtualAddress + section.PointerToRawData
        print(f"资源目录文件偏移: {file_offset:#x}")

        # 修改资源目录数据
        # 重新解析并修改
        # 这里简化处理：直接替换清单数据
        # 找到 NAME_ID=1 的数据条目，替换其数据

        # 重新解析资源目录
        resource_dir_data = pe_data[file_offset:file_offset + resource_dir_size]

        # 这个太复杂了，用另一种方式
        # 直接找到清单数据的位置，替换它
        # 清单数据在 resource_dir_rva + data_offset 处
        # 文件偏移 = file_offset + data_offset

        # 重新解析找到 data_offset
        offset = 0
        (char, timestamp, major, minor, num_named, num_id) = struct.unpack_from(
            '<IIHHHH', resource_dir_data, offset
        )
        offset += 16

        manifest_entry_offset = None
        for i in range(num_id):
            (entry_id, data_offset) = struct.unpack_from('<II', resource_dir_data, offset)
            offset += 8
            if entry_id == RT_MANIFEST:
                manifest_entry_offset = data_offset
                break

        if manifest_entry_offset is None:
            print("ERROR: 没有找到 RT_MANIFEST 条目")
            return False

        offset = manifest_entry_offset & 0x7FFFFFFF
        (char, timestamp, major, minor, num_named, num_id) = struct.unpack_from(
            '<IIHHHH', resource_dir_data, offset
        )
        offset += 16

        name1_entry_offset = None
        for i in range(num_id):
            (entry_id, data_offset) = struct.unpack_from('<II', resource_dir_data, offset)
            offset += 8
            if entry_id == NAME_ID:
                name1_entry_offset = data_offset
                break

        if name1_entry_offset is None:
            print("ERROR: 没有找到 NAME_ID=1 条目")
            return False

        offset = name1_entry_offset & 0x7FFFFFFF
        (char, timestamp, major, minor, num_named, num_lang) = struct.unpack_from(
            '<IIHHHH', resource_dir_data, offset
        )
        offset += 16

        for i in range(num_lang):
            (lang_id, data_rva) = struct.unpack_from('<II', resource_dir_data, offset)
            offset += 8

            data_entry_offset = data_rva & 0x7FFFFFFF
            (data_offset, data_size, data_char) = struct.unpack_from(
                '<III', resource_dir_data, data_entry_offset
            )

            # 计算清单数据在文件中的偏移
            manifest_file_offset = file_offset + data_offset
            print(f"清单数据文件偏移: {manifest_file_offset:#x}, 大小: {data_size}")

            # 替换清单数据
            # 始终更新数据条目中的大小，使其与实际写入的数据一致
            struct.pack_into('<I', resource_dir_data, data_entry_offset + 4, len(UAC_MANIFEST_XML))
            # 写回资源目录
            pe_data[file_offset:file_offset + resource_dir_size] = resource_dir_data

            # 写入 UAC 清单
            pe_data[manifest_file_offset:manifest_file_offset + len(UAC_MANIFEST_XML)] = UAC_MANIFEST_XML
            print(f"已写入 UAC 清单 ({len(UAC_MANIFEST_XML)} bytes)")

        # 写回 PE 文件
        f.seek(0)
        f.write(pe_data)
        f.truncate()

    print("SUCCESS: UAC 清单已嵌入")
    return True


def main():
    if len(sys.argv) < 2:
        print(f"用法: python {sys.argv[0]} <exe_path>")
        sys.exit(1)

    exe_path = sys.argv[1]
    if not add_uac_manifest(exe_path):
        sys.exit(1)


if __name__ == '__main__':
    main()
