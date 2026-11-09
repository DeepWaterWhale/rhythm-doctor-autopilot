"""读取当前存档成绩，选择未达 S 的已解锁关卡；只使用原生导航按键。"""
import importlib.util
import json
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parent
def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

nav = load("platinum_navigation", "rhythm-doctor-navigation.py")
_classes_cache = None


def reached_s(rank):
    return rank in (5, 15, "S", "S+")


def dictionary(scene, obj, string_keys=False, string_values=False):
    reader = scene.reader
    version = scene.get(obj, "_version")
    array, count = scene.get(obj, "_entries"), scene.get(obj, "_count")
    if not 0 <= count <= 10000:
        raise ValueError("存档字典长度异常")
    if count == 0:
        return {}
    meta = scene.meta(reader.u64(scene.cls(array)))
    offsets = {f["name"]: f["offset"] - 16 for f in meta["fields"]}
    if meta["instance_size"] - 16 != 24 or offsets != {"hashCode": 0, "next": 4, "key": 8, "value": 16}:
        raise ValueError("存档字典布局不匹配")
    if reader.u64(array + 24) < count:
        raise ValueError("存档字典容量异常")
    data = reader.read(array + 32, count * 24)
    result = {}
    for i in range(count):
        hash_code, _, key, value = struct.unpack_from("<iiQQ", data, i*24)
        if hash_code < 0:
            continue
        key = reader.managed_string(key, limit=65536) if string_keys else key & 0xFFFFFFFF
        if string_values:
            value = reader.managed_string(value, limit=65536)
        result[key] = value
    if version != scene.get(obj, "_version") or array != scene.get(obj, "_entries"):
        raise ValueError("读取期间存档字典发生变化")
    return result


def save_classes(scene, identity):
    global _classes_cache
    if _classes_cache and _classes_cache[0] == identity:
        return _classes_cache[1]
    path = ROOT / "active-save-classes.json"
    names = {"Persistence", "PlayerPrefsJson"}
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("identity") == identity and set(cached.get("classes", {})) == names:
            classes = cached["classes"]
            if all(scene.meta(at)["name"] == name and scene.meta(at)["namespace"] == "" for name, at in classes.items()):
                _classes_cache = identity, classes
                return classes
    parser = load("platinum_names", "managed-type-name-offsets.py")
    reader = scene.reader
    assembly = Path(reader.path).with_name("Rhythm Doctor_Data") / "Managed" / "Assembly-CSharp.dll"
    offsets = parser.read_type_name_offsets(assembly, sorted(names))["types"]
    anchor = scene.layout["scnGame"]
    image, anchor_name = reader.u64(anchor+0x40), reader.u64(anchor+0x48)
    wanted = {anchor_name+offsets[name]["delta_from_anchor"]: name for name in names}
    refs = reader.scan([struct.pack("<Q", address) for address in wanted], alignment=8)
    if refs.get("truncated"):
        raise RuntimeError("存档类型扫描超过上限")
    classes = {}
    for ref in refs["hits"]:
        at = ref["address"] - 0x48
        try:
            name = wanted.get(reader.u64(at+0x48))
            if not name or reader.u64(at+0x40) != image:
                continue
            if int(scene.meta(at)["token"], 16) != int(offsets[name]["token"], 16):
                continue
            if name in classes and classes[name] != at:
                raise RuntimeError("存档类型不唯一："+name)
            classes[name] = at
        except (OSError, ValueError, IndexError):
            continue
    if set(classes) != names:
        raise RuntimeError("等待游戏存档类型加载")
    path.write_text(json.dumps({"identity": identity, "classes": classes}, indent=2), encoding="utf-8")
    _classes_cache = identity, classes
    return classes


def read_profile(scene, identity):
    classes = save_classes(scene, identity)
    slot = scene.get(None, "currentSlotIndex", classes["Persistence"])
    if slot not in (0, 1, 2):
        raise RuntimeError("当前存档尚未选定")
    files = dictionary(scene, scene.get(None, "files", classes["PlayerPrefsJson"]))
    paths = dictionary(scene, scene.get(None, "filesPath", classes["PlayerPrefsJson"]), string_values=True)
    prefs = files[slot+1]
    if scene.get(prefs, "fileType") != slot+1:
        raise RuntimeError("当前存档对象不匹配")
    values = dictionary(scene, scene.get(prefs, "dict"), string_keys=True)
    ranks = {}
    for key, value in values.items():
        if key.startswith("Level_") and key.endswith("_rank"):
            if not value or scene.meta(scene.cls(value))["name"] != "String":
                raise ValueError("存档评价不是字符串")
            ranks[key[6:-5]] = scene.reader.managed_string(value, limit=64)
    if (scene.get(None, "currentSlotIndex", classes["Persistence"]) != slot
            or dictionary(scene, scene.get(None, "files", classes["PlayerPrefsJson"])).get(slot+1) != prefs):
        raise RuntimeError("读取过程中切换了存档")
    metadata = dictionary(scene, scene.get(None, "levelMetadata", scene.layout["GC"]))
    levels = {nav.LEVEL_NAMES[key]: {"type": scene.get(value, "type"), "id": scene.get(value, "id")}
              for key, value in metadata.items() if 0 <= key < len(nav.LEVEL_NAMES)}
    return {"slot": slot, "path": paths[slot+1], "ranks": ranks, "levels": levels}


def flow_order(identifier, difficulty):
    match = re.fullmatch(r"(\d+)-(\d+|X\d*|B\d+)", identifier)
    if match:
        chapter, part = int(match[1]), match[2]
        number = int(part) if part.isdigit() else 100 + int(part[1:] or 0) if part.startswith("X") else 200 + int(part[1:])
        return (chapter, number, difficulty, identifier)
    return (100, 0, difficulty, identifier)


def unfinished_levels(snapshot, ranks, levels):
    result = []
    for entity in snapshot["entities"]:
        definition = nav.SEQUENCE.get(entity["id"], {})
        for difficulty_name, level in definition.get("levels", {}).items():
            difficulty = 1 if difficulty_name == "Normal" else 2
            if level not in levels or levels[level]["type"] not in (1, 2, 3, 4, 5, 6):
                continue
            if (not nav.enabled(entity, difficulty) or ranks.get(level, "NotAvailable") == "NotAvailable"
                    or reached_s(ranks.get(level))):
                continue
            result.append({"level": level, "entity": entity["id"], "difficulty": difficulty,
                           "rank": ranks.get(level), "order": flow_order(entity["id"], difficulty),
                           "automatic": level != "RhythmWeightlifter"})
    return sorted(result, key=lambda item: item["order"])


class PlatinumNavigator(nav.Navigator):
    def __init__(self):
        super().__init__()
        self.exit_scene = None
        self.owned_exit = None
        self.exiting_scene = None
        self.exit_deadline = None
        self.pending_levels = []
        self.profile_slot = None
        self.all_complete = False

    def exit_current(self, snapshot, now, reason):
        scene = (snapshot["scene_pointer"], snapshot["level"])
        if scene != self.exit_scene:
            self.exit_scene, self.owned_exit, self.exiting_scene, self.exit_deadline = scene, None, None, now+3
            self.last_signature, self.repeated, self.action_count = None, 0, 0
        if self.exiting_scene == scene:
            return None, "等待返回选关界面"
        if snapshot["paused"]:
            menu = snapshot.get("pause_menu")
            if self.owned_exit != scene:
                return None, "游戏已由用户暂停，等待继续"
            if (menu and menu["showing"] and not menu["inputsLocked"]
                    and menu["currentModeName"] in (1, 5) and not menu["usingOptionDetail"]):
                return "quit", reason + "，返回选关"
            return None, "等待关卡菜单就绪"
        if snapshot.get("pause_blocked", True) or not snapshot.get("loaded"):
            return None, "等待游戏允许返回选关"
        if now < self.exit_deadline:
            return None, reason + "，3 秒后返回选关"
        return "escape", reason + "，打开关卡菜单"

    def decide(self, snapshot, now, profile):
        if self.profile_slot != profile["slot"]:
            if self.profile_slot is not None:
                self.__init__()
            self.profile_slot = profile["slot"]
        name = snapshot["scene_type"]
        if snapshot["two_player"]:
            return None, "自动白金等待单人模式"
        if name == "scnGame" and snapshot["level_type"] == 0:
            return self.tutorial(snapshot, now)
        dialogue = snapshot["dialogue"]
        if not snapshot["paused"] and dialogue["active"]:
            return (None, "剧情有多个选项，等待手动选择") if dialogue["choices"] > 1 else ("space", "继续剧情 · "+str(dialogue["knot"]))
        if name == "scnGame":
            level = snapshot["level"]
            if snapshot["rank_stage"] >= 2 and nav.passed(snapshot["rank_raw"]):
                return super().decide(snapshot, now)
            if reached_s(profile["ranks"].get(level)):
                return self.exit_current(snapshot, now, "本关已达 S")
            if snapshot["failed"] or snapshot["rank_stage"] >= 2:
                return self.exit_current(snapshot, now, "本次未达 S")
            if snapshot["paused"]:
                return None, "游戏已暂停"
            if snapshot["level_type"] in (1, 2, 3, 4, 5, 6):
                return "play", "接管当前关卡 · "+level
            if snapshot["level_type"] == 8 and snapshot["loaded"] and snapshot["game_state"] == 0:
                return "space", "开始过场剧情"
            return None, "等待过场剧情结束"
        if snapshot["paused"]:
            return None, "游戏已暂停"
        if name != "scnLevelSelect":
            return None, "等待选关界面或当前关卡"
        self.pending_levels = unfinished_levels(snapshot, profile["ranks"], profile["levels"])
        if not self.pending_levels:
            self.all_complete = True
            self.target = None
            return None, "全部已解锁正式关卡均已达到 S"
        self.all_complete = False
        automatic = [item for item in self.pending_levels if item["automatic"]]
        if not automatic:
            return None, "单人关卡均已达到 S；双人举重小游戏需要手动处理"
        target = automatic[0]["level"]
        if target != self.target:
            self.target = target
            self.last_signature, self.repeated, self.action_count = None, 0, 0
        # 确认面板可能仍是手动选中的已 S 关卡；先关闭再移动。
        monitor = snapshot["monitor"]
        if monitor["visible"] and monitor["levelToGo"] != target and not monitor["goingToLevel"]:
            return "escape", "关闭当前面板，寻找未达 S 关卡"
        return self.menu(snapshot)

    def action_sent(self, snapshot, key):
        super().action_sent(snapshot, key)
        scene = (snapshot.get("scene_pointer"), snapshot.get("level"))
        if snapshot.get("level_type") != 0 and scene == self.exit_scene:
            if key == "escape":
                self.owned_exit = scene
            elif key == "quit":
                self.exiting_scene = scene

