"""只读解析 PE/CLI TypeDef 的 #Strings 名称偏移，不加载或执行程序集。"""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def read_type_name_offsets(path, names, anchor="scnGame"):
    data = Path(path).read_bytes()
    u16 = lambda at: struct.unpack_from("<H", data, at)[0]
    u32 = lambda at: struct.unpack_from("<I", data, at)[0]
    if data[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    pe = u32(0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("PE 签名无效")
    section_count = u16(pe + 6)
    optional = pe + 24
    section_table = optional + u16(pe + 20)
    magic = u16(optional)
    if magic not in (0x10B, 0x20B):
        raise ValueError("不支持的 PE optional header")
    directories = optional + (112 if magic == 0x20B else 96)
    sections = [struct.unpack_from("<IIII", data, section_table + i * 40 + 8)
                for i in range(section_count)]

    def file_offset(rva):
        for virtual_size, virtual_address, raw_size, raw_pointer in sections:
            if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
                relative = rva - virtual_address
                if relative >= raw_size:
                    raise ValueError("RVA 没有文件内容")
                return raw_pointer + relative
        raise ValueError(f"RVA 未映射: {rva:#x}")

    cli = file_offset(u32(directories + 14 * 8))
    metadata = file_offset(u32(cli + 8))
    if data[metadata:metadata + 4] != b"BSJB":
        raise ValueError("CLI metadata 签名无效")
    position = (metadata + 16 + u32(metadata + 12) + 3) & ~3
    stream_count = u16(position + 2)
    position += 4
    streams = {}
    for _ in range(stream_count):
        offset, size = struct.unpack_from("<II", data, position)
        end = data.index(0, position + 8)
        stream_name = data[position + 8:end].decode("ascii")
        streams[stream_name] = (metadata + offset, size)
        position = (end + 4) & ~3
    table_stream = streams.get("#~", streams.get("#-"))
    if table_stream is None or "#Strings" not in streams:
        raise ValueError("缺少 tables 或 #Strings stream")
    tables = table_stream[0]
    heap_sizes = data[tables + 6]
    string_size = 4 if heap_sizes & 1 else 2
    guid_size = 4 if heap_sizes & 2 else 2
    valid = struct.unpack_from("<Q", data, tables + 8)[0]
    position = tables + 24
    rows = {}
    for table_id in range(64):
        if valid >> table_id & 1:
            rows[table_id] = u32(position)
            position += 4

    def coded_size(table_ids, tag_bits):
        return 4 if max(rows.get(i, 0) for i in table_ids) >= 1 << (16 - tag_bits) else 2

    # TypeDef 前只有 Module 和 TypeRef 两张表。
    position += rows.get(0, 0) * (2 + string_size + 3 * guid_size)
    position += rows.get(1, 0) * (coded_size((0, 26, 35, 1), 2) + 2 * string_size)
    typedef_size = (4 + 2 * string_size + coded_size((2, 1, 27), 2)
                    + (4 if rows.get(4, 0) >= 65536 else 2)
                    + (4 if rows.get(6, 0) >= 65536 else 2))
    strings = streams["#Strings"][0]
    read_index = u32 if string_size == 4 else u16
    wanted = set(names) | {anchor}
    result = {}
    for rid in range(1, rows.get(2, 0) + 1):
        at = position + (rid - 1) * typedef_size
        name_index = read_index(at + 4)
        namespace_index = read_index(at + 4 + string_size)
        name = data[strings + name_index:data.index(0, strings + name_index)].decode("utf-8")
        namespace = data[strings + namespace_index:data.index(0, strings + namespace_index)].decode("utf-8")
        full_name = namespace + "." + name if namespace else name
        if full_name in wanted:
            if full_name in result:
                raise ValueError(f"TypeDef 名称不唯一: {full_name}")
            result[full_name] = {"name": name, "namespace": namespace,
                                 "token": f"0x{0x02000000 + rid:08x}",
                                 "name_offset": name_index}
    if wanted - result.keys():
        raise ValueError(f"未找到类型: {sorted(wanted - result.keys())}")
    for item in result.values():
        item["delta_from_anchor"] = item["name_offset"] - result[anchor]["name_offset"]
    return {"assembly_sha256": hashlib.sha256(data).hexdigest(), "anchor": anchor, "types": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assembly")
    parser.add_argument("--types", nargs="+", default=["GC", "RDCalibration", "scrConductor"])
    parser.add_argument("--anchor", default="scnGame")
    args = parser.parse_args()
    print(json.dumps(read_type_name_offsets(args.assembly, args.types, args.anchor), ensure_ascii=False, indent=2))
