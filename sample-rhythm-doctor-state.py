"""通过 scnBase._instance 读取当前 scnGame，并可执行一次获准的空格输入实验。"""
import argparse
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("rd_inspect", ROOT / "inspect-scngame-instance.py")
ins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ins)
STATE_NAMES = {0: "PreStart", 1: "SpacePressedPreStart", 2: "HandmodePreCutscene", 3: "Handmode", 4: "Cutscene", 5: "CutsceneSkippable", 6: "RankShown"}


def class_metadata(reader, address):
    kind = reader.read(address + 0x1B, 1)[0]
    if kind not in (1, 2):
        raise ValueError(f"当前探针不解析 MonoClass kind={kind}")
    count = reader.u32(address + 0x100)
    if not 0 <= count <= 4096:
        raise ValueError("字段数量异常")
    return ins.inspect_class(reader, address, count)


def static_base(reader, metadata):
    vtable = int(metadata["vtables"][0]["address"], 16)
    if not reader.read(vtable + 0x30, 1)[0] & 4:
        raise ValueError("当前类型没有静态字段区")
    vtable_size = reader.i32(int(metadata["address"], 16) + 0x5C)
    if not 0 <= vtable_size < 4096:
        raise ValueError("vtable 大小异常")
    return reader.u64(vtable + 0x48 + vtable_size * 8)


def resolve(reader, game_class=None):
    if game_class is None:
        saved = json.loads((ROOT / "scngame-runtime-metadata.json").read_text(encoding="utf-8"))
        if saved["pid"] != reader.pid:
            raise ValueError("已保存地址属于不同 PID，需要重新定位")
        game_class = int(saved["address"], 16)
    game_metadata = class_metadata(reader, game_class)
    if game_metadata["name"] != "scnGame" or game_metadata["token"] != "0x20001c9":
        raise ValueError("scnGame 类型身份不匹配")
    address = game_class
    ancestry = []
    for _ in range(16):
        name = reader.cstring(reader.u64(address + 0x48))
        ancestry.append({"class": name, "address": hex(address)})
        if name == "scnBase":
            base_metadata = class_metadata(reader, address)
            instance_field = next(f for f in base_metadata["fields"] if f["name"] == "_instance" and f["static"])
            storage = static_base(reader, base_metadata)
            slot = storage + instance_field["offset"]
            instance = reader.u64(slot)
            if not instance or reader.u64(reader.u64(instance)) != game_class:
                raise ValueError("scnBase._instance 当前未指向 scnGame")
            return {"metadata": game_metadata, "instance": instance, "instance_slot": slot,
                    "game_static_base": static_base(reader, game_metadata), "ancestry": ancestry}
        address = reader.u64(address + 0x30)
        if not address:
            break
    raise ValueError("继承链中未找到 scnBase")


def snapshot(reader, context, start):
    current_instance = reader.u64(context["instance_slot"])
    if current_instance != context["instance"]:
        raise RuntimeError("游玩场景实例已变化，停止使用旧地址")
    result = ins.inspect_object(reader, context["metadata"], current_instance)
    values = {key: item["value"] for key, item in result["values"].items()}
    for key in ("paused", "failedLevel", "ignoreInput", "levelFinishedLoading"):
        if values[key] not in (0, 1):
            raise ValueError(f"布尔字段无效: {key}")
        values[key] = bool(values[key])
    values["game_state_name"] = STATE_NAMES.get(values["_gameState"], "Unknown")
    static_fields = {f["name"]: f for f in context["metadata"]["fields"] if f["static"]}
    for name in ("timeReference", "lastTimeReference", "framesElapsed", "levelSpeed"):
        field = static_fields[name]
        data = reader.read(context["game_static_base"] + field["offset"], 4)
        values[name] = struct.unpack("<f" if field["mono_type"] == 12 else "<i", data)[0]
    beats = int(values["beats"], 16)
    # List<T> 的 _size 偏移在本机运行时元数据中单独验证后才赋予含义。
    return {"elapsed_seconds": time.perf_counter() - start, "utc": datetime.now(timezone.utc).isoformat(),
            "instance": result["address"], "values": values}


def send_space(pid, observation_callback):
    user32 = C.WinDLL("user32", use_last_error=True)
    class KEYBDINPUT(C.Structure):
        _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]
    class MOUSEINPUT(C.Structure):
        _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]
    class HARDWAREINPUT(C.Structure):
        _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]
    class UNION(C.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]
    class INPUT(C.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", W.DWORD), ("u", UNION)]
    user32.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
    user32.SendInput.restype = W.UINT
    user32.GetForegroundWindow.restype = W.HWND
    user32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    user32.GetWindowThreadProcessId.restype = W.DWORD
    user32.IsWindowVisible.argtypes = [W.HWND]
    user32.IsWindowVisible.restype = W.BOOL
    user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
    user32.SetForegroundWindow.argtypes = [W.HWND]
    user32.SetForegroundWindow.restype = W.BOOL
    user32.BringWindowToTop.argtypes = [W.HWND]
    user32.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
    user32.AttachThreadInput.restype = W.BOOL
    user32.GetAsyncKeyState.argtypes = [C.c_int]
    user32.GetAsyncKeyState.restype = C.c_short
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, W.LPARAM]
    candidates = []
    @callback_type
    def enum_window(hwnd, _):
        owner = W.DWORD()
        user32.GetWindowThreadProcessId(hwnd, C.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = C.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value == "Rhythm Doctor":
                candidates.append(hwnd)
        return True
    user32.EnumWindows(enum_window, 0)
    if len(candidates) != 1:
        raise RuntimeError(f"目标游戏窗口数量应为 1，实际为 {len(candidates)}")
    hwnd = candidates[0]
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    if user32.GetForegroundWindow() != hwnd:
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        current_thread = ins.mem.K32.GetCurrentThreadId()
        attached = user32.AttachThreadInput(current_thread, foreground_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        time.sleep(0.1)
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("游戏窗口未获得前台焦点，没有发送按键")
    if user32.GetAsyncKeyState(0x20) & 0x8000:
        raise RuntimeError("空格当前已被按住，没有发送按键")
    down = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=0x39, dwFlags=0x8))
    up = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=0x39, dwFlags=0xA))
    if C.sizeof(INPUT) != 40:
        raise RuntimeError("INPUT 结构大小不符合 x64 布局")
    sent_down = user32.SendInput(1, C.byref(down), C.sizeof(INPUT))
    if sent_down != 1:
        raise C.WinError(C.get_last_error())
    try:
        time.sleep(0.04)
        observation_callback()
        time.sleep(0.04)
    finally:
        sent_up = user32.SendInput(1, C.byref(up), C.sizeof(INPUT))
    if sent_up != 1:
        raise C.WinError(C.get_last_error())
    return {"window": hex(hwnd), "foreground_verified": True, "scan_code": "0x39", "keydown_events_sent": sent_down, "keyup_events_sent": sent_up, "requested_hold_ms": 80}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=27896)
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--send-space", action="store_true")
    parser.add_argument("--output", default="scngame-current-state.json")
    args = parser.parse_args()
    reader = ins.mem.Reader(args.pid)
    try:
        context = resolve(reader)
        started = time.perf_counter()
        samples = []
        def capture():
            samples.append(snapshot(reader, context, started))
        capture()
        result = {"pid": args.pid, "instance": hex(context["instance"]), "instance_slot": hex(context["instance_slot"]),
                  "game_static_base": hex(context["game_static_base"]), "ancestry": context["ancestry"], "samples": samples}
        if args.send_space:
            if samples[0]["values"]["_gameState"] != 0 or samples[0]["values"]["paused"]:
                raise RuntimeError("当前状态不是未暂停的 PreStart，未发送空格")
            # 先保存输入前证据，避免后续读取失败丢失记录。
            ins.mem.save("scngame-before-space.json", result)
            result["input"] = send_space(args.pid, capture)
            ins.mem.save(args.output, result)
        until = time.perf_counter() + args.seconds
        while time.perf_counter() < until:
            capture()
            time.sleep(0.05)
        ins.mem.save(args.output, result)
        transitions = []
        previous = None
        for sample in samples:
            key = (sample["values"]["_gameState"], sample["values"]["paused"], sample["values"]["failedLevel"])
            if key != previous:
                transitions.append(sample)
                previous = key
        print(json.dumps({key: value for key, value in result.items() if key != "samples"} | {
            "sample_count": len(samples), "transitions": transitions, "last_sample": samples[-1]}, ensure_ascii=False, indent=2))
    finally:
        reader.close()


if __name__ == "__main__":
    main()
