"""离线验证：不连接游戏、不发送键盘输入。"""
import importlib.util
import json
import math
from pathlib import Path
import statistics
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("wl", ROOT / "rhythm-doctor-weightlifter.py")
wl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wl)


def simulate(weights, fps):
    schedule = wl.RatioSchedule(weights, 0)
    y = [0.0, 0.0]
    hit_times = [[], []]
    pending = set()
    previous_frame = -1
    last_frame_time = 0
    completed = False
    maximum_imbalance = 0
    for step in range(12000):
        now = step * .002
        pending.update(schedule.due(now))
        frame = int(now * fps)
        if frame == previous_frame:
            continue
        previous_frame = frame
        game_time = frame / fps
        dt = game_time - last_frame_time
        last_frame_time = game_time
        for i, key in enumerate(("p1", "p2")):
            if key in pending:
                y[i] = min(1 + 1 / 305, y[i] + .16393442 / weights[i])
                hit_times[i].append(game_time)
        pending.clear()
        if min(y) >= 1:
            completed = True
            break
        gravity = sum(weights) * 255 / 5000 * .8 * dt / 16 / 305 * 1000
        y = [max(0, height - gravity) for height in y]
        maximum_imbalance = max(maximum_imbalance, abs(y[0] - y[1]))
        threshold = 1.25 * (10 / 51) / max(1, min(weights) / 2)
        assert abs(y[0] - y[1]) <= threshold, (weights, fps, "失衡")
        for hits in hit_times:
            if len(hits) > 1:
                assert game_time - hits[-1] < (hits[-1] - hits[-2]) * 1.5, (weights, fps, "节奏中断")
    assert completed, (weights, fps, "未完成")
    consistency = []
    for hits in hit_times:
        intervals = [b - a for a, b in zip(hits, hits[1:])]
        differences = [a / b - 1 for a, b in zip(intervals, intervals[1:])]
        sd = statistics.pstdev(differences)
        frequency = len(hits) / (game_time - min(t[0] for t in hit_times))
        low, high = max(.5, .5 * frequency / 4), max(.1, .1 * frequency / 4)
        consistency.append(min(50, max(0, round((sd - low) / (high - low) * 50))))
    assert sum(consistency) == 100, (weights, fps, consistency)
    # 与 CalculateSpeedScore 的离散 60 Hz 公式一致。
    maximum_weight = max(weights)
    def reference_duration(frequency):
        frames = 0
        height = 0
        while height <= 255:
            frames += 1
            drop = sum(weights) * 255 / 5000 * .8 * frames
            height = 50 / maximum_weight * (frequency * frames / 60) - drop
            assert frames < 100000
        return frames / 60
    best = reference_duration(min(4.3, maximum_weight * 1.333))
    worst = reference_duration(maximum_weight * .666)
    elapsed = game_time - min(t[0] for t in hit_times)
    speed = min(100, max(0, round((elapsed - worst) / (best - worst) * 100)))
    assert speed == 100, (weights, fps, speed)
    return {"weights": weights, "fps": fps, "seconds": game_time,
            "max_imbalance": maximum_imbalance, "consistency": consistency, "speed": speed}


class FakeTime:
    def __init__(self):
        self.now = 0.0
    def perf_counter(self):
        return self.now
    def sleep(self, duration):
        self.now += duration


def verify_stop(focus_loss=False, paused=False, other_scene=False):
    fake_time = FakeTime()
    state = {"scene_type": "scnGame" if other_scene else wl.SCENE, "scene_pointer": 1,
             "screen": "intro", "level_index": 0, "last_unlocked": 0, "game_state": 0,
             "showing_score": False, "dialogue_playing": False, "infinite": False,
             "paused": paused, "campaign_complete": False, "failure": 0,
             "can_receive_input": True, "transition_playing": False,
             "p1": {"hit_list": 1}, "p2": {"hit_list": 2}, "extra_slot": -1}
    all_keys = []
    class Reader:
        def __init__(self, *args):
            self.closed = False
        def snapshot(self):
            return state.copy()
        def close(self):
            self.closed = True
    class Keys:
        def __init__(self, *args):
            self.held = {}
            self.pressed = 0
            all_keys.append(self)
        def check_idle(self):
            pass
        def is_foreground(self):
            return not focus_loss or fake_time.now < .2
        def release_due(self, now):
            self.held = {k: v for k, v in self.held.items() if now < v}
        def press(self, names, now, duration=.032):
            self.held.update({k: now + duration for k in names})
            self.pressed += len(names)
        def close(self):
            self.held.clear()
    old = wl.time, wl.WeightlifterReader, wl.WeightlifterKeys, wl.ctypes.WinDLL
    wl.time, wl.WeightlifterReader, wl.WeightlifterKeys = fake_time, Reader, Keys
    wl.ctypes.WinDLL = lambda name: SimpleNamespace(timeBeginPeriod=lambda value: 0, timeEndPeriod=lambda value: 0)
    try:
        result = wl.run_campaign(SimpleNamespace(keyboard_mod=None), 123, {},
                                 stop_requested=lambda: not focus_loss and fake_time.now >= .2, max_seconds=1)
    finally:
        wl.time, wl.WeightlifterReader, wl.WeightlifterKeys, wl.ctypes.WinDLL = old
    expected = "scene_changed" if other_scene else "blocked" if paused else "focus_lost" if focus_loss else "cancelled"
    assert result["status"] == expected, result
    assert all(not keys.held for keys in all_keys), "停止后仍持有按键"
    if paused or other_scene:
        assert all(keys.pressed == 0 for keys in all_keys), "不允许接管时发出了按键"
    else:
        assert all_keys[0].pressed == 2, "没有覆盖 Intro 双键持有期间的停止路径"
    return expected


def verify_exit_transition(mode):
    fake_time = FakeTime()
    state = {"scene_type": wl.SCENE, "scene_pointer": 1,
             "screen": "intro", "level_index": 11, "last_unlocked": 12, "game_state": 0,
             "showing_score": False, "dialogue_playing": False, "infinite": False,
             "paused": False, "campaign_complete": True, "failure": 0,
             "can_receive_input": True, "transition_playing": False,
             "p1": {"hit_list": 1}, "p2": {"hit_list": 2}, "extra_slot": -1}
    all_keys, observed = [], []
    class Reader:
        def __init__(self, *args):
            self.calls = 0
        def snapshot(self):
            self.calls += 1
            if self.calls == 1 and mode != "ordinary_error":
                return state.copy()
            if mode in ("ordinary_error", "no_recovery") or fake_time.now < .12:
                raise OSError(299, "退出期间旧场景对象失效")
            if fake_time.now < .24:
                return {"scene_type": "None", "scene_pointer": 0}
            observed.append("scnLevelSelect")
            return {"scene_type": "scnLevelSelect", "scene_pointer": 2}
        def close(self):
            pass
    class Keys:
        def __init__(self, *args):
            self.held = {}
            self.actions = []
            all_keys.append(self)
        def check_idle(self):
            pass
        def is_foreground(self):
            return True
        def release_due(self, now):
            self.held = {k: v for k, v in self.held.items() if now < v}
        def press(self, names, now, duration=.032):
            self.actions.extend(names)
            self.held.update({k: now + duration for k in names})
        def close(self):
            self.held.clear()
    old = wl.time, wl.WeightlifterReader, wl.WeightlifterKeys, wl.ctypes.WinDLL
    wl.time, wl.WeightlifterReader, wl.WeightlifterKeys = fake_time, Reader, Keys
    wl.ctypes.WinDLL = lambda name: SimpleNamespace(timeBeginPeriod=lambda value: 0, timeEndPeriod=lambda value: 0)
    try:
        result = wl.run_campaign(SimpleNamespace(keyboard_mod=None), 123, {}, max_seconds=7,
                                 stop_requested=lambda: mode == "cancel" and fake_time.now > .05)
    finally:
        wl.time, wl.WeightlifterReader, wl.WeightlifterKeys, wl.ctypes.WinDLL = old
    expected = "campaign_complete" if mode == "recover" else "cancelled" if mode == "cancel" else "blocked"
    assert result["status"] == expected, (mode, result)
    assert result["level"] == "RhythmWeightlifter"
    assert all(not keys.held for keys in all_keys)
    if mode == "recover":
        assert observed == ["scnLevelSelect"], "未确认新场景便报告完成"
        assert all_keys[0].actions == ["escape"], "退出期间依据旧场景重复发键"
    if mode == "ordinary_error":
        assert not all_keys[0].actions, "一般读取错误触发了按键"
    return {"mode": mode, "status": result["status"]}


results = [simulate([a, b], fps) for fps in (60, 120) for a in range(1, 7) for b in range(1, 7)]
stop_results = [verify_stop(), verify_stop(focus_loss=True), verify_stop(paused=True), verify_stop(other_scene=True)]
transition_results = [verify_exit_transition(mode) for mode in ("recover", "no_recovery", "ordinary_error", "cancel")]
try:
    wl.RatioSchedule([1, 6], 0).due(.3)
    raise AssertionError("迟到输入没有停止")
except RuntimeError:
    pass
output = {"simulation_cases": len(results), "all_complete": True, "consistency_100": True, "speed_100": True,
          "slowest_seconds": max(r["seconds"] for r in results), "stop_paths": stop_results,
          "late_input_stops": True, "exit_transition_tests": transition_results,
          "live_input_tested": False, "results": results}
(ROOT / "weightlifter-adapter-verification.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in output.items() if k != "results"}, ensure_ascii=False, indent=2))
