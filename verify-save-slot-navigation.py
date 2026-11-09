"""离线验证原生存档路线与恢复责任；不会构造键盘或修改游戏存档。"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("save_slot_navigation", ROOT / "rhythm-doctor-save-slot-navigation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ACCOUNT = 123  # 合成测试账户，不对应实际用户。
checks = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)


def pause(showing=False, **changes):
    result = {"showing": showing, "inputsLocked": False, "goingToScene": False,
              "currentModeName": 1, "firstModeName": 1, "usingOptionDetail": False}
    result.update(changes)
    return result


def selection(**changes):
    result = {"scene_type": "scnLevelSelect", "scene_pointer": 100, "paused": False,
              "pause_menu": pause(), "two_player": False, "currentWardIndex": 1,
              "currentDifficulty": 1, "selectedEntityIndex": 0, "lockedLevelPresses": 0,
              "firstFrame": False, "controlsAreLocked": False, "dialogueIsRunning": False,
              "enterIsLocked": False, "selectedVerticalIndex": 0, "destinations": [],
              "dialogue": {"active": False, "choices": 0, "knot": ""},
              "monitor": {"visible": False, "goingToLevel": False},
              "entities": [{"id": "1-1", "kind": "SelectableCharacter", "group": 1,
                            "normalEnabled": True, "hardEnabled": True},
                           {"id": "1-2", "kind": "SelectableCharacter", "group": 1,
                            "normalEnabled": True, "hardEnabled": True}]}
    result.update(changes)
    return result


def game(level, **changes):
    result = {"scene_type": "scnGame", "scene_pointer": 200 if level == "Intro" else 201,
              "level": level, "skip_to": "OrientalTechno" if level == "Intro" else "None",
              "loaded": True, "pause_blocked": False, "paused": False,
              "pause_menu": pause(), "dialogue": {"active": False, "choices": 0, "knot": ""}}
    result.update(changes)
    return result


def menu(**changes):
    inner = {"section": 0, "currentOption": 0, "currentSlotIndex": 0, "slotOpen": False,
             "openSlotSelectedOption": 0, "selectingSlotToCopy": False, "confirmingSlotToErase": False,
             "transitioningToAnotherScene": False, "changingSection": False,
             "showingPauseMenu": False, "showingSettings": False, "redeem_enabled": False,
             "options": ["设置", "制作人员", "故事模式", "退出"], "story_mode_index": 2,
             "pause_menu": pause()}
    inner.update(changes)
    return {"scene_type": "scnMenu", "scene_pointer": 300, "paused": False,
            "dialogue": {"active": False, "choices": 0, "knot": ""}, "main_menu": inner}


def profile(slot=0, full=True):
    ranks = {level: "S+" for entity in module.nav.SEQUENCE.values() for level in entity.get("levels", {}).values()} if full else {"OrientalTechno": "NotFinished"}
    return {"slot": slot, "ranks": ranks,
            "achievements": {"account": ACCOUNT, "rows": {"LevelSelectSpam": {"achieved": False}}}}


def inventory(slot=0):
    return {"current_slot": slot, "did_calibration": True,
            "slots": [{"slot": 0, "empty": False}, {"slot": 1, "empty": True}, {"slot": 2, "empty": True}]}


def step(state, snapshot, expected_key, profile_=None, inventory_=None, achieved=False):
    updated, key, reason = module.advance(state, snapshot, 10, profile_ or profile(), inventory_ or inventory(), achieved)
    if key != expected_key:
        raise AssertionError((expected_key, key, reason, updated))
    return updated


base = selection()
full = profile()
inv = inventory()
state = module.new_session(ACCOUNT, base, full, inv)
check("无原槽锁关时记录 original_slot=0 并选空槽 1", state["original_slot"] == 0 and state["temporary_slot"] == 1)
state = step(state, base, "escape")
state = step(state, selection(paused=True, pause_menu=pause(True)), "quit")
state = step(state, menu(), "space", profile(2), inventory(2))
state = step(state, menu(section=1), "down", profile(2), inventory(2))
state = step(state, menu(section=1, currentOption=2), "space", profile(2), inventory(2))
state = step(state, menu(section=2), "right", profile(2), inventory(2))
state = step(state, menu(section=2, currentSlotIndex=1), "space", profile(2), inventory(2))
state = step(state, menu(section=2, currentSlotIndex=1, slotOpen=True), "space", profile(2), inventory(2))
check("主菜单临时 Persistence slot=2 不覆盖 original_slot", state["original_slot"] == 0)
temp = profile(1, False)
temp_inv = inventory(1)
state = step(state, game("Intro"), "escape", temp, temp_inv)
check("实际进入 Intro 后才确认临时槽进入成功", state["temporary_entered"] and state["phase"] == "prepare_temporary")
state = step(state, game("Intro", paused=True, pause_menu=pause(True)), "skip", temp, temp_inv)
state = step(state, game("OrientalTechno"), "escape", temp, temp_inv)
state = step(state, game("OrientalTechno", paused=True, pause_menu=pause(True)), "quit", temp, temp_inv)
check("Intro 经 Skip 到 1-1，退出前没有请求 play", state["phase"] == "prepare_temporary")
state = step(state, selection(scene_pointer=400), "right", temp, temp_inv)
check("临时槽选择正常难度 1-2 锁关", state["target"]["level"] == "Intimate" and state["target"]["difficulty"] == 1)
for count in range(20):
    state = step(state, selection(scene_pointer=400, selectedEntityIndex=1, lockedLevelPresses=count), "space", temp, temp_inv)
check("20 次确认以游戏 lockedLevelPresses 递增为准", not state["native_triggered"])
state = step(state, selection(scene_pointer=400, selectedEntityIndex=1, lockedLevelPresses=20), None, temp, temp_inv)
check("计数 20 后停止确认并启动恢复", state["native_triggered"] and state["phase"] == "restore_original")
dialogue = {"active": True, "choices": 0, "knot": "MashingLockedLevel"}
state = step(state, selection(scene_pointer=400, selectedEntityIndex=1, lockedLevelPresses=20, controlsAreLocked=True,
                              dialogue=dialogue), "space", temp, temp_inv)
state = step(state, selection(scene_pointer=400), "escape", temp, temp_inv, achieved=True)
state = step(state, selection(scene_pointer=400, paused=True, pause_menu=pause(True)), "quit", temp, temp_inv, achieved=True)
check("Steam 成功仍不提前结束，必须恢复原槽", state["achieved"] and not state["original_restored"])
state = step(state, menu(section=2, currentSlotIndex=1, slotOpen=True), "escape", profile(2), inventory(2), achieved=True)
state = step(state, menu(section=2, currentSlotIndex=1), "left", profile(2), inventory(2), achieved=True)
state = step(state, menu(section=2, currentSlotIndex=0), "space", profile(2), inventory(2), achieved=True)
state = step(state, menu(section=2, currentSlotIndex=0, slotOpen=True), "space", profile(2), inventory(2), achieved=True)
check("仅在主菜单高亮原槽不算恢复", not state["original_restored"])
state = step(state, selection(scene_pointer=500), None, full, inv, achieved=True)
check("实际进入原槽选关后完成且原评价摘要一致", state["phase"] == "complete" and state["original_restored"] and state["original_ranks_unchanged"])

direct = module.new_session(ACCOUNT, base, profile(0, False), inv)
check("原槽有可达锁关时不使用空槽", direct["temporary_slot"] is None and direct["target"]["level"] == "Intimate")
for field in ("selectingSlotToCopy", "confirmingSlotToErase"):
    for option in (0, 1, 2):
        value = menu(section=2, currentSlotIndex=1, slotOpen=True, openSlotSelectedOption=option, **{field: True})
        check(f"{field} option={option} 仅取消", module._menu_slot(value, 1)[0] == "escape")
for option in (1, 2):
    check(f"展开槽位的危险选项 {option} 只上移", module._menu_slot(menu(section=2, currentSlotIndex=1,
          slotOpen=True, openSlotSelectedOption=option), 1)[0] == "up")
check("错误槽展开时先收起，不按左右", module._menu_slot(menu(section=2, currentSlotIndex=2, slotOpen=True), 1)[0] == "escape")
check("故事模式未唯一识别时不确认", module._menu_slot(menu(section=1, story_mode_index=None), 1)[0] is None)
check("菜单过渡时不按键", module._menu_slot(menu(changingSection=True), 1)[0] is None)

class RedeemFields:
    def __init__(self, flags):
        self.flags = flags

    def get(self, obj, name):
        if obj == 10:
            # 故意不提供 isEnabled：它不是字段。
            return {"inputField": 11, "redeemButton": 12, "cancelButton": 13}[name]
        if name != "m_EnableCalled":
            raise KeyError(name)
        return self.flags[obj - 11]


check("兑换属性不当字段读取，隐藏面板允许继续", not module._redeem_panel_active(RedeemFields([False]*3), 10))
for index in range(3):
    flags = [False]*3
    flags[index] = True
    check(f"兑换面板控件 {index} 尚启用时保守等待", module._redeem_panel_active(RedeemFields(flags), 10))
check("兑换面板启用时不发送菜单确认", module._menu_slot(menu(redeem_enabled=True), 1)[0] is None)

enter = module.new_session(ACCOUNT, base, full, inv)
altered = inventory(2)
altered["slots"][1]["empty"] = False
changed = step(enter, menu(section=2, currentSlotIndex=1), None, profile(2), altered)
check("原定空槽出现数据时取消进入并恢复", changed["phase"] == "restore_original" and changed["restore_requested"])
for reason, snapshot_, inventory_ in (("主菜单不猜原槽", menu(), inv),
                                      ("无空槽不覆盖已有数据", base, {**inv, "slots": [{"slot": i, "empty": False} for i in range(3)]}),
                                      ("未校准不误入未知路线", base, {**inv, "did_calibration": False})):
    try:
        module.new_session(ACCOUNT, snapshot_, full, inventory_)
    except ValueError:
        check(reason, True)
    else:
        check(reason, False)

prepare = {**enter, "phase": "prepare_temporary", "temporary_entered": True}
active_intro = game("Intro", paused=True, pause_menu=pause(True), dialogue={"active": True, "choices": 0, "knot": "intro"})
step(prepare, active_intro, "skip", temp, temp_inv)
check("暂停时不把 Space 误作对话确认", True)
step(prepare, game("Intro", paused=True, pause_menu=pause(True, currentModeName=9)), "escape", temp, temp_inv)
check("设置子菜单先返回主页，不发 Quit 或 Skip", True)
step(prepare, game("Intro", paused=True, pause_menu=pause(True, inputsLocked=True)), None, temp, temp_inv)
check("暂停菜单锁定时等待", True)
wrong = profile()
wrong["achievements"]["account"] += 1
step(enter, base, None, wrong, inv)
check("Steam account 改变后不驱动原会话", True)

chosen = selection(selectedEntityIndex=1, lockedLevelPresses=21)
step(direct, chosen, "right", profile(0, False), inv)
check("超过 20 的计数通过移动角色重置", True)
seen = step(direct, selection(selectedEntityIndex=1, lockedLevelPresses=20), None, profile(0, False), inv)
seen = step(seen, base, None, profile(0, False), inv)
check("游戏已触发但 Steam 未更新时只等待确认", seen["phase"] == "verify_achievement" and seen["original_restored"])

artifacts = ROOT / "save-slot-simulation-artifacts"
artifacts.mkdir(exist_ok=True)
path = artifacts / f"session-{time.time_ns()}.json"
wrapper = module.SaveSlotNavigator(ACCOUNT, path)
wrapper.start_session(base, full, inv)
check("第一次动作前已持久化原存档", json.loads(path.read_text(encoding="utf-8"))["original_slot"] == 0)
check("新场景等待 3 秒", wrapper.decide(base, 0, full, inv)[0] is None)
for now in (3, 4, 5):
    key, _ = wrapper.decide(base, now, full, inv)
    check(f"可重试未生效动作 {now - 2}", key == "escape")
    wrapper.action_sent(base, key)
check("同状态三次未生效后停止重试", wrapper.decide(base, 6, full, inv)[0] is None)
restarted = module.SaveSlotNavigator(ACCOUNT, path)
check("程序重启优先恢复原存档", restarted.active and restarted.state["phase"] == "restore_original"
      and restarted.state["recovered_after_restart"] and restarted.state["original_slot"] == 0)
key, _ = restarted.decide(menu(section=2, currentSlotIndex=2), 0, profile(2), inventory(2))
key, _ = restarted.decide(menu(section=2, currentSlotIndex=2), 3, profile(2), inventory(2))
check("重启后按持久化 slot0 恢复，忽略菜单 slot2", key == "right" and restarted.state["original_slot"] == 0)
restarted.action_sent(menu(section=2, currentSlotIndex=2), key)
restarted.decide(base, 4, full, inv)
check("中断会话恢复后以 cancelled 结束", not restarted.active and restarted.state["phase"] == "cancelled")

report = {"passed": len(checks), "checks": checks, "live_input_sent": False,
          "game_memory_written": False, "game_save_written": False,
          "simulation_session": str(path)}
(ROOT / "save-slot-navigation-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
