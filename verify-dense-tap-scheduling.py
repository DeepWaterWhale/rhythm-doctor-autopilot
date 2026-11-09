"""用合成密集节拍和逐帧输入采样验证连击；不连接游戏、不发送真实按键。"""
import contextlib
import importlib.util
import io
import json
import math
from pathlib import Path
import time as real_time

ROOT = Path(__file__).resolve().parent
RUN = str(real_time.time_ns())


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay(api, groups, fps, phase, label, focus_loss=None):
    groups = [[dict(record) for record in group] for group in groups]
    flattened = [r for g in groups for r in g]
    end = max(max(r["target"], r.get("release", 0)) for r in flattened) + .5

    class Clock:
        now = 0.0
        def perf_counter(self): return self.now
        def sleep(self, seconds): self.now += max(seconds, .0001)
        def sample(self):
            frame = math.floor((self.now + phase) * fps)
            return frame / fps - phase, frame
    clock = Clock()

    class FakeReader:
        def close(self): pass

    class FakeGame:
        instance, level = 1, "MontageReplay"
        def __init__(self, *args):
            self.reader = FakeReader()
            self.started = False
            self.pending = list(groups)
            self.transitions = []
            self.received = []
            self.active = []
            self.history = {}
            self.release_error = 0
        def step(self):
            _, frame = clock.sample()
            while self.transitions and self.transitions[0][0] <= frame:
                capture_frame, down = self.transitions.pop(0)
                dsp = capture_frame / fps - phase
                if down and self.pending:
                    group = self.pending.pop(0)
                    for record in group:
                        self.received.append({"row": record["row"], "error_ms": (dsp-record["target"])*1000})
                        self.history[record["row"]] = self.history.get(record["row"], 0) + 1
                        if record["hold"]:
                            self.active.append({**record, "hold_assist": False})
                elif not down and self.active:
                    for record in self.active:
                        self.release_error = max(self.release_error, abs(dsp-record["release"]))
                        self.history[record["row"]] += 1
                    self.active.clear()
        def clock(self): self.step(); return clock.sample()
        def state(self):
            self.step()
            return {"state": 3 if self.started else 0, "paused": False,
                    "failed": False, "ignore_input": False, "beats": 1}
        def scene_changed(self): return False
        def active_holds(self, state): self.step(); return self.active.copy()
        def release_margin(self, state): return .08
        def hold_release_result(self, state): return 0 if self.release_error < .08 else 1
        def row_history(self, state, row): return {"count": self.history.get(row, 0), "last": 0}
        def rank_stage(self, state): return 2 if not self.pending and not self.active else 0
        def metrics(self, state):
            self.step()
            return {"mistakes": sum(abs(x["error_ms"]) >= 20 for x in self.received),
                    "successful_hits": len(self.received), "rank_raw": 0}
        def beats(self, pointer):
            self.step()
            records = [r for g in self.pending for r in g]
            return records, len(records)

    game = FakeGame()
    transitions = []
    class FakeKeyboard:
        held = False
        def __init__(self, *args): pass
        def activate(self): raise AssertionError("模拟不得激活窗口")
        def is_foreground(self): return focus_loss is None or clock.now < focus_loss
        def tap_start(self): game.started = True
        def set_space(self, down):
            if self.held != down:
                frame = clock.sample()[1]
                self.held = down
                transitions.append((frame, down, clock.now))
                game.transitions.append((frame+1, down))
        def close(self): self.set_space(False)

    api.Game = lambda *args: game
    api.keyboard_mod.Keyboard = FakeKeyboard
    api.keyboard_mod.U.GetAsyncKeyState = lambda key: 0
    api.time = clock
    with contextlib.redirect_stdout(io.StringIO()):
        result = api.main(["--start", "--play", "--no-activate", "--max-seconds", str(end+1),
                           "--run-name", "dense-tap-replay-"+RUN+"-"+label])
    return {"label": label, "fps": fps, "phase": phase, "result": result,
            "received": game.received, "transitions": transitions,
            "max_abs_error_ms": max((abs(x["error_ms"]) for x in game.received), default=0),
            "release_error_ms": game.release_error*1000}


def tap(pointer, target, release=0):
    return {"pointer": pointer, "row": 0, "target": target, "raw_target": target,
            "release": release, "hold": bool(release), "calibration": 0}


if __name__ == "__main__":
    old = load("old", "rhythm-doctor-live-autoplay-before-dense-taps-20261108.py")
    new = load("new", "rhythm-doctor-live-autoplay.py")
    cases = [("dense-40ms", (0, .04, .08, .12)),
             ("dense-50ms", (0, .05, .10, .15)),
             ("dense-mixed", (0, .04, .10, .14))]
    runs = []
    for label, offsets in cases:
        groups = [[{**tap(f"test-{index}-{row}", .5 + offset), "row": row}
                   for row in range(1 + index % 2)] for index, offset in enumerate(offsets)]
        baseline = replay(old, groups, 60, 0, "old-"+label)
        assert baseline["result"]["metrics"]["mistakes"] > 0, baseline
        runs.append(baseline)
        for fps in (60, 90, 120):
            for phase_index in range(8):
                run = replay(new, groups, fps, phase_index/(fps*8), f"{label}-{fps}-{phase_index}")
                assert run["result"]["status"] == "rank_shown", run
                assert len(run["received"]) == sum(map(len, groups)), run
                assert run["max_abs_error_ms"] < 20, run
                # 每次按下/松开都必须由不同游戏帧采样。
                assert all(a[0] < b[0] for a, b in zip(run["transitions"], run["transitions"][1:])), run
                runs.append(run)
    for fps in (30, 60, 120):
        hold = replay(new, [[tap("hold", .5, 1)], [tap("after", 1.3)]], fps, 0, f"hold-{fps}")
        assert hold["result"]["holds_accepted"] == 1, hold
        assert hold["result"]["holds_released_perfect"] == 1, hold
        assert .98 <= hold["transitions"][1][2] <= 1.04, hold
        runs.append(hold)
    lost = replay(new, [[tap("hold", .5, 1)]], 60, 0, "focus-loss", .75)
    assert lost["result"]["status"] == "error", lost
    assert lost["transitions"][-1][1] is False, lost
    runs.append(lost)
    report = {"scenarios_passed": len(runs), "limitation": "模拟逐帧采样；不是实机重跑结果。",
              "runs": runs}
    (ROOT/"dense-tap-scheduling-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scenarios_passed": len(runs), "new_dense_max_abs_error_ms": max(r["max_abs_error_ms"] for r in runs if r["label"].startswith("dense-"))}, ensure_ascii=False))
