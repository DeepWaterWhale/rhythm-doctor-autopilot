"""使用只读 Windows API 定位 Rhythm Doctor 的 Mono 对象。"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import time

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
EXPECTED_EXE = r"C:\Program Files (x86)\Steam\steamapps\common\Rhythm Doctor\Rhythm Doctor.exe"
K32 = C.WinDLL("kernel32", use_last_error=True)


class MBI(C.Structure):
    _fields_ = [("BaseAddress", C.c_void_p), ("AllocationBase", C.c_void_p),
                ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD),
                ("RegionSize", C.c_size_t), ("State", W.DWORD),
                ("Protect", W.DWORD), ("Type", W.DWORD)]


K32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
K32.OpenProcess.restype = W.HANDLE
K32.CloseHandle.argtypes = [W.HANDLE]
K32.CloseHandle.restype = W.BOOL
K32.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
K32.ReadProcessMemory.restype = W.BOOL
K32.VirtualQueryEx.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(MBI), C.c_size_t]
K32.VirtualQueryEx.restype = C.c_size_t
K32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
K32.QueryFullProcessImageNameW.restype = W.BOOL


class Reader:
    def __init__(self, pid: int):
        self.pid = pid
        self.handle = K32.OpenProcess(0x410, False, pid)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        text = C.create_unicode_buffer(32768)
        size = W.DWORD(len(text))
        if not K32.QueryFullProcessImageNameW(self.handle, 0, text, C.byref(size)):
            self.close()
            raise C.WinError(C.get_last_error())
        if text.value.casefold() != EXPECTED_EXE.casefold():
            self.close()
            raise RuntimeError(f"目标路径不匹配: {text.value}")
        self.path = text.value

    def close(self):
        if self.handle:
            K32.CloseHandle(self.handle)
            self.handle = None

    def read(self, address: int, size: int, partial=False) -> bytes:
        if not 0 < address < 0x800000000000 or not 0 <= size <= 0x1000000:
            raise ValueError(f"无效读取范围: {address:#x}, {size}")
        buffer = C.create_string_buffer(size)
        copied = C.c_size_t()
        ok = K32.ReadProcessMemory(self.handle, address, buffer, size, C.byref(copied))
        if not partial and (not ok or copied.value != size):
            raise OSError(C.get_last_error(), f"读取 {address:#x}: {copied.value}/{size}")
        return buffer.raw[:copied.value]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def i32(self, address):
        return struct.unpack("<i", self.read(address, 4))[0]

    def cstring(self, address, limit=256):
        if not address:
            return None
        return self.read(address, limit, partial=True).split(b"\0", 1)[0].decode("utf-8", "replace")

    def managed_string(self, address, limit=1024):
        if not address:
            return None
        length = self.i32(address + 0x10)
        if not 0 <= length <= limit:
            raise ValueError(f"无效 MonoString 长度: {length}")
        return self.read(address + 0x14, length * 2).decode("utf-16-le", "replace")

    def regions(self, include_images=False):
        address = 0
        while address < 0x7FFFFFFFFFFF:
            mbi = MBI()
            if not K32.VirtualQueryEx(self.handle, address, C.byref(mbi), C.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress or 0, mbi.RegionSize
            if mbi.State == 0x1000 and not mbi.Protect & 0x100 and mbi.Protect & 0xFF in (2, 4, 8, 0x20, 0x40, 0x80):
                if include_images or mbi.Type != 0x1000000:
                    yield {"base": base, "size": size, "protect": mbi.Protect, "type": mbi.Type}
            next_address = base + size
            if next_address <= address:
                break
            address = next_address

    def scan(self, patterns: list[bytes], alignment=1, max_hits=10000):
        if not patterns:
            return {"hits": [], "bytes_read": 0}
        matcher = re.compile(b"|".join(re.escape(p) for p in sorted(set(patterns), key=len, reverse=True)))
        hits = []
        read_bytes = 0
        failed_bytes = 0
        start = time.perf_counter()
        carry_length = max(map(len, patterns)) - 1
        regions = list(self.regions())
        for region in regions:
            end = region["base"] + region["size"]
            address = region["base"]
            while address < end:
                size = min(0x400000, end - address)
                data = self.read(address, size, partial=True)
                read_bytes += len(data)
                failed_bytes += size - len(data)
                for match in matcher.finditer(data):
                    found = address + match.start()
                    if found % alignment == 0:
                        hits.append({"address": found, "address_hex": hex(found), "pattern_hex": match.group().hex(), "region_type": region["type"]})
                        if len(hits) >= max_hits:
                            return {"hits": hits, "bytes_read": read_bytes, "unread_bytes": failed_bytes, "seconds": time.perf_counter() - start, "truncated": True}
                if address + size >= end:
                    break
                address += max(1, size - carry_length)
        unique = {item["address"]: item for item in hits}
        return {"hits": list(unique.values()), "bytes_read": read_bytes, "unread_bytes": failed_bytes, "regions": len(regions), "seconds": time.perf_counter() - start, "truncated": False}


def save(name, value):
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["names", "name-refs", "refs", "dump"])
    parser.add_argument("--pid", type=int, default=27896)
    parser.add_argument("--addresses", nargs="*", default=[])
    parser.add_argument("--size", type=lambda x: int(x, 0), default=0x100)
    args = parser.parse_args()
    reader = Reader(args.pid)
    try:
        if args.command == "names":
            result = reader.scan([b"scnGame\0"])
            result.update(pid=args.pid, process_path=reader.path, observation="scnGame UTF-8 名称地址，尚非对象地址")
            save("scngame-name-locations.json", result)
        elif args.command == "name-refs":
            names = json.loads((ROOT / "scngame-name-locations.json").read_text(encoding="utf-8"))
            if names["pid"] != args.pid:
                raise ValueError("名称扫描属于不同进程")
            result = reader.scan([struct.pack("<Q", x["address"]) for x in names["hits"]], alignment=8)
            result.update(pid=args.pid, observation="指向名称的指针，尚待 MonoClass 布局验证")
            save("scngame-name-references.json", result)
        elif args.command == "refs":
            addresses = [int(x, 0) for x in args.addresses]
            result = reader.scan([struct.pack("<Q", x) for x in addresses], alignment=8)
            result.update(pid=args.pid, targets=[hex(x) for x in addresses])
            save("scngame-pointer-references.json", result)
        else:
            result = []
            for value in args.addresses:
                address = int(value, 0)
                data = reader.read(address, args.size)
                result.append({"address": hex(address), "words": [{"offset": hex(i), "value": hex(struct.unpack_from("<Q", data, i)[0])} for i in range(0, len(data) - 7, 8)]})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        reader.close()


if __name__ == "__main__":
    main()
