"""无真实输入的双人调度验证。"""
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("coop", ROOT / "rhythm-doctor-coop-autoplay.py")
coop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coop)
checks = []


def check(name, actual, expected):
    if actual != expected:
        raise AssertionError(f"{name}: {actual!r} != {expected!r}")
    checks.append({"name": name, "passed": True})


def beat(player, target, row=None, hold=False, release=0):
    return {"player": player, "target": target, "raw_target": target, "row": player if row is None else row,
            "pointer": f"{player}-{target}-{row}", "hold": hold, "release": release, "hold_assist": False}


class FakeKeyboard:
    def __init__(self, *_):
        self.held = [False, False]
        self.batches = []
        self.foreground = True

    def is_foreground(self):
        return self.foreground

    def ensure_neutral(self):
        pass

    def set_players(self, changes):
        if any(changes.values()) and not self.foreground:
            raise RuntimeError("foreground")
        self.batches.append(dict(changes))
        for player, down in changes.items():
            self.held[player] = down

    def close(self):
        for player in (0, 1):
            if self.held[player]:
                self.set_players({player: False})


class FakeGame:
    def __init__(self, *_):
        self.level, self.instance = "LuckyBreak", 100
        self.reader = SimpleNamespace(close=lambda: None)
        self.records, self.holds = [], []
        self.current_state = {"state": 3, "paused": False, "failed": False, "ignore_input": False, "beats": 1}
        self.dsp, self.frame = 0, 0
        self.histories = {}
        self.mistakes = [0, 0]
        self.mode = True

    def clock(self):
        return self.dsp, self.frame

    def scene_changed(self):
        return False

    def two_player(self):
        return self.mode

    def state(self):
        return self.current_state

    def rank_stage(self, _):
        return 0

    def key_schemes(self):
        return (0, 1)

    def active_holds(self, _):
        return self.holds

    def release_margin(self, _, player):
        return .08

    def beats(self, _):
        return self.records, len(self.records)

    def row_history(self, _, row):
        return self.histories.get(row, {"count": 0, "last": None})

    def metrics(self, _):
        return {"mistakes": sum(self.mistakes), "players": [{"mistakes": count} for count in self.mistakes]}


def scheduler():
    game, keyboard, events = FakeGame(), FakeKeyboard(), []
    result = {"actions": 0, "actions_by_player": [0, 0], "holds_accepted": 0,
              "holds_released_perfect": 0, "holds_release_unverified": 0}
    sched = coop.CoopScheduler(game, keyboard, lambda kind, **kw: events.append({"event": kind, **kw}), result)
    def step(dsp, frame):
        game.dsp, game.frame = dsp, frame
        sched.step(game.state(), dsp, frame, dsp, 1 / 120)
    return game, keyboard, events, result, step


game, keyboard, events, result, step = scheduler()
game.records = [beat(0, 1), beat(1, 1)]
step(.99, 1)
check("同拍双人按下一次提交", keyboard.batches, [{0: True, 1: True}])
step(1, 2)
check("双人点按下一帧独立释放", keyboard.held, [False, False])
step(1.001, 2)
check("同帧不重按且节拍不重复消费", result["actions"], 2)

game, keyboard, events, result, step = scheduler()
p1_hold, p2_first, p2_next = beat(0, 1, hold=True, release=2), beat(1, 1), beat(1, 1.05)
game.records = [p1_hold, p2_first, p2_next]
step(.99, 1)
game.records = [p2_next]
game.holds = [p1_hold]
step(1, 2)
check("P1长按不会阻止P2松开", keyboard.held, [True, False])
step(1.04, 3)
check("P1长按期间P2继续击打", keyboard.held, [True, True])
step(1.06, 4)
check("P2再次松开不影响P1长按", keyboard.held, [True, False])
step(1.995, 5)
check("仅在P1松开目标附近释放", keyboard.held, [False, False])
game.holds = []
game.histories[0] = {"count": 1, "last": 0}
game.mistakes[1] = 1
step(2.01, 6)
check("P1长按验证不受P2失误干扰", result["holds_released_perfect"], 1)

game, keyboard, events, result, step = scheduler()
first, second = beat(0, 1, hold=True, release=2), beat(1, 1, hold=True, release=3)
game.records = [first, second]
step(.99, 1)
game.records, game.holds = [], [first, second]
step(1.01, 2)
step(1.995, 3)
check("同时长按的不同结束时间独立处理", keyboard.held, [False, True])
game.holds = [second]
game.histories[0] = {"count": 1, "last": 0}
step(2.01, 4)
step(2.995, 5)
check("第二位玩家长按保持到其目标", keyboard.held, [False, False])


class Memory:
    def __init__(self):
        self.data = bytearray(4096)

    def put(self, address, fmt, value):
        struct.pack_into("<" + fmt, self.data, address, value)

    def read(self, address, size):
        return bytes(self.data[address:address + size])

    def i32(self, address):
        return struct.unpack_from("<i", self.data, address)[0]


memory = Memory()
bf = {"dead": 0, "unhittable": 1, "bomb": 2, "hasPulsed7thBeat": 3, "row": 8,
      "inputTimeSansCalibration": 16, "releaseTimeSansCalibration": 24,
      "playerDriven7thBeatHitTime": 32, "playerDrives7thBeat": 40, "isHeldClap": 41}
base = SimpleNamespace(reader=memory, beat_meta={"instance_size": 48}, bf=bf,
                       row_meta={"instance_size": 16}, rf={"multiplayer": 0, "singleplayer": 4, "dead": 8, "<id>k__BackingField": 12},
                       is_beat=lambda _: True, calibration_base=3000)
adapter = coop.CoopGame.__new__(coop.CoopGame)
adapter.base = base
adapter.api = SimpleNamespace(unpack=lambda data, offset, fmt: struct.unpack_from("<" + fmt, data, offset)[0])
adapter.gc_static, adapter.gc_fields = 3500, {"d_separateP2Calibration": 0}
adapter.calibration_fields = {"calibration_i": 0, "calibration_i_P2_internal": 4}
memory.put(1008, "Q", 2000)
memory.put(1016, "d", 10)
memory.put(1024, "d", 12)
memory.put(2000, "i", 1)
memory.put(2004, "i", 2)
memory.put(2012, "i", 7)
memory.put(3000, "f", .125)
memory.put(3004, "f", .25)
memory.put(3500, "B", 1)
record = adapter.read_beat(1000)
check("双人行按multiplayer识别P2，即使singleplayer是CPU", record["player"], 1)
check("P2独立校准应用于按下和松开", (record["target"], record["release"]), (10.25, 12.25))
memory.put(3500, "B", 0)
check("关闭独立P2校准时使用P1校准", adapter.read_beat(1000)["target"], 10.125)
memory.put(2000, "i", 2)
memory.put(2004, "i", 0)
check("双人CPU行不因singleplayer=P1而误按", adapter.read_beat(1000), None)
memory.put(2000, "i", 1)
memory.put(1032, "d", 20)
memory.put(1040, "B", 1)
check("自由节拍松开公式遵守游戏实现", adapter.read_beat(1000)["release"], 22)


prefix = "coop-verification-" + str(time.time_ns())
game, keyboard = FakeGame(), FakeKeyboard()
game.current_state["state"] = 0
calls = [0]
def cancelled_after_start():
    calls[0] += 1
    return calls[0] >= 3
result = coop.run(None, 1, {}, stop_requested=cancelled_after_start, run_name=prefix + "-cancel",
                  _game_factory=lambda *_: game, _keyboard_factory=lambda *_: keyboard)
check("开始双键持有期间取消返回cancelled", result["status"], "cancelled")
check("取消释放两位玩家按键", keyboard.held, [False, False])
check("开始流程确实同时按下双键", keyboard.batches[0], {0: True, 1: True})

class FocusLossKeyboard(FakeKeyboard):
    def set_players(self, changes):
        super().set_players(changes)
        if any(changes.values()):
            self.foreground = False
game, keyboard = FakeGame(), FocusLossKeyboard()
game.current_state["state"] = 0
result = coop.run(None, 1, {}, run_name=prefix + "-focus",
                  _game_factory=lambda *_: game, _keyboard_factory=lambda *_: keyboard)
check("失焦返回明确原因", result.get("interruption"), "focus_lost")
check("失焦后仍释放两个已经按下的键", keyboard.held, [False, False])

game, keyboard = FakeGame(), FakeKeyboard()
game.current_state["state"] = 0
def user_starts_during_setup():
    game.current_state = {**game.current_state, "state": 3}
    return (0, 1)
game.key_schemes = user_starts_during_setup
result = coop.run(None, 1, {}, run_name=prefix + "-race", max_seconds=0,
                  _game_factory=lambda *_: game, _keyboard_factory=lambda *_: keyboard)
check("定位期间用户已开始时改为接管", result.get("entry"), "attach")
check("定位期间已开始不重复发送双人开始键", keyboard.batches, [])

dual = coop.DualKeyboard.__new__(coop.DualKeyboard)
released = []
def fail_first(changes):
    player = next(iter(changes))
    released.append(player)
    if player == 0:
        raise OSError("模拟第一位玩家释放失败")
dual.set_players = fail_first
try:
    dual.close()
except RuntimeError:
    pass
check("一个释放失败仍尝试另一位", released, [0, 1])

previous = [[.01, .02], [.03, .04, .05]]
merged = [previous[0] + previous[1], previous[1]]
restored, storage = coop.independent_hit_offsets(merged, 2, (3, 3), previous)
check("结算 P2 追加到 P1 后恢复不同长度的各人判定", restored, previous)
check("结算记录合并修正来源", storage, "rank_p2_append_removed")
check("修正不修改原始列表", merged, [[.01, .02, .03, .04, .05], [.03, .04, .05]])
check("重复读取结算不会再次减少 P1", coop.independent_hit_offsets(merged, 3, (3, 3), previous)[0], previous)
check("游玩期间即便列表后缀相同也不去重", coop.independent_hit_offsets(merged, 0, (3, 3))[0], merged)
check("两位 Hard 模式结算不执行合并修正", coop.independent_hit_offsets(merged, 2, (4, 4))[0], merged)
check("只有一位 Hard 仍按原生合并语义恢复", coop.independent_hit_offsets(merged, 2, (4, 3), previous)[0], previous)
check("阶段先变更但列表尚未追加时保持各人数据", coop.independent_hit_offsets(previous, 2, (3, 3), previous),
      (previous, "rank_merge_pending"))
late_p1 = [[.01, .02, .06], previous[1]]
check("最后一次进度之后新增 P1 命中也保留", coop.independent_hit_offsets(
    [late_p1[0] + late_p1[1], late_p1[1]], 2, (3, 3), previous)[0], late_p1)
check("P2 零判定不会切掉 P1", coop.independent_hit_offsets([[.01], []], 2, (3, 3))[0], [[.01], []])
try:
    coop.independent_hit_offsets([[.01, .02], [.09]], 2, (3, 3))
except RuntimeError:
    check("结算列表后缀不符时报错，不猜测扣减", True, True)
else:
    raise AssertionError("应拒绝未知结算合并布局")


class MetricsMemory(Memory):
    def u64(self, address):
        return struct.unpack_from("<Q", self.data, address)[0]


metrics_memory = MetricsMemory()
metrics_memory.put(100, "Q", 300)
metrics_memory.put(108, "Q", 400)
metrics_memory.put(116, "i", 3)
metrics_memory.put(120, "i", 3)
metrics_memory.put(316, "Q", 500)
metrics_memory.put(416, "Q", 700)
metrics_memory.put(800, "i", 0)
metrics_memory.put(804, "i", 0)
def set_hit_lists(first, second):
    for pointer, array, values in ((300, 500, first), (400, 700, second)):
        metrics_memory.put(pointer + 24, "i", len(values))
        for index, value in enumerate(values):
            metrics_memory.put(array + 32 + index*4, "f", value)

set_hit_lists(*previous)
metrics_adapter = coop.CoopGame.__new__(coop.CoopGame)
metrics_adapter.base = SimpleNamespace(reader=metrics_memory, static=100,
    gf={"p1HitTimes": 0, "p2HitTimes": 8, "p1DefibMode": 16, "p2DefibMode": 20},
    mm={"instance_size": 8}, mf={"mistakesCountP1": 0, "mistakesCountP2": 4}, rank_offset=0,
    rank_stage=lambda state: metrics_memory.i32(804))
metrics_adapter.api = SimpleNamespace(unpack=lambda data, offset, fmt: struct.unpack_from("<" + fmt, data, offset)[0])
metrics_state = {"mistakes": 900, "rankscreen": 800}
check("真实 metrics 路径读取独立玩家计数", [x["successful_hits"] for x in metrics_adapter.metrics(metrics_state)["players"]], [2, 3])
set_hit_lists(*merged)
metrics_memory.put(800, "i", 15)
metrics_memory.put(804, "i", 2)
metrics = metrics_adapter.metrics(metrics_state)
check("真实 metrics 路径结算合计不重复 P2", metrics["successful_hits"], 5)
check("真实 metrics 路径保留正确各人计数", [x["successful_hits"] for x in metrics["players"]], [2, 3])
check("真实 metrics 路径保留原始列表长度供审计", metrics["raw_hit_list_counts"], [5, 3])
check("真实 metrics 路径不改变游戏评价或失误", (metrics["rank_raw"], metrics["mistakes"]), (15, 0.0))

output = {"checks": checks, "count": len(checks), "real_keyboard_input": False}
(ROOT / "coop-autoplay-verification.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(output, ensure_ascii=False, indent=2))
