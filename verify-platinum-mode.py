"""验证补 S 队列、原生退出流程与两种运行模式，不发送实际输入。"""
import copy
import importlib.util
import json
from pathlib import Path
import types

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("app", ROOT / "rhythm-doctor-autoplay-app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
p = app.platinum
checks = []
def check(name, actual, expected=True):
    assert actual == expected, (name, actual, expected)
    checks.append(name)

for rank in (5, 15, "S", "S+"):
    check("达到S-"+str(rank), p.reached_s(rank))
for rank in (-15, 14, 4, "S-", "A+", "NotFinished", "NotAvailable", None):
    check("未达S-"+str(rank), p.reached_s(rank), False)

profile = {"slot": 0, "path": "test-slot", "ranks": {"OrientalTechno": "S+", "OrientalDubstep": "NotFinished",
           "Intimate": "A+", "IntimateH": "S-", "OrientalInsomniac": "S"},
           "levels": {name: {"type": 2 if name == "OrientalInsomniac" else 1}
                      for name in ("OrientalTechno", "OrientalDubstep", "Intimate", "IntimateH", "OrientalInsomniac", "InsomniacHard")}}
entities = [dict(id=identifier, group=1, normalEnabled=True, hardEnabled=True, kind="SelectableCharacter")
            for identifier in ("1-1", "1-2", "1-X")]
menu = dict(scene_type="scnLevelSelect", scene_pointer=2, paused=False, two_player=False,
            dialogue=dict(active=False, choices=0, knot="", text=""), monitor=dict(visible=False, goingToLevel=False,
            canGoToLevel=True, levelToGo="Intimate", _twoPlayerSelected=False, currentSelected=1),
            entities=entities, selectedEntityIndex=1, currentWardIndex=1, currentDifficulty=1,
            controlsAreLocked=False, enterIsLocked=False, dialogueIsRunning=False, firstFrame=False,
            playerQuitLevel=False, voidItemProgress=0, selectedVerticalIndex=0, destinations=[])
pending = p.unfinished_levels(menu, profile["ranks"], profile["levels"])
check("流程排序且分别检查白班夜班", [x["level"] for x in pending], ["OrientalDubstep", "Intimate", "IntimateH"])
check("缺少存档key视为未解锁", "InsomniacHard" not in [x["level"] for x in pending])
locked = copy.deepcopy(menu); locked["entities"][0]["hardEnabled"] = False
check("尊重实际夜班入口状态", p.unfinished_levels(locked, profile["ranks"], profile["levels"])[0]["level"], "Intimate")
no_metadata = {**profile["levels"]}; no_metadata.pop("OrientalDubstep")
check("忽略无运行时metadata的配置残留", p.unfinished_levels(menu, profile["ranks"], no_metadata)[0]["level"], "Intimate")
planner = p.PlatinumNavigator()
check("没有历史关卡也可以自动选关", planner.decide(menu, 0, profile)[0], "left")
check("选择最前面的未达S关", planner.target, "OrientalDubstep")
at_target = {**menu, "selectedEntityIndex": 0}
check("切换到目标夜班", planner.decide(at_target, 1, profile)[0], "up")
check("正确班次打开关卡", planner.decide({**at_target, "currentDifficulty": 2}, 2, profile)[0], "space")
wrong_panel = {**menu, "monitor": {**menu["monitor"], "visible": True, "levelToGo": "OrientalTechno"}}
check("关闭已有S关的确认面板", planner.decide(wrong_panel, 3, profile)[0], "escape")
updated = copy.deepcopy(profile); updated["ranks"]["OrientalDubstep"] = "S+"
check("成绩变化后重新选未达S关", planner.decide(menu, 4, updated)[0], "space")
check("已达S不会重复入队", planner.target, "Intimate")

game = dict(scene_type="scnGame", scene_pointer=100, level="Intimate", game_state=3, level_type=1,
            paused=False, two_player=False, failed=False, loaded=True, rank_stage=0, rank_raw=0,
            rank_exiting=False, skippable_rank=True, pause_blocked=False,
            pause_menu=dict(showing=True, inputsLocked=False, currentModeName=1, usingOptionDetail=False),
            dialogue=dict(active=False, choices=0, knot="", text=""))
planner = p.PlatinumNavigator()
check("当前未达S运行关卡直接接管", planner.decide(game, 0, profile)[0], "play")
check("当前准备关卡交给三秒启动门控", planner.decide({**game, "game_state": 0}, 0, profile)[0], "play")
satisfied = {**game, "level": "OrientalTechno"}
check("已S的当前关先等待", planner.decide(satisfied, 1, profile)[0], None)
check("已S的当前关打开原生菜单", planner.decide(satisfied, 4, profile)[0], "escape")
check("用户自己暂停时不退出", planner.decide({**satisfied, "paused": True}, 5, profile)[0], None)
planner.action_sent(satisfied, "escape")
check("程序打开菜单后使用原生退出键", planner.decide({**satisfied, "paused": True}, 6, profile)[0], "quit")
planner.action_sent(satisfied, "quit")
check("等待退出不重复按键", planner.decide(satisfied, 7, profile)[0], None)
check("主动退出后的选关仍可继续", planner.decide({**menu, "playerQuitLevel": True}, 8, profile)[0], "left")
planner = p.PlatinumNavigator()
failed = {**game, "failed": True}
check("失败关卡等待返回选关", planner.decide(failed, 0, profile)[0], None)
check("失败关卡不会永久等待手动处理", planner.decide(failed, 3, profile)[0], "escape")
story = {**game, "dialogue": dict(active=True, choices=0, knot="Story", text="text")}
check("关卡内剧情交给导航", planner.decide(story, 0, profile)[0], "space")
check("多选剧情不代替用户选择", planner.decide({**story, "dialogue": {**story["dialogue"], "choices": 2}}, 0, profile)[0], None)
tutorial = {**game, "level": "IntimateTutorial", "level_type": 0, "skip_to": "Intimate", "tutorial_menu": game["pause_menu"]}
check("白金模式识别任意教学", planner.decide(tutorial, 0, profile)[0], None)
check("白金模式跳过当前教学", planner.decide(tutorial, 3, profile)[0], "escape")

gate = app.runtime.SessionGate(3)
observed = dict(scene="a", phase="running")
check("关闭白金不接管运行中的关卡", gate.observe(observed, True, False, 0, allow_attach=False)[0], None)
check("开启白金接管当前运行关卡", gate.observe(observed, True, False, 0, attach_current=True)[0], "attach")
check("开启后同一局只接管一次", gate.observe(observed, True, False, 1, attach_current=True)[0], None)
gate = app.runtime.SessionGate(3)
for second in (0, 1, 2):
    check("普通模式等待三秒-"+str(second), gate.observe(dict(scene="a", phase="ready"), True, False, second, allow_attach=False)[0], None)
check("普通模式第三秒开始当前关", gate.observe(dict(scene="a", phase="ready"), True, False, 3, allow_attach=False)[0], "start")
gate.finished("a", "rank_shown")
check("普通模式不接管下一运行关", gate.observe(dict(scene="b", phase="running"), True, False, 4, allow_attach=False)[0], None)
gate = app.runtime.SessionGate(3)
gate.observe(dict(scene="a", phase="ready"), True, False, 0, allow_attach=False)
check("关闭模式只监听开始界面", gate.observe(observed, True, False, 1, allow_attach=False)[0], None)

# 关闭白金时 Controller.navigate 必须在创建场景读取器/键盘前直接返回。
controller = app.Controller(0, dry_run=True, auto_platinum=False)
app.navigation.SceneReader = lambda *args: (_ for _ in ()).throw(AssertionError("关闭时不能导航"))
check("关闭模式不会执行选关或教学导航", controller.navigate(1, {}), False)
writes = []
app.runtime.atomic_json = lambda path, value: writes.append(value)
controller.set_platinum(True)
check("按钮开启模式", controller.auto_platinum.is_set())
check("模式切换使旧游玩会话失效", controller.mode_revision, 1)
controller.set_platinum(False)
check("按钮关闭模式", controller.auto_platinum.is_set(), False)
check("保存按钮状态", writes[-1], {"auto_platinum": False})

complete = copy.deepcopy(profile)
for name in complete["ranks"]: complete["ranks"][name] = "S"
planner = p.PlatinumNavigator()
check("全部已达S不再发导航键", planner.decide(menu, 0, complete)[0], None)
check("全部完成有明确状态", planner.all_complete)
other_slot = copy.deepcopy(profile); other_slot["slot"] = 1
planner.decide(menu, 1, other_slot)
check("切换存档后重新计算目标", planner.target, "OrientalDubstep")
check("切换存档不沿用完成状态", planner.all_complete, False)

summary = {"passed": len(checks), "checks": checks, "sent_real_input": False}
(ROOT / "platinum-mode-verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False))
