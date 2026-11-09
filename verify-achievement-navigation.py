"""验证成就计划、双人面板和准备门控；不连接或输入游戏。"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")
spec = importlib.util.spec_from_file_location("planner", ROOT / "rhythm-doctor-achievement-navigation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
checks = []
def check(name, value):
    assert value, name
    checks.append(name)
# 使用公开关卡映射构造测试场景，不读取玩家的真实存档或内存快照。
entities = [dict(id=identifier, group=item["group"], normalEnabled=True, hardEnabled=True,
                 kind="SelectableCharacter")
            for identifier, item in module.nav.SEQUENCE.items() if item.get("levels")]
levels = {level: {"type": 1, "id": identifier}
          for identifier, item in module.nav.SEQUENCE.items()
          for level in item.get("levels", {}).values() if level != "None"}
menu = dict(scene_type="scnLevelSelect", scene_pointer=100, paused=False, two_player=False,
            controlsAreLocked=False, dialogueIsRunning=False, enterIsLocked=False, firstFrame=False,
            currentWardIndex=8, currentDifficulty=1, selectedEntityIndex=0, selectedVerticalIndex=0,
            playerQuitLevel=False, voidItemProgress=0, destinations=[], entities=entities,
            dialogue={"active": False, "choices": 0},
            monitor=dict(visible=False, goingToLevel=False, currentSelected=1,
                         _twoPlayerSelected=False, canChangePlayerMode=True))
profile = {"slot": 0, "ranks": {level: "S+" for level in levels}, "levels": levels}
def fixture(task):
    value = copy.deepcopy(profile)
    value["achievements"] = {"total": 34, "achieved": 33,
                             "rows": {key: {"achieved": key != task, "name": key} for key in module.achievements.SPECIAL}}
    return module.AchievementNavigator(), copy.deepcopy(menu), value

for task, action in (("LevelSelectHeadache", "achievement_headache"), ("RhythmDogtor", "achievement_dog_code")):
    nav, shot, value = fixture(task)
    check(task + "使用专用操作", nav.decide(shot, 0, value)[0] == action)
    nav.sent_tasks[task] = (1, 1)
    check(task + "等待Steam确认", nav.decide(shot, 2, value)[0] is None)
    nav.sent_tasks[task] = (1, 3)
    check(task + "不无限重试", nav.decide(shot, 20, value)[0] is None)

nav, shot, value = fixture("LevelLuckyBreakCoopPerfect")
shot["monitor"].update(visible=True, levelToGo="LuckyBreak", canGoToLevel=True)
check("双人成就覆盖已有单人S", nav.decide(shot, 0, value)[0] == "right")
shot["monitor"]["_twoPlayerSelected"] = True
check("双人面板确认进入", nav.decide(shot, 1, value)[0] == "space")
shot["controlsAreLocked"] = True
check("确认面板显示时导航锁不阻止进入", nav.decide(shot, 1, value)[0] == "space")
shot.update(scene_type="scnGame", level="LuckyBreak", two_player=True, level_type=1, rank_stage=0, failed=False)
check("双人关准备仍等三秒", nav.decide(shot, 2, value)[0] is None)
check("双人关调用独立输入模块", nav.decide(shot, 5, value)[0] == "play_coop")
nav.suspend_tutorial()
check("失焦恢复重新等待", nav.decide(shot, 9, value)[0] is None)
check("恢复后满三秒再调用", nav.decide(shot, 12, value)[0] == "play_coop")

nav, shot, value = fixture("LevelLuckyBreakCoopPerfect")
shot.update(scene_type="scnGame", level="LuckyBreakTutorial", two_player=True,
            level_type=0, skip_to="LuckyBreak", loaded=True, failed=False, pause_blocked=False)
check("双人教学等待三秒", nav.decide(shot, 0, value)[0] is None)
check("双人教学可打开原生菜单", nav.decide(shot, 3, value)[0] == "escape")
nav.action_sent(shot, "escape")
shot.update(paused=True, tutorial_menu={"showing": True, "inputsLocked": False,
                                      "currentModeName": 1, "usingOptionDetail": False})
check("双人教学使用原生跳过选项", nav.decide(shot, 4, value)[0] == "skip")
check("教学判断不更改实际双人模式", shot["two_player"])

nav, shot, value = fixture("LevelRhythmWeightlifterAny")
shot["monitor"].update(visible=True, levelToGo="RhythmWeightlifter", _twoPlayerSelected=True, canGoToLevel=True)
check("举重强制双人面板可进入", nav.decide(shot, 0, value)[0] == "space")
shot["scene_type"] = "scnRhythmWeightlifter"
check("举重独立场景等待三秒", nav.decide(shot, 1, value)[0] is None)
check("举重调用独立模块", nav.decide(shot, 4, value)[0] == "play_weightlifter")
value["achievements"]["rows"]["LevelRhythmWeightlifterAny"]["achieved"] = True
nav = module.AchievementNavigator()
check("已解锁举重仍可正常退出小游戏", nav.decide(shot, 10, value)[0] is None
      and nav.decide(shot, 13, value)[0] == "play_weightlifter")

nav, shot, value = fixture("Eavesdropping")
shot.update(scene_type="scnGame", level="AthleteTherapy", level_type=4, rank_stage=0)
check("窃听不因已有S跳过", nav.decide(shot, 1, value)[0] is None)
check("窃听进入专用对白流程", nav.decide(shot, 4, value)[0] == "play_eavesdropping")
nav.special_finished({"status": "error", "error": "simulated"})
check("专用模块错误后不重复输入", nav.decide(shot, 1000000000, value)[0] is None)

nav, shot, value = fixture(None)
value["ranks"]["RhythmWeightlifter"] = "S+"
check("六项完成不冒充Steam全部完成", "全部解锁" not in nav.decide(shot, 0, value)[1] and not nav.all_complete)
value["achievements"]["achieved"] = 34
check("全部Steam和关卡完成才显示完成", "全部解锁" in nav.decide(shot, 1, value)[1] and nav.all_complete)

class SlotSession:
    account = 123
    active = True
    state = None
    started = False
    def decide(self, snapshot, now, profile, inventory=None):
        return "escape", "恢复原存档"
    def start_session(self, snapshot, profile, inventory):
        self.started = self.active = True

nav, shot, value = fixture(None)
value["achievements"].update(account=123, achieved=34)
value["slot_inventory"] = {}
session = nav.slot_navigator = SlotSession()
check("Steam已满仍优先恢复原存档", nav.decide(shot, 0, value)[1] == "恢复原存档" and not nav.all_complete)
session.active = False
session.state = {"phase": "cancelled", "failure": None}
value["achievements"]["rows"]["LevelSelectSpam"]["achieved"] = False
check("重启恢复且未发生错误后可继续成就", nav.decide(shot, 1, value)[1] == "恢复原存档" and session.started)
session.active = session.started = False
session.state["failure"] = "临时存档状态异常"
check("恢复后存在错误不继续使用新空槽", nav.decide(shot, 2, value)[0] is None and not session.started)

class Keys:
    def __init__(self): self.events = []
    def is_foreground(self): return True
    def set_key(self, key, down): self.events.append((key, down))
    def tap_key(self, key): self.events.append(key)
keys = Keys()
try:
    module.achievements.execute(keys, "achievement_headache", lambda: True)
except RuntimeError:
    pass
check("关闭模式后隐藏操作不发首个键", keys.events == [])

result = {"passed": len(checks), "checks": checks, "sent_real_input": False}
(ROOT / "achievement-navigation-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
