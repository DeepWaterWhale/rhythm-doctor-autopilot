"""只读查看已知 Mono 对象或类型的字段，不调用目标进程函数。"""
import argparse
import importlib.util
import json
from pathlib import Path
import struct
import sys

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rd_sample", ROOT / "sample-rhythm-doctor-state.py")
sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sample)


def field_count(reader, cls):
    kind = reader.read(cls + 0x1B, 1)[0]
    if kind in (1, 2):
        return reader.u32(cls + 0x100)
    if kind == 3:
        return field_count(reader, reader.u64(reader.u64(cls + 0xF0)))
    raise ValueError(f"不支持 MonoClass kind={kind}")


def metadata(reader, cls):
    return sample.ins.inspect_class(reader, cls, field_count(reader, cls))


def object_class(reader, obj):
    cls = reader.u64(reader.u64(obj))
    return cls, reader.cstring(reader.u64(cls + 0x48))


def inspect(reader, cls, obj=None):
    info = metadata(reader, cls)
    result = {key: value for key, value in info.items() if key != "fields"}
    result["object"] = hex(obj) if obj else None
    result["fields"] = []
    for field in info["fields"]:
        out = dict(field)
        if field["literal"] or field["offset"] < 0:
            result["fields"].append(out)
            continue
        try:
            base = sample.static_base(reader, info) if field["static"] else obj
            if not base:
                result["fields"].append(out)
                continue
            at = base + field["offset"]
            kind = field["mono_type"]
            if kind == 2:
                out["value"] = reader.read(at, 1)[0]
            elif kind in (8, 9):
                out["value"] = reader.i32(at)
            elif kind == 12:
                out["value"] = struct.unpack("<f", reader.read(at, 4))[0]
            elif kind == 13:
                out["value"] = struct.unpack("<d", reader.read(at, 8))[0]
            elif kind in (10, 11):
                out["value"] = reader.u64(at)
            elif kind in (14, 18, 20, 21, 28, 29):
                value = reader.u64(at)
                out["pointer"] = hex(value)
                if value:
                    subcls, name = object_class(reader, value)
                    out["object_class"] = name
                    out["object_class_address"] = hex(subcls)
                    if kind == 14:
                        out["value"] = reader.managed_string(value, limit=65536)[:200]
            elif kind == 17:
                # 此处仅输出原始前四字节，不能把任意 valuetype 当作 enum。
                out["raw_i32"] = reader.i32(at)
        except Exception as error:
            out["error"] = str(error)
        result["fields"].append(out)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=27896)
    parser.add_argument("--objects", nargs="*", default=[])
    parser.add_argument("--classes", nargs="*", default=[])
    parser.add_argument("--current-scene", action="store_true")
    parser.add_argument("--output", default="mono-object-inspection.json")
    args = parser.parse_args()
    reader = sample.ins.mem.Reader(args.pid)
    try:
        objects = [int(x, 0) for x in args.objects]
        if args.current_scene:
            objects.append(reader.u64(0x2650D338E38))
        results = []
        for obj in objects:
            try:
                cls, _ = object_class(reader, obj)
                results.append(inspect(reader, cls, obj))
            except Exception as error:
                results.append({"object": hex(obj), "error": str(error)})
        for value in args.classes:
            try:
                results.append(inspect(reader, int(value, 0)))
            except Exception as error:
                results.append({"class": value, "error": str(error)})
        sample.ins.mem.save(args.output, results)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        reader.close()


if __name__ == "__main__":
    main()
