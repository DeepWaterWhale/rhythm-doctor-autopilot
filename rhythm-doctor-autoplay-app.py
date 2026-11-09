"""常驻单人自动游玩面板：每秒检查准备状态，就绪三秒后开始。"""
import argparse
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
import queue
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parent
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

api = load("rd_autoplay", "rhythm-doctor-live-autoplay.py")
runtime = load("rd_runtime", "rhythm-doctor-runtime.py")
navigation = load("rd_navigation", "rhythm-doctor-navigation.py")
platinum = load("rd_platinum", "rhythm-doctor-platinum.py")
achievement_navigation = load("rd_achievement_navigation", "rhythm-doctor-achievement-navigation.py")
achievements = achievement_navigation.achievements
POLL_SECONDS = 1.0
START_DELAY = 3.0
TITLE = "节奏医生自动游玩"
MUTEX_NAME = "Local\\RhythmDoctorAutoplayApp"
STOP_NAME = "Local\\RhythmDoctorAutoplayAppStop"
K = runtime.K
K.CreateEventW.argtypes = [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR]
K.CreateEventW.restype = W.HANDLE
K.OpenEventW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
K.OpenEventW.restype = W.HANDLE
K.SetEvent.argtypes = [W.HANDLE]
K.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
K.WaitForSingleObject.restype = W.DWORD


class Controller:
    def __init__(self, native_stop, dry_run=False, auto_platinum=None):
        self.native_stop = native_stop
        self.dry_run = dry_run
        self.enabled = threading.Event()
        self.enabled.set()
        settings_path = ROOT / "app-settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        self.auto_platinum = threading.Event()
        if auto_platinum if auto_platinum is not None else settings.get("auto_platinum", False):
            self.auto_platinum.set()
        self.mode_revision = 0
        self.handoff_requested = None
        self.handoff_rank = None
        self.binding = None
        self.navigator = achievement_navigation.AchievementNavigator()
        self.shutdown = threading.Event()
        self.updates = queue.Queue()
        self.playing = False
        self.last_log_key = None
        self.context = {}
        self.last_result = None
        self.log_path = ROOT / ("app-session-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
        self.worker = threading.Thread(target=self.run, name="rhythm-monitor", daemon=True)

    def set_platinum(self, enabled):
        if enabled != self.auto_platinum.is_set():
            self.auto_platinum.set() if enabled else self.auto_platinum.clear()
            self.mode_revision += 1
            runtime.atomic_json(ROOT / "app-settings.json", {"auto_platinum": enabled})

    def watch_play(self, pid, binding, finished):
        """独立线程每秒观察剧情/已保存的 S，避免阻塞节拍输入线程。"""
        scene = navigation.SceneReader(api, pid, binding["classes"])
        try:
            while not finished.wait(POLL_SECONDS):
                if not self.auto_platinum.is_set() or self.stop_requested():
                    return
                try:
                    snapshot = scene.snapshot()
                    if snapshot["scene_type"] != "scnGame":
                        continue
                    if snapshot["dialogue"]["active"]:
                        self.handoff_requested = "dialogue"
                        return
                    profile = platinum.read_profile(scene, binding["identity"])
                    if platinum.reached_s(profile["ranks"].get(snapshot["level"])):
                        self.handoff_rank = profile["ranks"][snapshot["level"]]
                        self.handoff_requested = "reached_s"
                        return
                except (OSError, ValueError, RuntimeError, KeyError, IndexError):
                    continue
        finally:
            scene.close()

    def stop_requested(self):
        if K.WaitForSingleObject(self.native_stop, 0) == 0:
            self.shutdown.set()
        return self.shutdown.is_set() or not self.enabled.is_set()

    def publish(self, message, **data):
        state = {"time": datetime.now().astimezone().isoformat(), "app_pid": __import__("os").getpid(),
                 "message": message, "enabled": self.enabled.is_set(), "playing": self.playing,
                 "poll_seconds": POLL_SECONDS, "start_delay_seconds": START_DELAY,
                 "auto_platinum": self.auto_platinum.is_set(),
                 "last_result": self.last_result, **self.context, **data}
        runtime.atomic_json(ROOT / "app-status.json", state)
        self.updates.put(state)
        log_key = json.dumps([state.get(key) for key in
                              ("message", "pid", "level", "scene", "game_state", "phase", "rank_stage", "selected_entity")])
        if log_key != self.last_log_key or data.get("event") in ("run_finished", "auto_start", "auto_attach", "navigation_action"):
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(state, ensure_ascii=False) + "\n")
            self.last_log_key = log_key

    def on_play_event(self, event):
        kind = event["event"]
        if event.get("type") == "weightlifter":
            if kind in ("state", "progress", "round_complete", "round_failed"):
                snapshot = event.get("snapshot", {})
                level_number = snapshot["level_index"] + 1 if "level_index" in snapshot else event.get("level", 0)
                self.publish("节奏举重 · " + str(level_number) + " / 12",
                             event="weightlifter_" + kind)
            return
        if kind == "eavesdropping_state":
            snapshot = event["snapshot"]
            self.publish(event["decision"]["reason"], event="eavesdropping_progress")
            return
        if kind == "progress":
            message = (f"自动游玩中 · 分数 {event['special_score']} / 60" if "special_score" in event else
                       f"自动游玩中 · 成功 {event['successful_hits']} · 失误 {event['mistakes']:g}")
            self.publish(message, event="playing_progress")
        elif kind == "hold_release_checked":
            if not event["perfect"]:
                self.publish("自动游玩中 · 有长按松开判定未确认", event="hold_unverified")
        elif kind == "started":
            self.context["phase"] = "playing"
            self.publish("自动游玩中 · " + event["level"], event="playing_started", level=event["level"], entry=event.get("entry"))
        elif kind == "state":
            self.context["game_state"] = event["state"]
            self.publish("自动游玩中 · " + self.context.get("level", ""), event="playing_state")

    def profile(self, scene, identity):
        profile = platinum.read_profile(scene, identity)
        profile["achievements"] = achievements.read_status(scene, identity)
        profile["slot_inventory"] = achievement_navigation.save_slots.read_slot_inventory(scene, identity)
        return profile

    def run_special(self, action, pid, binding):
        revision = self.mode_revision
        def stop():
            return self.stop_requested() or self.mode_revision != revision or not self.auto_platinum.is_set()
        run_name = "app-achievement-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.playing = True
        self.context["phase"] = "achievement_playing"
        self.publish("正在完成 · " + str(self.navigator.achievement_task), event="special_started")
        result = None
        try:
            if action == "play_weightlifter":
                module = load("rd_weightlifter", "rhythm-doctor-weightlifter.py")
                result = module.run_campaign(api, pid, binding, stop_requested=stop, event_sink=self.on_play_event,
                                             log_path=ROOT / (run_name + ".jsonl"))
            elif action == "play_coop":
                module = load("rd_coop", "rhythm-doctor-coop-autoplay.py")
                result = module.run(api, pid, binding, stop_requested=stop, event_sink=self.on_play_event,
                                    start=True, no_activate=True, run_name=run_name)
            elif action == "play_eavesdropping":
                module = load("rd_eavesdropping", "rhythm-doctor-eavesdropping.py")
                result = module.run(api, pid, binding, stop_requested=stop, event_sink=self.on_play_event,
                                    start=True, run_name=run_name)
            else:
                raise ValueError("未知成就操作")
        except Exception as error:
            result = {"status": "error", "error": str(error)}
        finally:
            self.playing = False
        result.setdefault("level", {"play_weightlifter": "RhythmWeightlifter", "play_coop": "LuckyBreak",
                                    "play_eavesdropping": "AthleteTherapy"}[action])
        self.navigator.special_finished(result)
        self.last_result = {**result, "file": str(ROOT / (run_name + "-result.json"))}
        runtime.atomic_json(self.last_result["file"], result)
        self.publish("成就操作结束，检查 Steam 状态", event="special_finished")

    def navigate(self, pid, binding):
        if not self.auto_platinum.is_set():
            return False
        scene = navigation.SceneReader(api, pid, binding["classes"])
        try:
            snapshot = scene.snapshot()
            snapshot = achievement_navigation.save_slots.augment_snapshot(scene, snapshot)
            profile = self.profile(scene, binding["identity"])
            action, reason = self.navigator.decide(snapshot, time.monotonic(), profile)
            if action == "play":
                return False
            is_tutorial = snapshot["scene_type"] == "scnGame" and snapshot["level_type"] == 0
            self.context = {"pid": pid, "scene_type": snapshot["scene_type"], "scene_pointer": snapshot["scene_pointer"],
                            "level": snapshot.get("level"), "game_state": snapshot.get("game_state"),
                            "rank_stage": snapshot.get("rank_stage"),
                            "phase": "tutorial" if is_tutorial else "navigation",
                            "save_slot": profile["slot"], "target_level": self.navigator.target,
                            "remaining_levels": len(self.navigator.pending_levels)}
            self.context.update(achievements_unlocked=profile["achievements"]["achieved"],
                                achievements_total=profile["achievements"]["total"],
                                achievement_task=self.navigator.achievement_task)
            if is_tutorial:
                self.context["tutorial_target"] = snapshot.get("skip_to")
            if snapshot["scene_type"] == "scnLevelSelect":
                self.context["selected_entity"] = snapshot["entities"][snapshot["selectedEntityIndex"]]["id"]
            keyboard = api.keyboard_mod.Keyboard(pid)
            try:
                if not keyboard.is_foreground():
                    self.navigator.suspend_tutorial()
                    self.publish("已识别教学，等待游戏回到前台" if is_tutorial else "自动选关等待游戏回到前台")
                    return True
                if any(api.keyboard_mod.U.GetAsyncKeyState(key) & 0x8000 for key in (0x20, 0x0D, 0x1B, 0x25, 0x26, 0x27, 0x28, 0x53, 0x51)):
                    self.navigator.suspend_tutorial()
                    self.publish("自动选关等待松开按键")
                    return True
                self.context.update(previous_level=self.navigator.source, next_level=self.navigator.target)
                if action and not self.stop_requested():
                    # 即将发键前再次读取，不使用刚才画面的坐标或过期选中项。
                    fresh = scene.snapshot()
                    fresh = achievement_navigation.save_slots.augment_snapshot(scene, fresh)
                    fresh_profile = self.profile(scene, binding["identity"])
                    fresh_action, fresh_reason = self.navigator.decide(fresh, time.monotonic(), fresh_profile)
                    if (fresh_profile["slot"] != profile["slot"] or
                            (fresh_action, fresh_reason, fresh["scene_pointer"]) != (action, reason, snapshot["scene_pointer"])):
                        self.publish("导航状态已变化，等待下一次检查")
                        return True
                    if not self.navigator.permit(fresh, action):
                        self.publish("导航状态没有继续变化，等待手动处理", event="navigation_stalled")
                        return True
                    if not self.auto_platinum.is_set() or self.stop_requested():
                        return True
                    self.publish(reason + ("（仅观察）" if self.dry_run else ""), event="navigation_action", key=action)
                    if not self.dry_run:
                        if action.startswith("play_"):
                            self.run_special(action, pid, binding)
                        else:
                            revision = self.mode_revision
                            achievements.execute(keyboard, action, lambda: self.stop_requested() or self.mode_revision != revision
                                                 or not self.auto_platinum.is_set())
                            self.navigator.action_sent(fresh, action)
                else:
                    self.publish(reason)
                return True
            finally:
                keyboard.close()
        finally:
            scene.close()

    def run(self):
        gate = runtime.SessionGate(START_DELAY)
        binding = None
        retry_after = 0.0
        mode_revision = self.mode_revision
        try:
            while not self.shutdown.is_set():
                try:
                    if mode_revision != self.mode_revision:
                        mode_revision = self.mode_revision
                        gate = runtime.SessionGate(START_DELAY)
                        self.navigator = achievement_navigation.AchievementNavigator()
                    if self.stop_requested():
                        if self.shutdown.is_set():
                            break
                        gate.suspend()
                        self.navigator.suspend_tutorial()
                        self.publish("监听已暂停")
                        self.shutdown.wait(POLL_SECONDS)
                        continue
                    pid = runtime.game_pid()
                    if pid is None:
                        binding = None
                        gate = runtime.SessionGate(START_DELAY)
                        self.navigator = achievement_navigation.AchievementNavigator()
                        self.context = {}
                        self.publish("等待打开节奏医生")
                    else:
                        reader = api.sample.ins.mem.Reader(pid)
                        try:
                            identity = runtime.process_identity(reader)
                        finally:
                            reader.close()
                        if not binding or binding["identity"] != identity:
                            gate = runtime.SessionGate(START_DELAY)
                            self.navigator = achievement_navigation.AchievementNavigator()
                            self.context = {"pid": pid}
                            if time.monotonic() < retry_after:
                                self.shutdown.wait(POLL_SECONDS)
                                continue
                            try:
                                binding = runtime.resolve_layout(api, pid, self.publish)
                                self.binding = binding
                            except Exception:
                                retry_after = time.monotonic() + 15
                                raise
                            retry_after = 0.0
                        if self.navigate(pid, binding):
                            self.shutdown.wait(POLL_SECONDS)
                            continue
                        game = None
                        try:
                            game = api.Game(pid, binding["classes"])
                            observed = runtime.observe_scene(api, game)
                            scene, phase, reason = observed["scene"], observed["phase"], observed["reason"]
                            level = game.level
                            self.context = {"pid": pid, "level": level, **observed}
                            keyboard = api.keyboard_mod.Keyboard(pid)
                            foreground = keyboard.is_foreground()
                            space_down = bool(api.keyboard_mod.U.GetAsyncKeyState(0x20) & 0x8000)
                            action, remaining = gate.observe(observed, foreground, space_down, time.monotonic(),
                                                             allow_attach=self.auto_platinum.is_set(),
                                                             attach_current=self.auto_platinum.is_set())
                            if phase == "running" and not self.auto_platinum.is_set():
                                reason = "自动白金已关闭，等待当前关卡的开始界面"
                            if phase in ("ready", "running"):
                                if not foreground:
                                    reason = "请切回游戏窗口"
                                elif space_down:
                                    reason = "等待松开空格"
                                elif scene in gate.handled and not action:
                                    reason = "本局已处理，等待下一关的开始界面"
                        except (OSError, ValueError, RuntimeError, IndexError, KeyError) as error:
                            gate.gap()
                            self.context = {"pid": pid}
                            self.publish("等待选择正式关卡", detail=str(error))
                            self.shutdown.wait(POLL_SECONDS)
                            continue
                        finally:
                            if game:
                                game.reader.close()
                        if action and not self.stop_requested():
                            if self.dry_run:
                                self.publish("检测通过：此时会" + ("开始" if action == "start" else "接管") + "（仅观察）",
                                             event="dry_run_" + action, level=level)
                            else:
                                run_name = "app-play-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                                self.playing = True
                                self.publish(("3 秒已到，正在开始 · " if action == "start" else "正在接管已开始的关卡 · ") + level,
                                             event="auto_" + action, level=level)
                                result = None
                                self.handoff_requested = None
                                self.handoff_rank = None
                                run_revision = self.mode_revision
                                watch_finished = threading.Event()
                                watcher = None
                                if self.auto_platinum.is_set():
                                    watcher = threading.Thread(target=self.watch_play, args=(pid, binding, watch_finished),
                                                               name="platinum-state-watch", daemon=True)
                                    watcher.start()
                                try:
                                    expected_scene = scene
                                    def run_stop():
                                        return (self.stop_requested() or self.mode_revision != run_revision
                                                or self.handoff_requested is not None)
                                    def validate_start(candidate):
                                        current = runtime.observe_scene(api, candidate)
                                        allowed = ("ready", "running") if action == "start" else ("running",)
                                        return (current["scene"] == expected_scene and current["phase"] in allowed
                                                and runtime.process_identity(candidate.reader) == identity
                                                and not run_stop())
                                    result = api.main(["--pid", str(pid), "--start-or-attach" if action == "start" else "--attach-running",
                                                       "--play", "--no-activate",
                                                       "--layout-json", str(runtime.CACHE), "--max-seconds", "86400",
                                                       "--run-name", run_name],
                                                      stop_requested=run_stop, event_sink=self.on_play_event,
                                                      start_validator=validate_start)
                                    self.last_result = {"level": result["level"],
                                                        "status": "rank_saved" if self.handoff_requested == "reached_s" else result["status"],
                                                        "saved_rank": self.handoff_rank,
                                                        "metrics": result.get("metrics"),
                                                        "holds_accepted": result["holds_accepted"],
                                                        "holds_released_perfect": result["holds_released_perfect"],
                                                        "file": str(ROOT / (run_name + "-result.json")),
                                                        "error": result.get("error")}
                                finally:
                                    watch_finished.set()
                                    if watcher:
                                        watcher.join(timeout=2)
                                    self.playing = False
                                    retry_ready = bool(result and result.get("special_start_pending")
                                                       and not result.get("special_start_confirmed")
                                                       and result.get("interruption") == "focus_lost")
                                    gate.finished(scene, result["status"] if result else "error", retry_ready=retry_ready)
                                    if self.handoff_requested:
                                        gate.handled.discard(scene)
                                    self.context["phase"] = "run_finished"
                                self.publish("当前输入已结束，继续自动白金" if self.auto_platinum.is_set()
                                             else "本局已结束，等待手动选择关卡", event="run_finished")
                        elif remaining is not None:
                            self.publish(f"{math.ceil(remaining)} 秒后自动开始 · {level}", event="countdown", remaining=remaining, level=level)
                        else:
                            self.publish(reason, level=level)
                except Exception as error:
                    self.playing = False
                    gate.suspend()
                    self.publish("等待游戏就绪 · " + str(error), detail=traceback.format_exc())
                self.shutdown.wait(POLL_SECONDS)
        finally:
            self.playing = False
            self.publish("程序已停止", event="app_stopped")


def show_window(controller):
    import tkinter as tk
    from tkinter import ttk
    window = tk.Tk()
    window.title(TITLE)
    window.geometry("590x420")
    window.minsize(500, 280)
    window.configure(padx=22, pady=18)
    status = tk.StringVar(value="正在连接游戏…")
    last_result = tk.StringVar(value="尚无自动游玩结果")
    ttk.Label(window, text="节奏医生自动游玩", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
    ttk.Label(window, text="每秒检查一次 · 开始界面等待 3 秒后自动开始", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(6, 18))
    ttk.Label(window, textvariable=status, font=("Microsoft YaHei UI", 11), wraplength=535).pack(anchor="w", pady=(0, 12))
    ttk.Label(window, textvariable=last_result, wraplength=535).pack(anchor="w")
    ttk.Label(window, text="请保持游戏在前台。暂停监听或关闭本窗口会停止输入。", wraplength=535).pack(anchor="w", pady=(15, 12))
    def toggle_platinum():
        controller.set_platinum(not controller.auto_platinum.is_set())
        mode_button.configure(text="自动白金：开启" if controller.auto_platinum.is_set() else "自动白金：关闭")
        status.set("已切换模式，请保持游戏在前台")
    mode_button = ttk.Button(window, text="自动白金：开启" if controller.auto_platinum.is_set() else "自动白金：关闭",
                             command=toggle_platinum)
    mode_button.pack(anchor="w", pady=(0, 6))
    ttk.Label(window, text="开启：刷 S 并补齐 Steam 成就。关闭：只开始当前关卡。", wraplength=535).pack(anchor="w", pady=(0, 12))
    controls = ttk.Frame(window)
    controls.pack(anchor="w")
    def toggle():
        if controller.enabled.is_set():
            controller.enabled.clear()
            toggle_button.configure(text="继续监听")
            status.set("正在停止输入…")
        else:
            controller.enabled.set()
            toggle_button.configure(text="暂停监听")
            status.set("正在恢复监听…")
    toggle_button = ttk.Button(controls, text="暂停监听", command=toggle)
    toggle_button.pack(side="left", padx=(0, 10))
    closing = False
    def close():
        nonlocal closing
        if not closing:
            closing = True
            controller.shutdown.set()
            controller.enabled.clear()
            status.set("正在停止并释放按键…")
        if not controller.playing and not controller.worker.is_alive():
            window.destroy()
        else:
            window.after(50, close)
    ttk.Button(controls, text="退出", command=close).pack(side="left")
    window.protocol("WM_DELETE_WINDOW", close)
    def refresh():
        if closing:
            return
        if K.WaitForSingleObject(controller.native_stop, 0) == 0:
            close()
            return
        while True:
            try:
                item = controller.updates.get_nowait()
            except queue.Empty:
                break
            suffix = (f" · Steam 成就 {item['achievements_unlocked']}/{item['achievements_total']}"
                      if "achievements_total" in item else "")
            status.set(item["message"] + suffix)
            result = item.get("last_result")
            if result:
                metrics = result.get("metrics") or {}
                rank = result.get("saved_rank") or {15: "S+", 5: "S", -15: "S−"}.get(metrics.get("rank_raw"), str(metrics.get("rank_raw", "—")))
                if result["status"] in ("rank_shown", "rank_saved"):
                    last_result.set(f"上一关 {result['level']}：{rank} · 失误 {metrics.get('mistakes', '—')} · 长按 {result['holds_released_perfect']}/{result['holds_accepted']}")
                else:
                    last_result.set(f"上一关 {result.get('level', '成就挑战')}：{result.get('error') or result.get('reason') or result['status']}")
        window.after(200, refresh)
    controller.worker.start()
    window.after(200, refresh)
    window.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-platinum", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    if args.stop:
        event = K.OpenEventW(2, False, STOP_NAME)
        if event:
            K.SetEvent(event)
            K.CloseHandle(event)
        return
    if args.check:
        pid = runtime.game_pid()
        if pid is None:
            print(json.dumps({"pid": None, "status": "waiting_for_game"}))
            return
        binding = runtime.resolve_layout(api, pid, lambda text: print(text))
        try:
            game = api.Game(pid, binding["classes"])
        except (ValueError, RuntimeError, OSError) as error:
            print(json.dumps({"pid": pid, "ready": False, "reason": str(error), "status": "waiting_for_level"}, ensure_ascii=False))
            return
        try:
            observed = runtime.observe_scene(api, game)
            print(json.dumps({"pid": pid, "level": game.level, **observed, "ready": observed["phase"] == "ready",
                              "poll_seconds": POLL_SECONDS, "start_delay": START_DELAY}, ensure_ascii=False))
        finally:
            game.reader.close()
        return
    C.set_last_error(0)
    mutex = K.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex:
        raise C.WinError(C.get_last_error())
    if C.get_last_error() == 183:
        K.CloseHandle(mutex)
        return
    native_stop = K.CreateEventW(None, True, False, STOP_NAME)
    if not native_stop:
        K.CloseHandle(mutex)
        raise C.WinError(C.get_last_error())
    try:
        show_window(Controller(native_stop, args.dry_run, args.auto_platinum))
    finally:
        K.CloseHandle(native_stop)
        K.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        (ROOT / "app-launch-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
