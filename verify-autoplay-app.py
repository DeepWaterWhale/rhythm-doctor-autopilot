"""在虚拟时钟与模拟键盘上验证常驻程序，不向游戏发送输入。"""
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("app", ROOT / "rhythm-doctor-autoplay-app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
passed = []


def check(name, condition):
    assert condition, name
    passed.append(name)


gate = app.runtime.ReadyGate(3)
for second in (0, 1, 2):
    check("三秒前不开始-" + str(second), not gate.observe("a", True, second)[0])
check("第三秒开始", gate.observe("a", True, 3)[0])
check("同一准备状态只启动一次", not gate.observe("a", True, 10)[0])
gate.observe("a", False, 11)
check("已启动状态切换焦点不会再次开始", not gate.observe("a", True, 20)[0])
check("换关重新倒计时", not gate.observe("b", True, 21)[0])
check("新关三秒后开始", gate.observe("b", True, 24)[0])
gate.observe(None, False, 25)
check("离开后重新进入同一地址也能开始倒计时", gate.observe("b", True, 26)[1] == 3)
gate.observe("b", False, 27)
check("失去就绪条件会重置倒计时", gate.observe("b", True, 29)[1] == 3)
check("重置后满三秒才启动", gate.observe("b", True, 32)[0])
check("每秒检测一次", app.POLL_SECONDS == 1)


def observed(scene="a", phase="ready"):
    return {"scene": scene, "phase": phase}


gate = app.runtime.SessionGate(3)
for second in (0, 1, 2):
    check("会话三秒前不开始-" + str(second), gate.observe(observed(), True, False, second)[0] is None)
check("会话第三秒启动", gate.observe(observed(), True, False, 3)[0] == "start")
gate.finished("a", "cancelled")
for phase_name in ("running", "ready"):
    check("取消后同一局不再接管-" + phase_name, gate.observe(observed(phase=phase_name), True, False, 8)[0] is None)
gate.gap()
check("读失败恢复后同一局不再接管", gate.observe(observed(phase="running"), True, False, 10)[0] is None)
gate.suspend()
check("暂停监听恢复后同一局不再接管", gate.observe(observed(phase="running"), True, False, 11)[0] is None)

gate = app.runtime.SessionGate(3)
gate.observe(observed(), True, False, 0)
check("手动按开始的中间状态不发键", gate.observe(observed(phase="waiting"), True, True, 1)[0] is None)
check("倒计时中手动开始转为接管", gate.observe(observed(phase="running"), True, False, 2)[0] == "attach")
check("手动开始只接管一次", gate.observe(observed(phase="running"), True, False, 3)[0] is None)
gate.finished("a", "rank_shown")
check("已结算不会重新接管", gate.observe(observed(phase="finished"), True, False, 4)[0] is None)
gate.gap()
gate.observe(observed("b", "waiting"), True, False, 5)
check("前关结算后直接接管运行中的新关", gate.observe(observed("b", "running"), True, False, 6)[0] == "attach")
gate.finished("b", "scene_changed")
check("无结算的直接场景衔接可接管", gate.observe(observed("c", "running"), True, False, 7)[0] == "attach")
gate.finished("c", "error")
check("错误停止不会继续接管后续场景", gate.observe(observed("d", "running"), True, False, 8)[0] is None)

gate = app.runtime.SessionGate(3)
check("新启动程序不会接管任意运行关卡", gate.observe(observed(phase="running"), True, False, 0)[0] is None)
gate.observe(observed(), True, False, 1)
check("未参与的准备关卡不授权另一运行关卡", gate.observe(observed("b", "running"), True, False, 2)[0] is None)
gate.observe(observed(), True, False, 3)
gate.suspend()
check("暂停监听会撤销准备阶段的接管", gate.observe(observed(phase="running"), True, False, 4)[0] is None)
gate.observe(observed(), True, False, 5)
gate.observe(observed(), False, False, 6)
check("失焦会撤销准备阶段的接管", gate.observe(observed(phase="running"), True, False, 7)[0] is None)
check("恢复前台重新等待三秒", gate.observe(observed(), True, False, 8)[1] == 3)
gate.observe(observed(), True, True, 9)
check("按住物理空格不自动开始", gate.observe(observed(), True, True, 12)[0] is None)
check("松开后重新等待三秒", gate.observe(observed(), True, False, 13)[1] == 3)

for blocked_phase in ("blocked", "finished"):
    gate = app.runtime.SessionGate(3)
    gate.observe(observed(), True, False, 0)
    check("不接管受阻状态-" + blocked_phase, gate.observe(observed(phase=blocked_phase), True, False, 1)[0] is None)
    check("受阻后不继续使用旧准备状态-" + blocked_phase, gate.observe(observed(phase="running"), True, False, 2)[0] is None)


class PreparationReader:
    def __init__(self):
        self.b = {102: 1, 103: 0, 204: 0, 305: 0, 402: 0}
        self.level_type = 1
    def read(self, address, count):
        return bytes([self.b[address]])
    def u64(self, address):
        return 300
    def i32(self, address):
        return self.level_type


class PreparationGame:
    instance, level = 100, "Level"
    gf = {"levelFinishedLoading": 2, "persistenceStartImmediately": 3, "handControllerOnTop": 4}
    layout = {"GC": 999}
    def __init__(self):
        self.reader = PreparationReader()
        self.rank = 0
        self.s = {"state": 0, "paused": False, "failed": False, "ignore_input": False, "level_object": 200}
    def state(self): return self.s
    def object_fields(self, obj): return {"levelType": 1, "startImmediately": 4, "slidingIn": 5}
    def rank_stage(self, state): return self.rank

original_metadata = app.api.objmod.metadata
original_static = app.api.sample.static_base
app.api.objmod.metadata = lambda *args: {"fields": [{"name": "twoPlayerMode", "offset": 2}]}
app.api.sample.static_base = lambda *args: 400
try:
    game = PreparationGame()
    check("正式关准备状态可启动", app.runtime.preparation(app.api, game)[1])
    for level_type in (0, 7, 8):
        game.reader.level_type = level_type
        check("跳过教学或剧情-" + str(level_type), not app.runtime.preparation(app.api, game)[1])
    game.reader.level_type = 4
    check("有评分的休息关支持准备状态", app.runtime.preparation(app.api, game)[1])
    game.reader.level_type = 1
    for field in ("paused", "failed", "ignore_input"):
        game.s[field] = True
        check("门控-" + field, not app.runtime.preparation(app.api, game)[1])
        game.s[field] = False
    for address, blocked, label in ((102, 0, "未加载"), (103, 1, "自动开始过渡"),
                                     (204, 1, "无开始界面"), (305, 1, "开始动画"), (402, 1, "双人模式")):
        old = game.reader.b[address]
        game.reader.b[address] = blocked
        check("门控-" + label, not app.runtime.preparation(app.api, game)[1])
        game.reader.b[address] = old
    for running_state in (2, 3):
        game.s["state"] = running_state
        check("运行状态可接管-" + str(running_state), app.runtime.observe_scene(app.api, game)["phase"] == "running")
    game.reader.b[103] = game.reader.b[204] = game.reader.b[305] = 1
    check("自动开始标记不阻止运行中接管", app.runtime.observe_scene(app.api, game)["phase"] == "running")
    game.reader.b[103] = game.reader.b[204] = game.reader.b[305] = 0
    for level_type in (0, 7, 8):
        game.reader.level_type = level_type
        check("不接管教学或剧情-" + str(level_type), app.runtime.observe_scene(app.api, game)["phase"] != "running")
    game.reader.level_type = 4
    check("有评分的休息关支持运行状态", app.runtime.observe_scene(app.api, game)["phase"] == "running")
    game.reader.level_type = 1
    for field in ("paused", "failed", "ignore_input"):
        game.s[field] = True
        check("运行中不可接管-" + field, app.runtime.observe_scene(app.api, game)["phase"] != "running")
        game.s[field] = False
    game.reader.b[102] = 0
    check("不接管未加载关卡", app.runtime.observe_scene(app.api, game)["phase"] != "running")
    game.reader.b[102] = 1
    game.reader.b[402] = 1
    check("不接管双人模式", app.runtime.observe_scene(app.api, game)["phase"] != "running")
    game.reader.b[402] = 0
    game.rank = 2
    check("gameState为3时仍识别结算", app.runtime.observe_scene(app.api, game)["phase"] == "finished")
    game.rank = 0
    for state_number in (1, 4, 5, 6):
        game.s["state"] = state_number
        check("不接管开始过渡剧情或评分-" + str(state_number), app.runtime.observe_scene(app.api, game)["phase"] != "running")
finally:
    app.api.objmod.metadata = original_metadata
    app.api.sample.static_base = original_static

events = []
phase = 0
class FakeKeyboard:
    held = False
    def __init__(self, pid): pass
    def is_foreground(self): return True
    def activate(self): raise AssertionError("不应抢占前台")
    def set_space(self, down):
        if self.held != down:
            self.held = down
            events.append("down" if down else "up")
    def tap_start(self):
        global phase
        events.append("start")
        phase = 3
    def close(self): self.set_space(False)

class FakeReader:
    def close(self): events.append("reader_closed")

class FakeGame:
    instance, level = 1, "Simulation"
    def __init__(self, *args): self.reader = FakeReader()
    def state(self): return {"state": phase, "paused": False, "failed": False, "ignore_input": False, "beats": 1}
    def active_holds(self, state): return []
    def release_margin(self, state): return .08
    def metrics(self, state): return {"mistakes": 0, "successful_hits": 0, "rank_raw": 0}
    def scene_changed(self): return False
    def rank_stage(self, state): return 0
    def clock(self): return 0, 1
    def beats(self, pointer): return [{"pointer": "1", "raw_target": 0, "target": 0, "hold": False}], 1

app.api.Game = FakeGame
app.api.keyboard_mod.Keyboard = FakeKeyboard
app.api.keyboard_mod.U.GetAsyncKeyState = lambda key: 0
run_id = str(time.time_ns())
args = ["--start", "--play", "--no-activate", "--max-seconds", "1", "--run-name", "app-verification-" + run_id]
result = app.api.main(args, stop_requested=lambda: "down" in events)
check("按住时停止会释放按键", events == ["start", "down", "up", "reader_closed"])
check("停止状态可返回监听器", result["status"] == "cancelled")
events.clear()
phase = 0
args[-1] += "-stale"
result = app.api.main(args, start_validator=lambda game: False)
check("倒计时后场景变更不会发送开始键", events == ["reader_closed"] and result["status"] == "error")


class AttachGame(FakeGame):
    def __init__(self, *args):
        super().__init__(*args)
        self.frame = 0
    def clock(self):
        self.frame += 1
        return (0 if self.frame == 1 else .05), self.frame
    def beats(self, pointer):
        return [{"pointer": "1", "raw_target": .05, "target": .05, "hold": False}], 1


app.api.Game = AttachGame
for mode, start_phase in (("--attach-running", 3), ("--start-or-attach", 3), ("--start-or-attach", 0)):
    phase = start_phase
    events.clear()
    run_args = [mode, "--play", "--no-activate", "--max-seconds", "1", "--run-name",
                "app-verification-" + run_id + "-" + mode[2:] + "-" + str(start_phase)]
    result = app.api.main(run_args, stop_requested=lambda: "down" in events, start_validator=lambda game: True)
    expected = (["start"] if start_phase == 0 else []) + ["down", "up", "reader_closed"]
    check("接管模式按键与退出释放-" + mode + str(start_phase), events == expected)
    check("实际进入方式有记录-" + mode + str(start_phase), result["entry"] == ("start" if start_phase == 0 else "attach"))

phase = 3
events.clear()
run_args = ["--attach-running", "--play", "--no-activate", "--max-seconds", "1", "--run-name",
            "app-verification-" + run_id + "-attach-stale"]
result = app.api.main(run_args, start_validator=lambda game: False)
check("接管前会重新验证关卡", events == ["reader_closed"] and result["status"] == "error")

events.clear()
run_args[-1] += "-cancel"
app.api.keyboard_mod.U.GetAsyncKeyState = lambda key: 0x8000
stop_calls = 0
def stop_while_waiting():
    global stop_calls
    stop_calls += 1
    return stop_calls >= 2
result = app.api.main(run_args, stop_requested=stop_while_waiting, start_validator=lambda game: True)
check("等用户松开期间可立即取消", events == ["reader_closed"] and result["status"] == "cancelled")

events.clear()
run_args[-1] += "-changed"
validation_calls = 0
def change_while_waiting(game):
    global validation_calls
    validation_calls += 1
    return validation_calls == 1
result = app.api.main(run_args, start_validator=change_while_waiting)
check("等用户松开期间场景变化取消接管", events == ["reader_closed"] and result["status"] == "error")
app.api.keyboard_mod.U.GetAsyncKeyState = lambda key: 0

summary = {"passed": len(passed), "checks": passed, "sent_real_input": False}
(ROOT / "autoplay-app-verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False))
