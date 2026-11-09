"""在刷 S 流程后执行尚未解锁的 Steam 特殊成就。"""
import importlib.util
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
platinum = load("achievement_platinum", "rhythm-doctor-platinum.py")
achievements = load("achievement_status", "rhythm-doctor-achievements.py")
save_slots = load("achievement_save_slots", "rhythm-doctor-save-slot-navigation.py")
nav = platinum.nav

class AchievementNavigator(platinum.PlatinumNavigator):
    def __init__(self):
        super().__init__()
        self.achievement_task = None
        self.special_deadline = None
        self.special_scene = None
        self.sent_tasks = {}
        self.special_error = None
        self.slot_navigator = None

    def task_ready(self, snapshot, now, action):
        if snapshot.get("paused"):
            self.special_deadline = None
            return None, "游戏已暂停"
        scene = snapshot["scene_pointer"], self.achievement_task
        if scene != self.special_scene:
            self.special_scene, self.special_deadline = scene, now + 3
            self.special_error = None
        if self.special_error:
            return None, self.special_error
        if self.special_deadline is None:
            self.special_deadline = now + 3
        if now < self.special_deadline:
            return None, "成就关卡准备完成，3 秒后开始"
        return action, "开始成就挑战 · " + self.achievement_task

    def suspend_tutorial(self):
        super().suspend_tutorial()
        self.special_deadline = None

    def decide(self, snapshot, now, profile):
        status = profile.get("achievements")
        if status is None:
            return super().decide(snapshot, now, profile)
        account = status.get("account")
        if account and (self.slot_navigator is None or self.slot_navigator.account != account):
            self.slot_navigator = save_slots.SaveSlotNavigator(account)
        if self.slot_navigator and self.slot_navigator.active:
            self.achievement_task = "LevelSelectSpam"
            return self.slot_navigator.decide(snapshot, now, profile, inventory=profile.get("slot_inventory"))
        rows = status["rows"]
        missing = [key for key in achievements.SPECIAL if not rows[key]["achieved"]]
        name, level = snapshot["scene_type"], snapshot.get("level")
        if (self.achievement_task and self.achievement_task not in missing
                and not (name == "scnRhythmWeightlifter" and self.achievement_task == "LevelRhythmWeightlifterAny")):
            self.achievement_task = None
            self.special_scene = self.special_deadline = self.special_error = None
        if name == "scnRhythmWeightlifter":
            self.achievement_task = "LevelRhythmWeightlifterAny"
            return self.task_ready(snapshot, now, "play_weightlifter")
        if (name == "scnGame" and snapshot.get("level_type") == 0
                and snapshot.get("skip_to") == "LuckyBreak" and snapshot["two_player"]
                and "LevelLuckyBreakCoopPerfect" in missing):
            self.achievement_task = "LevelLuckyBreakCoopPerfect"
            # 游戏的教学跳过菜单同时适用于双人；保持运行中的双人模式。
            return self.tutorial({**snapshot, "two_player": False}, now)
        if name == "scnGame" and snapshot.get("level_type") != 0:
            if level == "AthleteTherapy" and "Eavesdropping" in missing:
                self.achievement_task = "Eavesdropping"
                if snapshot.get("rank_stage", 0) >= 2 or snapshot.get("failed"):
                    return super().decide(snapshot, now, profile)
                return self.task_ready(snapshot, now, "play_eavesdropping")
            if level == "LuckyBreak" and snapshot["two_player"] and "LevelLuckyBreakCoopPerfect" in missing:
                self.achievement_task = "LevelLuckyBreakCoopPerfect"
                if snapshot.get("rank_stage", 0) < 2 and not snapshot.get("failed"):
                    return self.task_ready(snapshot, now, "play_coop")
            if snapshot["two_player"] and level == "LuckyBreak":
                snapshot = {**snapshot, "two_player": False}
            return super().decide(snapshot, now, profile)
        if name != "scnLevelSelect":
            return super().decide(snapshot, now, profile)
        if snapshot["paused"]:
            return None, "游戏已暂停"
        if snapshot["dialogue"]["active"]:
            return ("space", "继续成就剧情") if snapshot["dialogue"]["choices"] <= 1 else (None, "等待选择剧情选项")
        self.pending_levels = platinum.unfinished_levels(snapshot, profile["ranks"], profile["levels"])
        ordinary = [item for item in self.pending_levels if item["automatic"]]
        if ordinary:
            panel = snapshot["monitor"]
            if panel["visible"] and panel["_twoPlayerSelected"] and panel.get("canChangePlayerMode", True):
                return "right", "恢复单人刷 S 模式"
            return super().decide({**snapshot, "two_player": False}, now, profile)
        if not missing:
            self.all_complete = not self.pending_levels and status["achieved"] == status["total"]
            if status["achieved"] != status["total"]:
                return None, "特殊成就已完成，等待 Steam 确认其余通关成就"
            return None, ("Steam 成就已全部解锁；全部已解锁关卡均达到 S" if self.all_complete else
                          "Steam 成就已全部解锁，等待剩余关卡")
        task = missing[0]
        if self.achievement_task != task:
            self.achievement_task = task
            self.special_scene = self.special_deadline = self.special_error = None
            self.last_signature, self.repeated, self.action_count = None, 0, 0
        if not snapshot["monitor"]["visible"] and any(snapshot[key] for key in
                ("firstFrame", "controlsAreLocked", "dialogueIsRunning", "enterIsLocked")):
            return None, "等待选关动画结束"
        if task in ("LevelSelectHeadache", "RhythmDogtor"):
            if snapshot["monitor"]["visible"]:
                return "escape", "关闭关卡面板以执行隐藏操作"
            if snapshot["currentWardIndex"] == 11:
                self.target = "Lesmis"
                return self.menu(snapshot)
            attempt = self.sent_tasks.get(task)
            if attempt and now - attempt[0] < 15:
                return None, "等待 Steam 确认成就"
            if attempt and attempt[1] >= 3:
                return None, "成就操作未得到 Steam 确认，等待处理"
            return ("achievement_headache" if task == "LevelSelectHeadache" else "achievement_dog_code"), "解锁 · " + rows[task]["name"]
        target = {"Eavesdropping": "AthleteTherapy", "LevelLuckyBreakCoopPerfect": "LuckyBreak",
                  "LevelRhythmWeightlifterAny": "RhythmWeightlifter"}.get(task)
        if target:
            attempt = self.sent_tasks.get(task)
            if attempt and now - attempt[0] < 15:
                return None, "等待 Steam 确认成就"
            if attempt and attempt[1] >= 3:
                return None, "成就挑战未得到 Steam 确认，等待处理"
            self.target = target
            return self.achievement_menu(snapshot, task in ("LevelLuckyBreakCoopPerfect", "LevelRhythmWeightlifterAny"))
        if task == "LevelSelectSpam" and self.slot_navigator:
            if (self.slot_navigator.state and self.slot_navigator.state["phase"] == "cancelled"
                    and self.slot_navigator.state.get("failure")):
                return None, "存档操作已恢复并停止 · " + str(self.slot_navigator.state.get("failure"))
            if "slot_inventory" not in profile:
                return None, "等待读取空白存档状态"
            self.slot_navigator.start_session(snapshot, profile, profile["slot_inventory"])
            return self.slot_navigator.decide(snapshot, now, profile, inventory=profile["slot_inventory"])
        return None, "等待读取 Steam 账户以执行隐藏操作"

    def achievement_menu(self, snapshot, coop):
        panel = snapshot["monitor"]
        if panel["visible"]:
            if panel["goingToLevel"]:
                return None, "等待进入成就关卡"
            if panel["levelToGo"] != self.target:
                return "escape", "关闭当前面板，选择成就关卡"
            if panel["currentSelected"] != 1:
                return ("down" if panel["currentSelected"] == 0 else "up"), "选择开始按钮"
            if panel["_twoPlayerSelected"] != coop:
                return "right", "切换到双人模式" if coop else "切换到单人模式"
            return ("space", "进入成就关卡 · " + self.target) if panel["canGoToLevel"] else (None, "等待开始按钮")
        return self.menu(snapshot)

    def action_sent(self, snapshot, key):
        if self.slot_navigator and self.slot_navigator.active:
            self.slot_navigator.action_sent(snapshot, key)
            return
        super().action_sent(snapshot, key)
        if key.startswith("achievement_"):
            previous = self.sent_tasks.get(self.achievement_task, (0, 0))
            self.sent_tasks[self.achievement_task] = time.monotonic(), previous[1] + 1

    def special_finished(self, result):
        status = result.get("status")
        self.special_deadline = time.monotonic() + 3
        if result.get("interruption") == "focus_lost" or status == "focus_lost":
            return
        if status in ("rank_shown", "campaign_complete"):
            previous = self.sent_tasks.get(self.achievement_task, (0, 0))
            self.sent_tasks[self.achievement_task] = time.monotonic(), previous[1] + 1
        if status in ("error", "blocked", "timed_out", "timeout", "level_failed"):
            self.special_error = "成就操作停止 · " + str(result.get("error") or result.get("reason") or status)
