"""仅在假游戏、假时钟和假键盘中验证专用 runner；不导入真实输入模块。"""
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("eaves_sim", ROOT / "rhythm-doctor-eavesdropping.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ARTIFACTS = ROOT / "eavesdropping-simulation-artifacts"
ARTIFACTS.mkdir(exist_ok=True)
module.ROOT = ARTIFACTS


class World:
    def __init__(self, mode="complete"):
        self.mode = mode
        self.now = 0.0
        self.phase = self.reps = self.progress = self.completed = 0
        self.started = mode != "start"
        self.dialogue = "Intro0"
        self.story_end = self.phase_end = None
        self.finished = False
        self.held = False
        self.hold = self.released_at = None
        self.next_target = 0.0
        self.serial = 1
        self.keys = []
        self.snapshots = []
        self.reader_closed = self.scene_closed = self.keyboard_closed = False
        self.misses = self.stale_targets_pressed = 0

    def advance(self, amount):
        self.now += max(amount, .000001)
        if self.released_at is not None and self.now >= self.released_at + 1 / 60:
            if self.mode == "release_timeout":
                return
            self.hold = None
            self.released_at = None
            self.reps += 1
            self.completed += 1
            self.serial += 1
            self.next_target = self.now + .15
            if self.phase in module.STORY_PHASES and self.reps == 5:
                self.dialogue = f"Phase{module.STORY_PHASES[self.phase]}Freeform1"
                self.story_end = self.now + 5.0
            goal = 20 if self.phase in module.STORY_PHASES else 5
            if self.reps == goal:
                self.phase_end = self.now + 4.5
        if self.story_end is not None and self.now >= self.story_end:
            self.progress += 1
            self.dialogue = ""
            self.story_end = None
        if self.phase_end is not None and self.now >= self.phase_end:
            self.phase_end = None
            if self.phase == 5:
                self.finished = True
            else:
                self.phase += 1
                self.reps = 0
                self.dialogue = f"Intro{self.phase}"
                self.next_target = self.now + .15

    def snapshot(self):
        self.snapshots.append((self.now, self.dialogue))
        freeform = self.dialogue in module.FREEFORM_KNOTS
        return {"scene_type": "scnGame", "level": module.LEVEL, "loaded": True,
                "two_player": False, "paused": False, "failed": False,
                "game_state": 6 if self.finished else 3 if self.started else 0,
                "rank_stage": 2 if self.finished else 0, "ignore_input": False,
                "phase_index": self.phase, "reps": self.reps,
                "rep_goal": 20 if self.phase in module.STORY_PHASES else 5,
                "eavesdropping_progress": self.progress,
                "bar": 24 if self.finished else 2 + self.phase * 4,
                "therapy_dialogue": {"playing": bool(self.dialogue), "visible": True,
                                     "active": bool(self.dialogue) and not freeform,
                                     "knot": self.dialogue, "choices": 0}}


class Memory:
    def __init__(self, world):
        self.world = world

    def read(self, address, count):
        if count == 1:
            return bytes([1])
        if count == 8:
            return struct.pack("<d", 0.0)
        raise AssertionError((address, count))

    def close(self):
        self.world.reader_closed = True
        if self.world.mode == "reader_close_error":
            raise RuntimeError("simulated reader cleanup failure")


class Game:
    level = module.LEVEL
    bf = {"playerDrives7thBeat": 1, "playerDriven7thBeatHitTime": 8}

    def __init__(self, world):
        self.w = world
        self.reader = Memory(world)

    def scene_changed(self):
        return self.w.mode == "scene_changed" and self.w.now >= 2.0

    def state(self):
        return {"beats": 1, "paused": False, "failed": False, "ignore_input": False}

    def clock(self):
        return self.w.now, int(self.w.now * 60)

    def beats(self, _):
        w = self.w
        if w.hold or w.phase_end or w.dialogue or w.finished:
            return [], 0
        return [{"pointer": hex(4096 + w.serial * 32), "row": 0, "hold": True,
                 "target": w.next_target, "release": w.next_target + .5}], 1

    def active_holds(self, _):
        w = self.w
        if not w.hold or w.now < w.hold["pressed"] + 1 / 60 or w.mode == "no_acknowledgment":
            return []
        return [w.hold.copy()]

    def row_history(self, state, row):
        return {"count": self.w.completed, "last": 0 if self.w.completed else None}

    def hold_release_result(self, _):
        return 0

    def metrics(self, _):
        if self.w.mode == "metrics_error":
            raise RuntimeError("simulated stale metrics")
        return {"mistakes": self.w.misses, "successful_hits": self.w.completed}


class Keyboard:
    def __init__(self, world):
        self.w = world
        self.held = False

    def is_foreground(self):
        if self.w.mode == "initial_focus_lost":
            return False
        return not (self.w.mode in ("focus_lost", "focus_lost_keyboard_error") and self.w.hold and
                    self.w.now >= self.w.hold["pressed"] + .1)

    def tap_start(self):
        assert not self.held
        self.w.keys.append(("start", self.w.now))
        self.w.started = True

    def tap_key(self, name):
        assert name == "space" and self.w.dialogue.startswith("Intro")
        self.w.keys.append(("dialogue", self.w.now, self.w.dialogue))
        self.w.dialogue = ""

    def set_space(self, down):
        w = self.w
        if self.held == down:
            return
        self.held = w.held = down
        w.keys.append(("down" if down else "up", w.now, w.phase, w.reps, w.progress))
        if down:
            assert w.started and not w.dialogue and not w.phase_end
            required = module.STORY_PHASES.get(w.phase)
            assert not (required and w.reps >= 5 and w.progress < required), "未完成对白便继续练习"
            w.stale_targets_pressed += w.now - w.next_target > .15
            w.hold = {"pointer": hex(4096 + w.serial * 32), "row": 0, "hold": True,
                      "pressed": w.now, "target": w.now, "release": w.now + .5}
        else:
            if w.hold:
                w.misses += abs(w.now - w.hold["release"]) > .025
            w.released_at = w.now

    def close(self):
        self.set_space(False)
        self.w.keyboard_closed = True
        if self.w.mode in ("keyboard_close_error", "focus_lost_keyboard_error"):
            raise RuntimeError("simulated keyboard cleanup failure")


def simulate(mode, sink=None):
    world = World(mode)
    module.time = SimpleNamespace(monotonic=lambda: world.now, sleep=world.advance,
                                  strftime=lambda _: "simulation")

    class Reader:
        def __init__(self, api, pid, binding):
            self.runtime = SimpleNamespace(observe_scene=lambda *_: {"phase": "ready", "reason": ""})

        def snapshot(self):
            return world.snapshot()

        def close(self):
            world.scene_closed = True

    module.EavesdroppingReader = Reader
    api = SimpleNamespace(Game=lambda *_: Game(world), keyboard_mod=SimpleNamespace(
        Keyboard=lambda *_: Keyboard(world), U=SimpleNamespace(GetAsyncKeyState=lambda _: 0)))
    stop = lambda: mode == "cancelled" and world.hold is not None and world.now >= world.hold["pressed"] + .1
    result = module.run(api, 1, {"identity": [1, 2], "classes": {}}, stop_requested=stop,
                        event_sink=sink, start=mode == "start", max_seconds=300,
                        run_name="sim-" + mode)
    assert not world.held
    assert world.reader_closed and world.scene_closed and world.keyboard_closed
    assert Path(result["file"]).exists()
    return world, result


checks = []
for mode in ("complete", "start", "metrics_error", "reader_close_error", "keyboard_close_error",
             "cancelled", "focus_lost", "initial_focus_lost", "focus_lost_keyboard_error",
             "scene_changed", "no_acknowledgment", "release_timeout", "sink_error"):
    def failing_sink(event):
        raise RuntimeError("simulated event callback failure")

    world, result = simulate(mode, failing_sink if mode == "sink_error" else None)
    expected = {"keyboard_close_error": "error", "cancelled": "cancelled", "focus_lost": "focus_lost",
                "initial_focus_lost": "focus_lost", "focus_lost_keyboard_error": "error",
                "scene_changed": "scene_changed", "no_acknowledgment": "error", "release_timeout": "error"}
    assert result["status"] == expected.get(mode, "rank_shown"), (mode, result)
    if mode in ("focus_lost", "initial_focus_lost"):
        assert result["interruption"] == "focus_lost"
    else:
        assert "interruption" not in result
    if result["status"] == "rank_shown":
        assert result["actions"] == 75
        assert result["holds_accepted"] == result["holds_released_perfect"] == 75
        assert result["holds_release_unverified"] == 0
        assert result["eavesdropping_progress"] == 3 and result["grant_point_reached"]
        assert world.misses == 0 and world.stale_targets_pressed >= 3
        # 等待被动对白时状态读取间隔为一秒；每段首次状态读取可能紧随松开。
        passive = [(stamp, knot) for stamp, knot in world.snapshots if knot in module.FREEFORM_KNOTS]
        for (before, knot), (after, next_knot) in zip(passive, passive[1:]):
            if knot == next_knot:
                assert after - before >= .999, (mode, before, after)
    if mode == "start":
        assert sum(event[0] == "start" for event in world.keys) == 1
    if mode == "metrics_error":
        assert result["metrics"] is None and "metrics_error" in result
    if mode in ("reader_close_error", "keyboard_close_error", "focus_lost_keyboard_error"):
        assert result["cleanup_errors"]
    if mode == "sink_error":
        assert result["event_sink_error"]
    checks.append({"case": mode, "status": result["status"], "actions": result["actions"],
                   "holds_released_perfect": result["holds_released_perfect"],
                   "eavesdropping_progress": result["eavesdropping_progress"],
                   "interruption": result.get("interruption"),
                   "all_cleanup_attempted": True, "space_released": True})

report = {"passed": len(checks), "checks": checks, "sent_real_input": False,
          "limitations": "假游戏只验证控制流、门控和资源清理；不证明游戏字段布局及原生判定正确。"}
(ROOT / "eavesdropping-runner-verification.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
