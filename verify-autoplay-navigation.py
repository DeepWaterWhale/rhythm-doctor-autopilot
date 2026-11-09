"""验证结算与选关决策，不向游戏发送输入。"""
import copy
import importlib.util
import json
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("nav", ROOT / "rhythm-doctor-navigation.py")
nav = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nav)
checks = []
def check(name, actual, expected):
    assert actual == expected, (name, actual, expected)
    checks.append(name)

rank = dict(scene_type="scnGame", scene_pointer=1, level="Bitterness", game_state=3, level_type=2,
            paused=False, two_player=False, failed=False, loaded=True, rank_stage=3, rank_raw=15,
            rank_exiting=False, skippable_rank=True, dialogue=dict(active=False, choices=0, knot="", text=""))
gate = nav.Navigator()
check("结算先等待三秒", gate.decide(rank, 0)[0], None)
check("结算未满三秒不发键", gate.decide(rank, 2)[0], None)
check("结算三秒后确认", gate.decide(rank, 3)[0], "space")
check("Bitterness主线后继为Montage", gate.target, "Montage")
for key in ("paused", "two_player", "failed", "rank_exiting"):
    blocked = {**rank, key: True}
    check("受阻时不确认-" + key, gate.decide(blocked, 5)[0], None)
check("失败评分不继续", gate.decide({**rank, "rank_raw": 0}, 6)[0], None)
check("结算离开过程中不乱按", gate.decide({**rank, "rank_stage": 4}, 6)[0], None)
story = {**rank, "level_type": 8, "level": "cutscene_rooftop", "rank_stage": 0}
story["dialogue"] = dict(active=True, choices=0, knot="Story", text="Hello")
check("单线剧情会推进", gate.decide(story, 7)[0], "space")
story["dialogue"]["choices"] = 2
check("多选剧情等待用户", gate.decide(story, 8)[0], None)
check("运行中的正式关卡不插入导航按键", gate.decide({**rank, "rank_stage": 0}, 9)[0], None)

def menu(level, selected, difficulty=1):
    entities = []
    for identifier, item in nav.SEQUENCE.items():
        entities.append(dict(id=identifier, group=item["group"], normalEnabled=item.get("normalEnabled", False),
                             hardEnabled=item.get("hardEnabled", False), kind="SelectableCharacter" if item["type"] == "character" else "SelectableObject",
                             action=item.get("action", "")))
    index = next(i for i, item in enumerate(entities) if item["id"] == selected)
    entities[index]["normalEnabled" if difficulty == 1 else "hardEnabled"] = True
    return dict(scene_type="scnLevelSelect", scene_pointer=2, paused=False, two_player=False,
                dialogue=dict(active=False, choices=0, knot="", text=""), monitor=dict(visible=False, goingToLevel=False,
                canGoToLevel=True, levelToGo=level, _twoPlayerSelected=False, currentSelected=1),
                entities=entities, selectedEntityIndex=index, currentWardIndex=entities[index]["group"],
                currentDifficulty=difficulty, controlsAreLocked=False, enterIsLocked=False,
                dialogueIsRunning=False, firstFrame=False, playerQuitLevel=False, voidItemProgress=0,
                selectedVerticalIndex=0, destinations=["ExitRecordsRoom", "ExitVoid"])

m = menu("Montage", "2-X", 2)
check("Bitterness通关先去出口", gate.decide(m, 10)[1], "移动到 · ExitSVTWard")
m = menu("Montage", "ExitSVTWard", 2)
check("确认病房出口以触发天台剧情", gate.decide(m, 11)[0], "space")
m = menu("Montage", "MainElevator")
check("电梯选择终章目的地", gate.decide(m, 12)[0], "up")
m["selectedVerticalIndex"] = 1
check("电梯终章目的地确认", gate.decide(m, 13)[0], "space")
m["destinations"] = ["ExitRecordsRoom"]
m["selectedVerticalIndex"] = 0
check("没有终章选项时不确认", gate.decide(m, 14)[0], None)
for index in (1, 2, 3):
    m = menu("Montage", "VoidItem" + str(index))
    m["voidItemProgress"] = index - 1
    for entity in m["entities"]:
        if entity["id"] == "7-X": entity["normalEnabled"] = False
        if entity["id"] == "VoidItem" + str(index): entity["normalEnabled"] = True
    check("按剧情进度确认终章道具-" + str(index), gate.decide(m, 15 + index)[0], "space")
m = menu("Montage", "7-X")
for entity in m["entities"]:
    if entity["id"] == "7-X": entity["normalEnabled"] = True
check("已在下一关时直接打开确认面板", gate.decide(m, 20)[0], "space")
m["monitor"]["visible"] = True
check("单人开始面板确认", gate.decide(m, 21)[0], "space")
for key, value in (("goingToLevel", True), ("canGoToLevel", False), ("_twoPlayerSelected", True),
                   ("currentSelected", 2), ("levelToGo", "WrongLevel")):
    changed = copy.deepcopy(m)
    changed["monitor"][key] = value
    check("面板受阻不误选-" + key, gate.decide(changed, 22)[0], None)
m["monitor"]["visible"] = False
for key in ("controlsAreLocked", "dialogueIsRunning", "firstFrame", "enterIsLocked", "playerQuitLevel"):
    check("选关受阻不发键-" + key, gate.decide({**m, key: True}, 23)[0], None)
gate.complete("Blurred")
check("Blurred下一关为Bitterness", gate.target, "Bitterness")
m = menu("Bitterness", "ExitRecordsRoom")
check("Blurred先确认特殊剧情出口", gate.decide(m, 24)[0], "space")
gate.complete("OrientalTechno")
check("普通主线使用游戏解锁顺序", gate.target, "Intimate")
m = menu("Intimate", "1-2")
check("游戏已移到下一关时不额外按右", gate.decide(m, 25)[0], "space")
gate.complete("Montage2")
check("主线末尾无后继时停住", gate.target, None)
gate = nav.Navigator()
check("新启动且未观察过关结果不乱选关", gate.decide(m, 26)[0], None)
for number in range(1, 5):
    check("相同界面重试限制-" + str(number), gate.permit(m, "space"), number <= 3)
check("状态变更后允许下一步", gate.permit({**m, "selectedEntityIndex": 0}, "right"), True)
reader = nav.SceneReader.__new__(nav.SceneReader)
reader.layout = {"scrGameManager": 100}
values = {(None, "instance"): 1, (1, "inkDialogue"): 2, (2, "isPlaying"): True,
          (2, "activeDialoguePresent"): True, (2, "visible"): True, (2, "currentKnot"): "Test",
          (2, "story"): 0, (2, "dialogueTextLatinPixel"): 3, (2, "dialogueTextVector"): 4,
          (2, "notification_textUI"): 5, (3, "m_Text"): "", (4, "m_text"): "第一句", (5, "m_Text"): ""}
reader.get = lambda obj, name, cls=None: values[(obj, name)]
first = reader.dialogue()
values[(4, "m_text")] = "第二句"
check("中文TMP对话变化能被观测", first["text"] != reader.dialogue()["text"], True)
gate = nav.Navigator()
gate.complete("Bitterness")
tutorial = {**rank, "level_type": 0, "level": "MontageTutorial", "rank_stage": 0, "skip_to": "Montage",
            "pause_blocked": False,
            "tutorial_menu": dict(showing=True, inputsLocked=False, currentModeName=1, usingOptionDetail=False)}
check("教学先等待三秒", gate.decide(tutorial, 30)[0], None)
check("已确认后继的教学可打开菜单", gate.decide(tutorial, 33)[0], "escape")
check("不接管用户已暂停的教学", gate.decide({**tutorial, "paused": True}, 31)[0], None)
gate.permit(tutorial, "escape")
check("仅许可按键不取得教学暂停所有权", gate.owned_tutorial, None)
gate.action_sent(tutorial, "escape")
check("程序打开的教学菜单使用跳过键", gate.decide({**tutorial, "paused": True}, 32)[0], "skip")
check("正式关不会使用跳过键", gate.decide({**tutorial, "level_type": 2}, 33)[0], None)
check("未知教学后继不打开菜单", gate.decide({**tutorial, "skip_to": "Other"}, 34)[0], None)

intimate = {**tutorial, "scene_pointer": 20, "level": "IntimateTutorial", "skip_to": "Intimate"}
for source_level in (None, "HelpingHands", "Bitterness"):
    gate = nav.Navigator()
    if source_level:
        gate.complete(source_level)
    label = source_level or "无历史"
    check("手动选择的教学开始倒计时-" + label, gate.decide(intimate, 0)[0], None)
    check("重复读取不重置教学倒计时-" + label, gate.decide(intimate, 2)[0], None)
    check("教学不依赖上一关路线-" + label, gate.decide(intimate, 3)[0], "escape")
    check("跳过前保留实际历史关卡-" + label, gate.source, source_level)
    gate.action_sent(intimate, "escape")
    check("教学菜单跳转到自身目标-" + label,
          gate.decide({**intimate, "paused": True}, 4), ("skip", "使用游戏的跳过教学选项 · Intimate"))
    gate.action_sent(intimate, "skip")
    check("记录实际教学目标-" + label, gate.target, "Intimate")
    check("跳转过程中不重复按Esc-" + label, gate.decide(intimate, 5)[0], None)
    check("跳转过程中不重复按S-" + label, gate.decide({**intimate, "paused": True}, 6)[0], None)
    next_tutorial = {**intimate, "scene_pointer": 21}
    check("新教学重新倒计时-" + label, gate.decide(next_tutorial, 10)[0], None)
    check("新教学重新允许跳过-" + label, gate.decide(next_tutorial, 13)[0], "escape")

for field, value in (("two_player", True), ("loaded", False), ("failed", True),
                     ("pause_blocked", True), ("skip_to", None), ("skip_to", "None"),
                     ("skip_to", "IntimateTutorial")):
    gate = nav.Navigator()
    blocked = {**intimate, field: value}
    gate.decide(blocked, 0)
    check("教学门控-" + field + "-" + str(value), gate.decide(blocked, 5)[0], None)

gate = nav.Navigator()
gate.decide(intimate, 0)
gate.suspend_tutorial()
check("失焦或暂停监听后教学重新计时", gate.decide(intimate, 8)[0], None)
check("教学恢复未满三秒不操作", gate.decide(intimate, 10)[0], None)
check("教学恢复满三秒可操作", gate.decide(intimate, 11)[0], "escape")
gate.action_sent(intimate, "escape")
for field, value in (("showing", False), ("inputsLocked", True), ("currentModeName", 2), ("usingOptionDetail", True)):
    blocked = {**intimate, "paused": True, "tutorial_menu": {**intimate["tutorial_menu"], field: value}}
    check("教学菜单门控-" + field, gate.decide(blocked, 12)[0], None)
check("教学菜单未创建时等待", gate.decide({**intimate, "paused": True, "tutorial_menu": None}, 13)[0], None)
summary = {"passed": len(checks), "checks": checks, "sent_real_input": False}
(ROOT / "autoplay-navigation-verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False))
