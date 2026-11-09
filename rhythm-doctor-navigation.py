"""只读识别结算、剧情和选关状态，通过普通按键沿主线继续。"""
from collections import deque
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
SEQUENCE = json.loads((ROOT / "level-sequence-resources.json").read_text(encoding="utf-8-sig"))
enum_text = (ROOT / "level-enum-decompiled.cs").read_text(encoding="utf-8-sig").split("{", 1)[1].split("}", 1)[0]
LEVEL_NAMES = re.findall(r"\b[A-Za-z_][A-Za-z_0-9]*\b", enum_text)
LEVEL_ENTITIES = {level: (identifier, 1 if difficulty == "Normal" else 2)
                  for identifier, item in SEQUENCE.items() for difficulty, level in item.get("levels", {}).items()}


class SceneReader:
    def __init__(self, api, pid, layout):
        self.api, self.layout = api, layout
        self.reader = api.sample.ins.mem.Reader(pid)
        self.cache = {}
        try:
            cls = layout["scnGame"]
            for _ in range(16):
                if self.meta(cls)["name"] == "scnBase":
                    break
                cls = self.reader.u64(cls + 0x30)
            else:
                raise ValueError("未找到场景基类")
            self.slot = api.sample.static_base(self.reader, self.meta(cls)) + self.fields(cls)["_instance"][0]["offset"]
        except Exception:
            self.reader.close()
            raise

    def close(self):
        self.reader.close()

    def meta(self, cls):
        if cls not in self.cache:
            self.cache[cls] = self.api.objmod.metadata(self.reader, cls)
        return self.cache[cls]

    def fields(self, cls):
        result = {}
        for _ in range(24):
            if not cls:
                break
            meta = self.meta(cls)
            for field in meta["fields"]:
                result.setdefault(field["name"], (field, meta))
            cls = self.reader.u64(cls + 0x30)
        return result

    def cls(self, obj):
        return self.reader.u64(self.reader.u64(obj))

    def get(self, obj, name, cls=None):
        field, owner = self.fields(cls or self.cls(obj))[name]
        base = self.api.sample.static_base(self.reader, owner) if field["static"] else obj
        at = base + field["offset"]
        kind = field["mono_type"]
        if kind == 2:
            value = self.reader.read(at, 1)[0]
            if value not in (0, 1):
                raise ValueError("导航布尔字段无效：" + name)
            return bool(value)
        if kind in (8, 9, 17):
            return self.reader.i32(at)
        pointer = self.reader.u64(at)
        return self.reader.managed_string(pointer, limit=65536) if kind == 14 and pointer else pointer

    def items(self, pointer, integers=False):
        if not pointer:
            return []
        count = self.get(pointer, "_size")
        array = self.get(pointer, "_items")
        if not 0 <= count <= 1000:
            raise ValueError("导航列表长度异常")
        size = 4 if integers else 8
        read = self.reader.i32 if integers else self.reader.u64
        return [read(array + 32 + size * i) for i in range(count)]

    def dialogue(self):
        manager = self.get(None, "instance", self.layout["scrGameManager"])
        ink = self.get(manager, "inkDialogue")
        active = bool(ink and self.get(ink, "isPlaying") and self.get(ink, "activeDialoguePresent") and self.get(ink, "visible"))
        result = {"active": active, "choices": 0, "knot": "", "text": ""}
        if active:
            result["knot"] = self.get(ink, "currentKnot")
            story = self.get(ink, "story")
            if story:
                state = self.get(story, "_state")
                flow = self.get(state, "_currentFlow")
                result["choices"] = len(self.items(self.get(flow, "currentChoices")))
            # CJK 使用 TMP_Text；currentDialogueText 指向的旧 Text 可以始终为空。
            texts = []
            for field, text_field in (("dialogueTextLatinPixel", "m_Text"), ("dialogueTextVector", "m_text"),
                                      ("notification_textUI", "m_Text")):
                text = self.get(ink, field)
                texts.append(self.get(text, text_field) if text else "")
            result["text"] = texts
        return result

    def snapshot(self):
        obj = self.reader.u64(self.slot)
        name = self.meta(self.cls(obj))["name"] if obj else "None"
        result = {"scene_type": name, "scene_pointer": obj, "dialogue": self.dialogue(),
                  "two_player": self.get(None, "twoPlayerMode", self.layout["GC"]), "paused": False}
        if name == "scnGame":
            level = self.get(obj, "currentLevel")
            rank = self.get(obj, "rankscreen")
            result.update(level=self.get(obj, "levelIdentifier"), game_state=self.get(obj, "_gameState"),
                          paused=self.get(obj, "paused"), failed=self.get(obj, "failedLevel"),
                          loaded=self.get(obj, "levelFinishedLoading"), level_type=self.get(level, "levelType") if level else -1,
                          rank_stage=self.get(rank, "trueGameover") if rank else 0,
                          rank_raw=self.get(rank, "mRank") if rank else 0,
                          rank_exiting=self.get(rank, "exiting") if rank else False,
                          skippable_rank=self.get(level, "skippableRankScreen") if level else False)
            result["pause_blocked"] = self.get(obj, "pauseBlocked")
            pause = self.get(obj, "pauseMenu")
            result["pause_menu"] = {key: self.get(pause, key) for key in
                                    ("showing", "inputsLocked", "currentModeName", "usingOptionDetail")} if pause else None
            if level and result["level_type"] == 0:
                skip_to = self.get(level, "levelToSkipTo")
                result["skip_to"] = LEVEL_NAMES[skip_to] if 0 <= skip_to < len(LEVEL_NAMES) else None
                result["tutorial_menu"] = result["pause_menu"]
        elif name == "scnLevelSelect":
            pause = self.get(obj, "pauseMenu")
            entities = []
            for index, entity in enumerate(self.items(self.get(obj, "selectableEntities"))):
                item = {key: self.get(entity, key) for key in ("id", "group", "normalEnabled", "hardEnabled")}
                item.update(index=index, kind=self.meta(self.cls(entity))["name"])
                if item["kind"] == "SelectableObject":
                    item["action"] = self.get(entity, "action")
                entities.append(item)
            monitor = self.get(obj, "heartMonitor")
            panel = {key: self.get(monitor, key) for key in
                     ("visible", "canGoToLevel", "goingToLevel", "currentSelected", "levelToGo", "_twoPlayerSelected", "canChangePlayerMode")}
            result.update(entities=entities, monitor=panel, paused=self.get(pause, "showing") if pause else False)
            for key in ("selectedEntityIndex", "currentWardIndex", "currentDifficulty", "controlsAreLocked", "enterIsLocked",
                        "dialogueIsRunning", "firstFrame", "voidItemProgress", "selectedVerticalIndex", "playerQuitLevel",
                        "lockedLevelPresses"):
                result[key] = self.get(obj, key)
            played = self.get(obj, "justPlayedLevel")
            result["just_played"] = LEVEL_NAMES[played] if 0 <= played < len(LEVEL_NAMES) else "None"
            result["destinations"] = [self.get(item, "targetObjectId") for item in self.items(self.get(obj, "selectedVerticalDestinations"))]
        if self.reader.u64(self.slot) != obj:
            raise RuntimeError("导航读取期间场景改变")
        return result


def passed(rank):
    return rank in (3, 4, 5, 13, 14, 15, -13, -14, -15)


def next_level(level):
    special = {"Blurred": "Bitterness", "Bitterness": "Montage", "Montage": "Montage2",
               "InsomniacHard": "LuckyBreak", "AthleteFinale": "HaileyDuet", "PaigesReckoning": "Blurred"}
    if level in special:
        return special[level]
    entity, difficulty = LEVEL_ENTITIES.get(level, (None, None))
    if entity and difficulty == 1:
        unlocks = SEQUENCE[entity].get("unlocks", [])
        return unlocks[0] if unlocks else None
    return None


def enabled(entity, difficulty):
    return entity["normalEnabled" if difficulty == 1 else "hardEnabled"]


def direction_to(snapshot, target):
    choices = [item["id"] for item in snapshot["entities"]
               if item["group"] == snapshot["currentWardIndex"] and enabled(item, snapshot["currentDifficulty"])]
    selected = snapshot["entities"][snapshot["selectedEntityIndex"]]["id"]
    if target not in choices or selected not in choices:
        return None
    delta = (choices.index(target) - choices.index(selected)) % len(choices)
    return "right" if delta <= len(choices) - delta else "left"


def first_exit(snapshot, target_group):
    entities = {item["id"]: item for item in snapshot["entities"]}
    queue = deque([(snapshot["currentWardIndex"], None)])
    visited = set()
    while queue:
        group, first = queue.popleft()
        if group == target_group:
            return first
        if group in visited:
            continue
        visited.add(group)
        for item in entities.values():
            action = item.get("action", "")
            if item["group"] != group or not enabled(item, snapshot["currentDifficulty"]) or not action.startswith("transition:"):
                continue
            destinations = [action.split(":", 1)[1]]
            if item["id"] == "MainElevator":
                destinations = ["ExitRecordsRoom", "ExitVoid"]
            for destination in destinations:
                if destination in entities:
                    queue.append((entities[destination]["group"], first or (item["id"], destination)))
    return None


class Navigator:
    def __init__(self):
        self.source = None
        self.target = None
        self.last_signature = None
        self.repeated = 0
        self.action_count = 0
        self.deadline = 0
        self.completed_scene = None
        self.owned_tutorial = None
        self.tutorial_scene = None
        self.tutorial_deadline = None
        self.skipping_tutorial = None

    def complete(self, level):
        self.source, self.target = level, next_level(level)
        self.last_signature, self.repeated, self.action_count = None, 0, 0

    def decide(self, snapshot, now):
        name = snapshot["scene_type"]
        if name == "scnGame" and snapshot["level_type"] == 0:
            return self.tutorial(snapshot, now)
        if snapshot["paused"] or snapshot["two_player"]:
            return None, "导航等待：游戏暂停或双人模式"
        if name == "scnGame":
            if snapshot["failed"]:
                return None, "本局失败，等待手动处理"
            if snapshot["rank_stage"] >= 2 and snapshot["level_type"] in (1, 2, 3, 4, 5, 6):
                if not passed(snapshot["rank_raw"]):
                    return None, "本局未过关，等待手动处理"
                scene_key = (snapshot["scene_pointer"], snapshot["level"])
                if scene_key != self.completed_scene:
                    self.completed_scene = scene_key
                    self.complete(snapshot["level"])
                    self.deadline = now + 3
            elif snapshot["level_type"] not in (0, 8):
                return None, "等待关卡结束"
        elif name != "scnLevelSelect":
            return None, "等待选关或剧情场景"
        if not self.source:
            return None, "等待本程序观察到过关结果"
        if now < self.deadline:
            return None, "结算后等待 3 秒再继续"
        dialogue = snapshot["dialogue"]
        if dialogue["active"]:
            if dialogue["choices"] > 1:
                return None, "剧情有多个选项，等待手动选择"
            return "space", "继续剧情 · " + str(dialogue["knot"])
        if name == "scnGame":
            if snapshot["rank_exiting"]:
                return None, "等待离开关卡"
            if 1 <= snapshot["rank_stage"] <= 3 and snapshot["skippable_rank"]:
                return "space", "继续结算"
            if snapshot["level_type"] == 8 and snapshot["loaded"] and snapshot["game_state"] == 0:
                return "space", "开始过场剧情"
            return None, "等待剧情或结算过渡"
        if snapshot["playerQuitLevel"]:
            return None, "已手动退出关卡，停止自动选关"
        if not self.target:
            return None, "主线已结束或没有已确认的下一关路线"
        return self.menu(snapshot)

    def suspend_tutorial(self):
        self.tutorial_deadline = None

    def tutorial(self, snapshot, now):
        # 手动重玩时上一关可能属于另一条路线；以当前教学自身的正式目标为准。
        target = snapshot.get("skip_to")
        scene = (snapshot["scene_pointer"], snapshot["level"], target)
        if scene != self.tutorial_scene:
            self.tutorial_scene = scene
            self.tutorial_deadline = self.owned_tutorial = self.skipping_tutorial = None
            self.last_signature, self.repeated, self.action_count = None, 0, 0
        if target not in LEVEL_ENTITIES or target == snapshot["level"]:
            self.suspend_tutorial()
            return None, "已识别教学，但未确认对应的正式关卡"
        if snapshot["two_player"] or not snapshot["loaded"] or snapshot["failed"]:
            self.suspend_tutorial()
            return None, "等待单人教学场景就绪"
        if self.skipping_tutorial == scene:
            return None, "等待教学跳转到 · " + target
        if snapshot["paused"]:
            self.suspend_tutorial()
            menu = snapshot.get("tutorial_menu")
            if self.owned_tutorial != scene:
                return None, "教学已由用户暂停，等待手动处理"
            if (menu and menu["showing"] and not menu["inputsLocked"]
                    and menu["currentModeName"] == 1 and not menu["usingOptionDetail"]):
                return "skip", "使用游戏的跳过教学选项 · " + target
            return None, "等待教学菜单就绪"
        if snapshot.get("pause_blocked", True):
            self.suspend_tutorial()
            return None, "已识别教学，等待游戏允许打开菜单"
        if self.tutorial_deadline is None:
            self.tutorial_deadline = now + 3
        if now < self.tutorial_deadline:
            return None, "已识别教学，等待 3 秒后进入 · " + target
        return "escape", "打开教学菜单以进入 · " + target

    def action_sent(self, snapshot, key):
        if snapshot.get("level_type") != 0:
            return
        scene = (snapshot["scene_pointer"], snapshot["level"], snapshot.get("skip_to"))
        if key == "escape":
            self.owned_tutorial = scene
        elif key == "skip":
            self.skipping_tutorial = scene
            self.target = snapshot["skip_to"]

    def menu(self, snapshot):
        monitor = snapshot["monitor"]
        if monitor["visible"]:
            if monitor["goingToLevel"]:
                return None, "等待加载下一关"
            if monitor["levelToGo"] != self.target:
                return None, "当前确认面板与目标关卡不同，等待手动处理"
            if monitor["_twoPlayerSelected"] or monitor["currentSelected"] != 1:
                return None, "等待单人开始选项"
            return ("space", "确认进入 · " + self.target) if monitor["canGoToLevel"] else (None, "等待开始按钮就绪")
        if snapshot["firstFrame"] or snapshot["controlsAreLocked"] or snapshot["dialogueIsRunning"] or snapshot["enterIsLocked"]:
            return None, "等待选关动画结束"
        target_id, difficulty = LEVEL_ENTITIES.get(self.target, (None, None))
        entities = {item["id"]: item for item in snapshot["entities"]}
        if target_id not in entities:
            return None, "下一关不在当前选关列表"
        desired = target_id
        destination = None
        ward = snapshot["currentWardIndex"]
        if self.source == "Bitterness" and ward == 2:
            desired = "ExitSVTWard"
        elif self.source == "Blurred" and ward == 10:
            desired = "ExitRecordsRoom"
        elif self.source == "AthleteFinale" and ward == SEQUENCE["ExitAthleteWard"]["group"]:
            desired = "ExitAthleteWard"
        elif self.target == "Montage" and ward == 11 and not entities[target_id]["normalEnabled"]:
            desired = "VoidItem" + str(snapshot["voidItemProgress"] + 1)
        elif entities[target_id]["group"] != ward:
            route = first_exit(snapshot, entities[target_id]["group"])
            if not route:
                return None, "没有可通行的下一关路线"
            desired, destination = route
        if desired not in entities or entities[desired]["group"] != ward:
            return None, "等待下一关入口出现"
        selected = snapshot["entities"][snapshot["selectedEntityIndex"]]
        if selected["id"] != desired:
            key = direction_to(snapshot, desired)
            if key:
                return key, "移动到 · " + desired
            # 目标只在另一难度可见时，在可切换的角色上切换；对象上的上下有其他含义。
            if desired == target_id and selected["kind"] == "SelectableCharacter" and selected["normalEnabled"] and selected["hardEnabled"]:
                return "up", "切换到目标关卡所在班次"
            return None, "目标入口尚未启用"
        if destination and desired == "MainElevator":
            options = snapshot["destinations"]
            if destination not in options:
                return None, "电梯目标尚未解锁"
            if options[snapshot["selectedVerticalIndex"]] != destination:
                return "up", "选择电梯目的地 · " + destination
        if desired == target_id and snapshot["currentDifficulty"] != difficulty:
            return "up", "切换到目标关卡所在班次"
        return "space", "选择 · " + desired

    def permit(self, snapshot, key):
        # 同一状态最多重试三次，避免未解锁关卡、无效按键或未知界面被持续按键。
        signature = json.dumps([snapshot, key], ensure_ascii=False, sort_keys=True)
        if signature == self.last_signature:
            self.repeated += 1
        else:
            self.last_signature, self.repeated = signature, 1
        self.action_count += 1
        allowed = self.repeated <= 3 and self.action_count <= 500
        return allowed


if __name__ == "__main__":
    import importlib.util
    spec = importlib.util.spec_from_file_location("app", ROOT / "rhythm-doctor-autoplay-app.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    pid = app.runtime.game_pid()
    binding = app.runtime.resolve_layout(app.api, pid, print)
    scene = SceneReader(app.api, pid, binding["classes"])
    try:
        value = scene.snapshot()
        app.runtime.atomic_json(ROOT / "navigation-current-state.json", value)
        print(json.dumps(value, ensure_ascii=False))
    finally:
        scene.close()
