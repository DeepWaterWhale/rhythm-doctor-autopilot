"""读取 Steam 的成就缓存并执行游戏原生隐藏操作；不调用成就写入接口。"""
import importlib.util
import json
from pathlib import Path
import struct
import time
import winreg

ROOT = Path(__file__).resolve().parent
SPECIAL = ("LevelSelectHeadache", "RhythmDogtor", "Eavesdropping", "LevelLuckyBreakCoopPerfect",
           "LevelRhythmWeightlifterAny", "LevelSelectSpam")
_steam_class = None
_status_cache = None

def binary_kv(path):
    raw = Path(path).read_bytes()
    at = 0
    def string():
        nonlocal at
        end = raw.index(b"\0", at)
        value = raw[at:end].decode("utf-8")
        at = end + 1
        return value
    def record():
        nonlocal at
        values = {}
        while at < len(raw):
            kind = raw[at]
            at += 1
            if kind == 8:
                return values
            name = string()
            if kind == 0:
                value = record()
            elif kind == 1:
                value = string()
            elif kind in (2, 3, 7):
                fmt = {2: "<I", 3: "<f", 7: "<Q"}[kind]
                value = struct.unpack_from(fmt, raw, at)[0]
                at += struct.calcsize(fmt)
            else:
                raise ValueError("Steam 缓存字段类型未知")
            if name in values:
                raise ValueError("Steam 缓存字段重复")
            values[name] = value
        raise ValueError("Steam 缓存尚未写完")
    result = record()
    if at != len(raw):
        raise ValueError("Steam 缓存末尾存在未解析内容")
    return result

def steam_class(scene, identity):
    global _steam_class
    if _steam_class and _steam_class[0] == identity:
        return _steam_class[1]
    cache_path = ROOT / "achievement-runtime-class.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached["identity"] == identity and scene.meta(cached["class"])["name"] == "SteamIntegration":
            _steam_class = identity, cached["class"]
            return cached["class"]
    spec = importlib.util.spec_from_file_location("achievement_types", ROOT / "managed-type-name-offsets.py")
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    r = scene.reader
    assembly = Path(r.path).with_name("Rhythm Doctor_Data") / "Managed" / "Assembly-CSharp.dll"
    offset = parser.read_type_name_offsets(assembly, ["SteamIntegration"])["types"]["SteamIntegration"]
    anchor = scene.layout["scnGame"]
    image = r.u64(anchor + 0x40)
    name_pointer = r.u64(anchor + 0x48) + offset["delta_from_anchor"]
    refs = r.scan([struct.pack("<Q", name_pointer)], alignment=8)
    if refs.get("truncated"):
        raise RuntimeError("Steam 类型扫描截断")
    found = set()
    for ref in refs["hits"]:
        cls = ref["address"] - 0x48
        try:
            if (r.u64(cls + 0x40) == image and scene.meta(cls)["name"] == "SteamIntegration"
                    and int(scene.meta(cls)["token"], 16) == int(offset["token"], 16)):
                found.add(cls)
        except (OSError, ValueError, RuntimeError):
            continue
    if len(found) != 1:
        raise RuntimeError("未唯一定位游戏 Steam 身份")
    cls = found.pop()
    cache_path.write_text(json.dumps({"identity": identity, "class": cls}), encoding="utf-8")
    _steam_class = identity, cls
    return cls

def read_status(scene, identity):
    global _status_cache
    cls = steam_class(scene, identity)
    if not scene.get(None, "initialized", cls):
        raise RuntimeError("游戏尚未连接 Steam")
    steam_id = scene.get(None, "userSteamID", cls)
    account = steam_id & 0xFFFFFFFF
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
        steam_path = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    stats = steam_path / "appcache" / "stats"
    cache_path = stats / f"UserGameStats_{account}_774181.bin"
    schema_path = stats / "UserGameStatsSchema_774181.bin"
    version = account, cache_path.stat().st_mtime_ns, schema_path.stat().st_mtime_ns
    if _status_cache and _status_cache[0] == version:
        return _status_cache[1]
    schema = binary_kv(schema_path)["774181"]["stats"]
    cache = binary_kv(cache_path)["cache"]
    rows = {}
    for group, definition in schema.items():
        if definition.get("type") != "ACHIEVEMENTS":
            continue
        flags = cache.get(group, {}).get("data", 0)
        for bit, entry in definition.get("bits", {}).items():
            display = entry["display"]
            name = display["name"]
            rows[entry["name"]] = {"name": name.get("schinese", name.get("english")) if isinstance(name, dict) else name,
                                    "achieved": bool(flags & (1 << int(bit)))}
    if not set(SPECIAL) <= rows.keys():
        raise RuntimeError("Steam 成就列表与当前适配不符")
    result = {"account": account, "total": len(rows), "achieved": sum(row["achieved"] for row in rows.values()),
              "rows": rows, "cache_mtime_ns": version[1]}
    _status_cache = version, result
    return result

def execute(keyboard, action, stop_requested=lambda: False):
    def check():
        if stop_requested():
            raise RuntimeError("成就操作已取消")
        if not keyboard.is_foreground():
            raise RuntimeError("游戏失去前台焦点，停止成就操作")
    def wait(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            check()
            time.sleep(.02)
    check()
    if action == "achievement_headache":
        check()
        keyboard.set_key("right", True)
        try:
            wait(5.4)
        finally:
            keyboard.set_key("right", False)
    elif action == "achievement_dog_code":
        for key in ["right", "left", "right", "right", "left", "right", "left", "left"] * 2:
            wait(.12)
            check()
            keyboard.tap_key(key)
    else:
        check()
        keyboard.tap_key(action)
