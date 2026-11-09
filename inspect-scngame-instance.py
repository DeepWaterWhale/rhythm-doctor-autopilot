"""根据本机 Mono 导出函数验证的布局解析 scnGame 元数据和运行时实例。"""
import argparse
import importlib.util
import json
from pathlib import Path
import struct
import sys

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rd_memory", ROOT / "rhythm-doctor-memory-probe.py")
mem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem)


def inspect_class(reader, address, count):
    result = {"address": hex(address), "name": reader.cstring(reader.u64(address + 0x48)),
              "namespace": reader.cstring(reader.u64(address + 0x50)),
              "token": hex(reader.u32(address + 0x58)), "instance_size": reader.u32(address + 0x1C)}
    fields = reader.u64(address + 0x98)
    result["fields_address"] = hex(fields)
    result["fields"] = []
    for i in range(count):
        field = fields + i * 0x20
        data = reader.read(field, 0x20)
        type_ptr, name_ptr, parent, offset = struct.unpack_from("<QQQi", data)
        if parent != address:
            raise RuntimeError(f"字段所属类型不匹配: {field:#x}")
        type_data = reader.read(type_ptr, 12)
        flags = struct.unpack_from("<H", type_data, 8)[0]
        result["fields"].append({"name": reader.cstring(name_ptr), "offset": offset,
                                 "flags": flags, "static": bool(flags & 0x10), "literal": bool(flags & 0x40),
                                 "mono_type": type_data[10], "type_data": hex(struct.unpack_from("<Q", type_data)[0])})
    runtime = reader.u64(address + 0xD0)
    result["runtime_info"] = hex(runtime)
    if runtime:
        max_domain = struct.unpack("<H", reader.read(runtime, 2))[0]
        result["max_domain"] = max_domain
        if max_domain > 32:
            raise RuntimeError("domain 数量异常")
        result["vtables"] = []
        for i in range(max_domain + 1):
            vtable = reader.u64(runtime + 8 + i * 8)
            if vtable:
                result["vtables"].append({"domain_id": i, "address": hex(vtable), "class_pointer": hex(reader.u64(vtable)), "class_matches": reader.u64(vtable) == address})
    return result


def inspect_object(reader, metadata, address):
    vtable = reader.u64(address)
    actual_class = reader.u64(vtable)
    if actual_class != int(metadata["address"], 16):
        raise RuntimeError("对象 vtable 的类指针不匹配")
    data = reader.read(address, metadata["instance_size"])
    wanted = {"_gameState", "paused", "failedLevel", "ignoreInput", "levelFinishedLoading", "levelIdentifier", "beats", "currentLevel", "gameStateWatcher", "p1WentDownTimeStamp", "p2WentDownTimeStamp", "loadedFromRestart", "loadedFromPersistence", "beginLevel"}
    values = {}
    for field in metadata["fields"]:
        if field["static"] or field["name"] not in wanted:
            continue
        offset, kind = field["offset"], field["mono_type"]
        if kind == 2:
            value = data[offset]
        elif kind == 8 or kind == 0x11:
            value = struct.unpack_from("<i", data, offset)[0]
        elif kind == 0xC:
            value = struct.unpack_from("<f", data, offset)[0]
        else:
            value = hex(struct.unpack_from("<Q", data, offset)[0])
            if kind == 0xE:
                value = reader.managed_string(int(value, 16))
        values[field["name"]] = {"offset": hex(offset), "mono_type": kind, "value": value}
    return {"address": hex(address), "vtable": hex(vtable), "class_verified": True, "values": values}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=27896)
    parser.add_argument("--class-address", type=lambda x: int(x, 0), default=0x265F6EBFD00)
    parser.add_argument("--field-count", type=int, default=171)
    parser.add_argument("--objects", nargs="*", default=[])
    parser.add_argument("--output", default="scngame-runtime-metadata.json")
    args = parser.parse_args()
    reader = mem.Reader(args.pid)
    try:
        metadata = inspect_class(reader, args.class_address, args.field_count)
        metadata["pid"] = args.pid
        if args.objects:
            result = {"pid": args.pid, "class_address": metadata["address"], "objects": []}
            for value in args.objects:
                try:
                    result["objects"].append(inspect_object(reader, metadata, int(value, 0)))
                except Exception as error:
                    result["objects"].append({"address": value, "error": str(error)})
        else:
            result = metadata
        mem.save(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        reader.close()


if __name__ == "__main__":
    main()
