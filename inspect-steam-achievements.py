"""只读解析此游戏的 Steam 成就 schema 和本地统计缓存。"""
import argparse
import json
from pathlib import Path
import re
import struct
import sys
import winreg

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent

def default_stats_directory():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
        return Path(winreg.QueryValueEx(key, "SteamPath")[0]) / "appcache" / "stats"


def select_cache(stats, account_id=None):
    if account_id is not None:
        if not 0 < account_id <= 0xFFFFFFFF:
            raise ValueError("account-id 必须是有效的 32 位 Steam 账户编号")
        path = stats / f"UserGameStats_{account_id}_774181.bin"
        if not path.is_file():
            raise ValueError("指定账户没有此游戏的本地成就缓存")
        return path
    candidates = [path for path in stats.glob("UserGameStats_*_774181.bin")
                  if path.is_file() and re.fullmatch(r"UserGameStats_\d+_774181\.bin", path.name)]
    if len(candidates) != 1:
        raise ValueError("未找到唯一成就缓存；请用 --account-id 指定账户")
    return candidates[0]

def binary_kv(path):
    raw = path.read_bytes()
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
                raise ValueError((path, at, kind))
            if name in values:
                raise ValueError("Duplicate key: " + name)
            values[name] = value
        raise ValueError("Missing end marker")
    result = record()
    assert at == len(raw), (at, len(raw))
    return result

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, help="多个本地账户时显式指定 Steam 账户编号")
    parser.add_argument("--stats-dir", type=Path, help="Steam 成就缓存目录，默认从注册表读取")
    args = parser.parse_args(argv)
    stats = args.stats_dir if args.stats_dir is not None else default_stats_directory()
    try:
        cache_path = select_cache(stats, args.account_id)
    except ValueError as error:
        parser.error(str(error))
    schema = binary_kv(stats / "UserGameStatsSchema_774181.bin")["774181"]["stats"]
    cache = binary_kv(cache_path)["cache"]
    achievements = []
    for group, definition in schema.items():
        if definition.get("type") != "ACHIEVEMENTS":
            continue
        flags = cache.get(group, {}).get("data", 0)
        for bit, entry in definition.get("bits", {}).items():
            display = entry["display"]
            def localized(value):
                return value.get("schinese", value.get("english")) if isinstance(value, dict) else value
            achievements.append({"id": entry["name"], "name": localized(display["name"]),
                                 "description": localized(display.get("desc", "")),
                                 "hidden": display.get("hidden"), "group": group, "bit": int(bit),
                                 "achieved": bool(flags & (1 << int(bit))),
                                 "unlocked": cache.get(group, {}).get("AchievementTimes", {}).get(bit)})
    result = {"source": str(cache_path), "mtime": cache_path.stat().st_mtime, "total": len(achievements),
              "achieved": sum(row["achieved"] for row in achievements),
              "missing": [row for row in achievements if not row["achieved"]], "achievements": achievements}
    (ROOT / "steam-achievement-cache-inspection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "achievements"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
