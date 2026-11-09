"""为 LevelSelectSpam 规划原生界面按键；只读游戏，不构造键盘或修改存档。"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platinum = load("save_slot_platinum", "rhythm-doctor-platinum.py")
nav = platinum.nav
_class_cache = None
TERMINAL = {"complete", "cancelled"}


def _save_classes(scene, identity):
    """复用已验证的类型地址；必要时只写本模块自己的缓存。"""
    global _class_cache
    identity = list(identity)
    if _class_cache and _class_cache[0] == identity:
        return _class_cache[1]
    names = {"Persistence", "PlayerPrefsJson"}
    own_path = ROOT / "save-slot-runtime-classes.json"
    for path in (own_path, ROOT / "active-save-classes.json"):
        if not path.exists():
            continue
        cached = json.loads(path.read_text(encoding="utf-8-sig"))
        classes = cached.get("classes", {})
        if cached.get("identity") == identity and set(classes) == names:
            if all(scene.meta(at)["name"] == name and scene.meta(at)["namespace"] == ""
                   for name, at in classes.items()):
                _class_cache = identity, classes
                return classes
    parser = load("save_slot_names", "managed-type-name-offsets.py")
    reader = scene.reader
    assembly = Path(reader.path).with_name("Rhythm Doctor_Data") / "Managed" / "Assembly-CSharp.dll"
    offsets = parser.read_type_name_offsets(assembly, sorted(names))["types"]
    anchor = scene.layout["scnGame"]
    image, anchor_name = reader.u64(anchor + 0x40), reader.u64(anchor + 0x48)
    wanted = {anchor_name + offsets[name]["delta_from_anchor"]: name for name in names}
    refs = reader.scan([struct.pack("<Q", address) for address in wanted], alignment=8)
    if refs.get("truncated"):
        raise RuntimeError("存档类型扫描超过上限")
    classes = {}
    for ref in refs["hits"]:
        at = ref["address"] - 0x48
        try:
            name = wanted.get(reader.u64(at + 0x48))
            if not name or reader.u64(at + 0x40) != image:
                continue
            meta = scene.meta(at)
            if (meta["name"] != name or meta["namespace"] != ""
                    or int(meta["token"], 16) != int(offsets[name]["token"], 16)):
                continue
            if name in classes and classes[name] != at:
                raise RuntimeError("存档类型不唯一：" + name)
            classes[name] = at
        except (OSError, ValueError, IndexError):
            continue
    if set(classes) != names:
        raise RuntimeError("等待存档类型加载")
    own_path.write_text(json.dumps({"identity": identity, "classes": classes}, indent=2), encoding="utf-8")
    _class_cache = identity, classes
    return classes


def _boxed_boolean(scene, pointer, default):
    if not pointer:
        return default
    if scene.meta(scene.cls(pointer))["name"] != "Boolean":
        raise ValueError("存档布尔值类型不匹配")
    return scene.get(pointer, "m_value")


def read_slot_inventory(scene, identity):
    """读取三个槽位；菜单中的 current_slot 不能用作用户所选存档。"""
    classes = _save_classes(scene, identity)
    prefs_class = classes["PlayerPrefsJson"]
    files_pointer = scene.get(None, "files", prefs_class)
    files = platinum.dictionary(scene, files_pointer)
    paths = platinum.dictionary(scene, scene.get(None, "filesPath", prefs_class), string_values=True)
    values = {}
    for kind in range(4):
        prefs = files.get(kind)
        if not prefs or scene.get(prefs, "fileType") != kind:
            raise RuntimeError("等待全部存档对象加载")
        values[kind] = platinum.dictionary(scene, scene.get(prefs, "dict"), string_keys=True)
    slots = []
    for slot in range(3):
        data = values[slot + 1]
        keys = sorted(data)
        ranks = {}
        for key, pointer in data.items():
            if key.startswith("Level_") and key.endswith("_rank"):
                if not pointer or scene.meta(scene.cls(pointer))["name"] != "String":
                    raise ValueError("存档评价不是字符串")
                ranks[key[6:-5]] = scene.reader.managed_string(pointer, limit=64)
        slots.append({"slot": slot, "path": paths[slot + 1], "key_count": len(keys),
                      "keys": keys, "empty": keys == ["version"], "ranks": ranks,
                      "first_time": _boxed_boolean(scene, data.get("first_time"), True)})
    if files != platinum.dictionary(scene, files_pointer):
        raise RuntimeError("读取期间存档对象改变")
    return {"current_slot": scene.get(None, "currentSlotIndex", classes["Persistence"]),
            "current_slot_trusted_in": ["scnGame", "scnLevelSelect"],
            "did_calibration": _boxed_boolean(scene, values[0].get("did_calibration"), False),
            "slots": slots}


def _pause_snapshot(scene, pointer):
    if not pointer:
        return None
    return {key: scene.get(pointer, key) for key in
            ("showing", "inputsLocked", "goingToScene", "currentModeName", "firstModeName", "usingOptionDetail")}


def _redeem_panel_active(scene, pointer):
    # RedeemCode.isEnabled 是 CanvasGroup.interactable 的 native 属性代理，没有 Mono 字段。
    # Selectable.OnEnable/OnDisable 维护 m_EnableCalled；任何兑换控件仍启用时均保守等待，
    # 包括 EnablePanel(false) 的淡出期，避免将空格发送给兑换或取消按钮。
    if not pointer:
        return False
    active = []
    for name in ("inputField", "redeemButton", "cancelButton"):
        control = scene.get(pointer, name)
        if not control:
            raise RuntimeError("兑换面板控件尚未初始化")
        active.append(scene.get(control, "m_EnableCalled"))
    return any(active)


def augment_snapshot(scene, snapshot=None):
    """追加主菜单与暂停状态，完成后再次检查场景指针。"""
    result = dict(snapshot if snapshot is not None else scene.snapshot())
    obj, name = result["scene_pointer"], result["scene_type"]
    if scene.reader.u64(scene.slot) != obj:
        raise RuntimeError("存档导航读取前场景改变")
    if name in ("scnGame", "scnLevelSelect"):
        result["pause_menu"] = _pause_snapshot(scene, scene.get(obj, "pauseMenu"))
        if name == "scnGame":
            level = scene.get(obj, "currentLevel")
            if level:
                skip_to = scene.get(level, "levelToSkipTo")
                result["skip_to"] = nav.LEVEL_NAMES[skip_to] if 0 <= skip_to < len(nav.LEVEL_NAMES) else None
    elif name == "scnMenu":
        menu = {key: scene.get(obj, key) for key in
                ("section", "currentOption", "currentSlotIndex", "slotOpen", "openSlotSelectedOption",
                 "selectingSlotToCopy", "confirmingSlotToErase", "transitioningToAnotherScene",
                 "changingSection", "showingPauseMenu", "showingSettings")}
        redeem = scene.get(obj, "redeemCode")
        menu["redeem_enabled"] = _redeem_panel_active(scene, redeem)
        menu["redeem_state_source"] = "Selectable.m_EnableCalled（包含关闭淡出期）"
        menu["pause_menu"] = _pause_snapshot(scene, scene.get(obj, "settingsMenu"))
        title = scene.get(obj, "title")
        menu["story_mode_text"] = scene.get(title, "m_Text") if title else ""
        texts = scene.get(obj, "optionsText")
        count = scene.reader.u64(texts + 24) if texts else 0
        if not 0 <= count <= 32:
            raise ValueError("主菜单选项数量异常")
        menu["options"] = [scene.get(scene.reader.u64(texts + 32 + 8*i), "m_Text") for i in range(count)]
        matches = [i for i, value in enumerate(menu["options"]) if value and value == menu["story_mode_text"]]
        menu["story_mode_index"] = matches[0] if len(matches) == 1 else None
        result["main_menu"] = menu
    if scene.reader.u64(scene.slot) != obj:
        raise RuntimeError("存档导航读取期间场景改变")
    return result


def locked_candidates(snapshot, profile):
    """仅选择可用普通导航到达、但存档状态为 NotAvailable 的角色。"""
    if snapshot.get("scene_type") != "scnLevelSelect":
        return []
    candidates = []
    ward, difficulty = snapshot["currentWardIndex"], snapshot["currentDifficulty"]
    for entity in snapshot["entities"]:
        if entity["kind"] != "SelectableCharacter":
            continue
        for shift, level in nav.SEQUENCE.get(entity["id"], {}).get("levels", {}).items():
            desired_difficulty = 1 if shift == "Normal" else 2
            if level == "None" or not nav.enabled(entity, desired_difficulty):
                continue
            if profile["ranks"].get(level, "NotAvailable") != "NotAvailable":
                continue
            if entity["group"] != ward and not nav.first_exit(snapshot, entity["group"]):
                continue
            candidates.append({"entity": entity["id"], "difficulty": desired_difficulty, "level": level,
                               "group": entity["group"]})
    return sorted(candidates, key=lambda item: (item["group"] != ward, item["difficulty"] != difficulty,
                                               platinum.flow_order(item["entity"], item["difficulty"])))


def _menu_slot(snapshot, target_slot):
    menu = snapshot.get("main_menu")
    if not menu:
        return None, "等待主菜单状态"
    if menu["transitioningToAnotherScene"] or menu["changingSection"]:
        return None, "等待主菜单过渡"
    if menu["selectingSlotToCopy"] or menu["confirmingSlotToErase"]:
        return "escape", "退出存档复制或删除界面"
    if menu["redeem_enabled"]:
        return None, "兑换码界面已打开，等待手动关闭"
    if menu["showingPauseMenu"] or menu["showingSettings"]:
        pause = menu.get("pause_menu")
        if pause and pause["showing"] and not pause["inputsLocked"] and not pause["goingToScene"]:
            return "escape", "返回主菜单"
        return None, "等待设置界面退出"
    section = menu["section"]
    if section == 0:
        return "space", "进入主菜单"
    if section == 1:
        target, selected = menu.get("story_mode_index"), menu["currentOption"]
        if target is None or not 0 <= selected < len(menu["options"]):
            return None, "未唯一识别故事模式选项"
        if selected == target:
            return "space", "打开故事模式存档列表"
        delta = (target - selected) % len(menu["options"])
        return ("down" if delta <= len(menu["options"]) - delta else "up"), "选择故事模式"
    if section == 3:
        return "escape", "从制作人员界面返回主菜单"
    if section != 2 or menu["currentSlotIndex"] not in (0, 1, 2):
        return None, "未识别的存档菜单状态"
    selected = menu["currentSlotIndex"]
    if menu["slotOpen"]:
        if selected != target_slot:
            return "escape", "收起当前存档选项"
        if menu["openSlotSelectedOption"] != 0:
            return "up", "选择进入存档选项"
        return "space", f"确认进入存档 {target_slot + 1}"
    if selected == target_slot:
        return "space", f"展开存档 {target_slot + 1}"
    return ("right" if (target_slot - selected) % 3 == 1 else "left"), f"选择存档 {target_slot + 1}"


def _pause_action(snapshot, key):
    menu = snapshot.get("pause_menu")
    if menu and menu.get("showing"):
        if menu["inputsLocked"] or menu["goingToScene"]:
            return None, "等待暂停菜单过渡"
        if menu["usingOptionDetail"] or menu["currentModeName"] != menu["firstModeName"]:
            return "escape", "返回暂停菜单主页"
        return key, "跳过开场教学" if key == "skip" else "通过菜单返回上一层"
    if snapshot.get("scene_type") == "scnGame":
        if not snapshot.get("loaded") or snapshot.get("pause_blocked", True):
            return None, "等待关卡允许打开菜单"
    elif snapshot.get("scene_type") == "scnLevelSelect":
        if snapshot.get("monitor", {}).get("visible"):
            return "escape", "关闭关卡面板"
        if any(snapshot.get(key, True) for key in ("firstFrame", "controlsAreLocked", "dialogueIsRunning", "enterIsLocked")):
            return None, "等待选关界面允许打开菜单"
    else:
        return None, "等待可退出的场景"
    return "escape", "打开暂停菜单"


def _locked_navigation(snapshot, target):
    pause = snapshot.get("pause_menu")
    if snapshot.get("paused") or (pause and pause.get("showing")):
        if pause and (pause["inputsLocked"] or pause["goingToScene"]):
            return None, "等待暂停菜单过渡"
        return "escape", "关闭暂停菜单以选择锁关"
    if snapshot["monitor"]["visible"]:
        return "escape", "关闭关卡面板以选择锁关"
    if any(snapshot.get(key, True) for key in ("firstFrame", "controlsAreLocked", "dialogueIsRunning", "enterIsLocked")):
        return None, "等待选关动画结束"
    entities = {item["id"]: item for item in snapshot["entities"]}
    selected = snapshot["entities"][snapshot["selectedEntityIndex"]]
    if target["entity"] not in entities or not nav.enabled(entities[target["entity"]], target["difficulty"]):
        return None, "目标锁关不再可见"
    desired, destination = target["entity"], None
    if entities[desired]["group"] != snapshot["currentWardIndex"]:
        route = nav.first_exit(snapshot, entities[desired]["group"])
        if not route:
            return None, "没有通往目标锁关的路线"
        desired, destination = route
    if selected["id"] != desired:
        direction = nav.direction_to(snapshot, desired)
        if direction:
            return direction, "移动到 · " + desired
        if (selected["kind"] == "SelectableCharacter" and selected["normalEnabled"] and selected["hardEnabled"]):
            return "up", "切换到锁关所在班次"
        switchable = [item for item in snapshot["entities"] if item["kind"] == "SelectableCharacter"
                      and item["group"] == snapshot["currentWardIndex"] and item["normalEnabled"] and item["hardEnabled"]]
        if switchable:
            return nav.direction_to(snapshot, switchable[0]["id"]), "移动到可切换班次的角色"
        return None, "没有可切换班次的角色"
    if destination and desired == "MainElevator":
        options = snapshot["destinations"]
        if destination not in options:
            return None, "电梯目的地不可用"
        if options[snapshot["selectedVerticalIndex"]] != destination:
            return "up", "选择电梯目的地 · " + destination
    if desired != target["entity"]:
        return "space", "通过入口 · " + desired
    if snapshot["currentDifficulty"] != target["difficulty"]:
        if selected["normalEnabled"] and selected["hardEnabled"]:
            return "up", "切换到目标锁关班次"
        return None, "目标角色不能切换到所需班次"
    count = snapshot["lockedLevelPresses"]
    if not isinstance(count, int) or count < 0:
        return None, "锁关计数无效"
    if count > 20:
        return "right", "切换角色以重置已超过阈值的锁关计数"
    if count == 20:
        return None, "游戏已触发锁关成就"
    return "space", f"尝试锁关 · {count + 1}/20"


def _digest(ranks):
    return hashlib.sha256(json.dumps(ranks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def new_session(account, snapshot, profile, inventory):
    """纯函数：在离开选关前记录可信原槽，优先使用原槽的可达锁关。"""
    if snapshot.get("scene_type") != "scnLevelSelect" or profile.get("slot") not in (0, 1, 2):
        raise ValueError("必须先在选关界面确认原存档，不能从主菜单猜测")
    if inventory["current_slot"] != profile["slot"]:
        raise ValueError("当前存档读取不一致")
    if profile.get("achievements", {}).get("account") != account:
        raise ValueError("Steam account 与存档恢复会话不匹配")
    candidates = locked_candidates(snapshot, profile)
    temporary = None
    if not candidates:
        if not inventory["did_calibration"]:
            raise ValueError("尚未校准，不能执行空白存档的既定路线")
        empty = [item["slot"] for item in inventory["slots"] if item["slot"] != profile["slot"] and item["empty"]]
        if not empty:
            raise ValueError("没有可达锁关，也没有空白存档")
        temporary = min(empty)
    return {"version": 1, "account": account, "original_slot": profile["slot"], "temporary_slot": temporary,
            "phase": "locked_target" if candidates else "enter_temporary", "target": candidates[0] if candidates else None,
            "original_ranks_digest": _digest(profile["ranks"]), "native_triggered": False,
            "achieved": False, "restore_requested": False, "temporary_entered": False,
            "original_restored": False, "action_count": 0, "last_action": None, "repeat_count": 0,
            "failure": None, "created_at": datetime.now(timezone.utc).isoformat()}


def advance(state, snapshot, now, profile, inventory, achieved=False):
    """纯状态机；返回 (新状态, 按键或 None, 原因)，从不发送输入。"""
    result = deepcopy(state)
    if result["phase"] in TERMINAL:
        return result, None, "存档恢复会话已结束"
    if profile.get("achievements", {}).get("account") != result["account"]:
        return result, None, "Steam account 改变，保留原账户的恢复记录"
    name = snapshot["scene_type"]
    trusted = name in ("scnGame", "scnLevelSelect")
    slot = profile.get("slot") if trusted else None
    if achieved:
        result["achieved"] = True
        result["phase"] = "restore_original"
    if result.get("restore_requested"):
        result["phase"] = "restore_original"
    if trusted and slot not in (result["original_slot"], result["temporary_slot"]):
        result["failure"] = "检测到用户进入其他存档，暂停自动恢复"
        return result, None, result["failure"]
    dialogue = snapshot.get("dialogue", {})
    # 暂停时 Space 会选择菜单条目，不能将它误作对话确认。
    # Intro 可直接用暂停菜单跳过，无须完成开场对话或教学节拍。
    pause = snapshot.get("pause_menu")
    pause_showing = snapshot.get("paused") or (pause and pause.get("showing"))
    use_game_menu = (name == "scnGame" and not snapshot.get("pause_blocked", True)
                     and result["phase"] in ("prepare_temporary", "enter_temporary", "restore_original"))
    if dialogue.get("active") and not pause_showing and not use_game_menu:
        if dialogue.get("choices", 0) > 1:
            return result, None, "剧情有多个选项，保留恢复会话等待选择"
        if dialogue.get("knot") == "MashingLockedLevel":
            result["native_triggered"] = True
            result["phase"] = "restore_original"
        return result, "space", "继续存档导航剧情 · " + str(dialogue.get("knot", ""))
    phase = result["phase"]
    if phase == "verify_achievement":
        return result, None, "已回到原存档，等待 Steam 确认锁关成就"
    if phase == "restore_original":
        if name == "scnLevelSelect" and slot == result["original_slot"]:
            if snapshot.get("paused"):
                return result, "escape", "关闭原存档暂停菜单"
            if any(snapshot.get(key, True) for key in ("firstFrame", "controlsAreLocked", "dialogueIsRunning", "enterIsLocked")):
                return result, None, "等待原存档选关界面就绪"
            result["original_restored"] = True
            result["original_ranks_unchanged"] = result["original_ranks_digest"] == _digest(profile["ranks"])
            result["phase"] = ("complete" if result["achieved"] else
                               "verify_achievement" if result["native_triggered"] else "cancelled")
            return result, None, "已回到原存档" + ("；Steam 已确认成就" if result["achieved"] else "")
        if name == "scnMenu":
            key, reason = _menu_slot(snapshot, result["original_slot"])
        else:
            key, reason = _pause_action(snapshot, "quit")
        return result, key, "恢复原存档 · " + reason
    if phase == "enter_temporary":
        temporary = result["temporary_slot"]
        if trusted and slot == temporary:
            result["temporary_entered"] = True
            result["phase"] = "prepare_temporary"
            phase = "prepare_temporary"
        elif name == "scnMenu":
            # 再确认一次空槽；进入后才允许本任务产生的少量原生存档数据。
            target = next((item for item in inventory["slots"] if item["slot"] == temporary), None)
            if not target or not target["empty"]:
                result["failure"] = "原定空白存档已含数据，取消切换并恢复原存档"
                result["restore_requested"] = True
                result["phase"] = "restore_original"
                return result, None, result["failure"]
            key, reason = _menu_slot(snapshot, temporary)
            return result, key, reason
        else:
            key, reason = _pause_action(snapshot, "quit")
            return result, key, reason
    if phase == "prepare_temporary":
        if not trusted or slot != result["temporary_slot"]:
            return result, None, "等待进入临时存档"
        if name == "scnGame":
            if snapshot.get("level") == "Intro" and snapshot.get("skip_to") == "OrientalTechno":
                key, reason = _pause_action(snapshot, "skip")
                return result, key, reason
            if snapshot.get("level") == "OrientalTechno":
                key, reason = _pause_action(snapshot, "quit")
                return result, key, "保留 1-2 锁定状态 · " + reason
            result["failure"] = "临时存档进入了未适配的场景，恢复原存档"
            result["restore_requested"] = True
            result["phase"] = "restore_original"
            return result, None, result["failure"]
        if name == "scnLevelSelect":
            candidates = locked_candidates(snapshot, profile)
            if not candidates:
                if any(snapshot.get(key, True) for key in ("firstFrame", "controlsAreLocked", "dialogueIsRunning", "enterIsLocked")):
                    return result, None, "等待临时存档选关列表就绪"
                result["failure"] = "临时存档没有可达锁关，恢复原存档"
                result["restore_requested"] = True
                result["phase"] = "restore_original"
                return result, None, result["failure"]
            result["target"] = candidates[0]
            result["phase"] = "locked_target"
            phase = "locked_target"
    if phase == "locked_target":
        desired_slot = result["temporary_slot"] if result["temporary_slot"] is not None else result["original_slot"]
        if name != "scnLevelSelect" or slot != desired_slot:
            return result, None, "等待目标存档的选关界面"
        target = result["target"]
        if profile["ranks"].get(target["level"], "NotAvailable") != "NotAvailable":
            result["failure"] = "目标锁关已解锁，恢复原存档"
            result["restore_requested"] = True
            result["phase"] = "restore_original"
            return result, None, result["failure"]
        selected = snapshot["entities"][snapshot["selectedEntityIndex"]]
        if (selected["id"] == target["entity"] and snapshot["currentDifficulty"] == target["difficulty"]
                and snapshot["lockedLevelPresses"] == 20):
            result["native_triggered"] = True
            result["phase"] = "restore_original"
            return result, None, "游戏内锁关计数已达 20，开始恢复原存档"
        key, reason = _locked_navigation(snapshot, target)
        return result, key, reason
    return result, None, "等待存档导航状态变化"


class SaveSlotNavigator:
    """持久化恢复责任；调用者负责前台检查、1 秒轮询和实际按键。"""
    def __init__(self, account, state_path=None):
        if not isinstance(account, int) or account <= 0:
            raise ValueError("需要当前 Steam account")
        self.account = account
        self.path = Path(state_path) if state_path is not None else ROOT / f"level-select-spam-session-{account}.json"
        self.state = None
        self.scene_key = None
        self.scene_ready_at = None
        self.now = 0
        self.proposal = None
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (saved.get("version") != 1 or saved.get("account") != account
                    or saved.get("original_slot") not in (0, 1, 2)
                    or saved.get("temporary_slot") not in (None, 0, 1, 2)):
                raise ValueError("存档恢复会话不匹配，未执行操作")
            self.state = saved
            if self.active:
                self.state.update(phase="restore_original", restore_requested=True, recovered_after_restart=True,
                                  last_action=None, repeat_count=0)
                self._persist()

    @property
    def active(self):
        return bool(self.state and self.state["phase"] not in TERMINAL)

    @property
    def complete(self):
        return bool(self.state and self.state["phase"] == "complete")

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def start_session(self, snapshot, profile, inventory):
        if self.active:
            return False
        self.state = new_session(self.account, snapshot, profile, inventory)
        self.scene_key = self.scene_ready_at = self.proposal = None
        self._persist()
        return True

    def request_restore(self, reason="已请求恢复原存档"):
        if self.active:
            self.state.update(restore_requested=True, phase="restore_original", failure=reason,
                              last_action=None, repeat_count=0)
            self._persist()

    def decide(self, snapshot, now, profile, inventory=None, achieved=None):
        self.now, self.proposal = now, None
        if not self.active:
            return None, "没有待处理的存档恢复会话"
        inventory = inventory if inventory is not None else snapshot.get("slot_inventory")
        if inventory is None:
            return None, "等待只读存档清单"
        if achieved is None:
            achieved = profile.get("achievements", {}).get("rows", {}).get("LevelSelectSpam", {}).get("achieved", False)
        updated, key, reason = advance(self.state, snapshot, now, profile, inventory, achieved)
        if updated != self.state:
            self.state = updated
            self._persist()
        if key is None:
            return key, reason
        scene_key = (snapshot["scene_type"], snapshot["scene_pointer"], snapshot.get("level"))
        if self.scene_key != scene_key:
            self.scene_key, self.scene_ready_at = scene_key, now + 3
        if now < self.scene_ready_at:
            return None, "场景就绪，等待 3 秒再继续存档导航"
        signature = self._signature(snapshot, key)
        if self.state.get("last_action") == signature and self.state.get("repeat_count", 0) >= 3:
            return None, "相同界面按键未生效，保留恢复记录并停止重试"
        if self.state["action_count"] >= 300:
            return None, "存档导航动作超过限制，保留恢复记录"
        self.proposal = signature
        return key, reason

    @staticmethod
    def _signature(snapshot, key):
        # 计数是游戏实际接受的确认次数；每次计数增加都允许下一个确认。
        return json.dumps([snapshot, key], ensure_ascii=False, sort_keys=True)

    def action_sent(self, snapshot, key):
        signature = self._signature(snapshot, key)
        if not self.active or signature != self.proposal:
            raise ValueError("实际动作不是本模块当前建议的动作")
        repeats = self.state.get("repeat_count", 0) + 1 if self.state.get("last_action") == signature else 1
        self.state.update(last_action=signature, repeat_count=repeats, action_count=self.state["action_count"] + 1)
        self.proposal = None
        self._persist()


if __name__ == "__main__":
    app = load("save_slot_read_only_app", "rhythm-doctor-autoplay-app.py")
    pid = app.runtime.game_pid()
    binding = app.runtime.resolve_layout(app.api, pid, print)
    scene = nav.SceneReader(app.api, pid, binding["classes"])
    try:
        value = {"snapshot": augment_snapshot(scene), "inventory": read_slot_inventory(scene, binding["identity"])}
        (ROOT / "save-slot-read-only-snapshot.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(value, ensure_ascii=False))
    finally:
        scene.close()
