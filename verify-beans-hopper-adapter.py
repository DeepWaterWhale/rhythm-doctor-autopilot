"""验证跳豆的专用 CPU 提示拍、校准与提前缓存，不连接游戏。"""
import importlib.util
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("api", ROOT/"rhythm-doctor-live-autoplay.py")
api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
checks = []
def check(name, condition):
    assert condition, name
    checks.append(name)

class Reader:
    def __init__(self): self.parts = {}
    def read(self, address, count):
        for start, data in self.parts.items():
            if start <= address and address+count <= start+len(data):
                return bytes(data[address-start:address-start+count])
        raise ValueError((address, count))
    def u64(self, address): return struct.unpack("<Q", self.read(address, 8))[0]
    def i32(self, address): return struct.unpack("<i", self.read(address, 4))[0]

g = api.Game.__new__(api.Game)
g.reader = Reader()
g.level = "BeansHopper"
g.bf = {"dead": 0, "unhittable": 1, "bomb": 2, "hasPulsed7thBeat": 3,
        "row": 8, "inputTimeSansCalibration": 16, "releaseTimeSansCalibration": 24,
        "playerDriven7thBeatHitTime": 32, "playerDrives7thBeat": 40, "isHeldClap": 41}
g.rf = {"singleplayer": 0, "dead": 4, "<id>k__BackingField": 8}
g.beat_meta, g.row_meta = {"instance_size": 48}, {"instance_size": 16}
g.calibration_meta = {"fields": [{"name": "calibration_v", "offset": 4}]}
g.calibration_base, g.calibration_offset = 3000, 0
g.is_beat = lambda pointer: True
beat, row = bytearray(48), bytearray(16)
struct.pack_into("<Qddd", beat, 8, 2000, 100, 0, 0)
struct.pack_into("<i", row, 0, 2); struct.pack_into("<i", row, 8, 4)
g.reader.parts = {1000: beat, 2000: row, 3000: struct.pack("<ff", .09933333, .08)}
record = g.read_beat(1000)
check("跳豆CPU row4提供跳跃目标", record is not None and record["row"] == 4)
check("跳豆使用视觉校准", abs(record["target"]-100.08) < .000001)
g.level = "Montage"
check("普通关卡仍跳过CPU", g.read_beat(1000) is None)
struct.pack_into("<i", row, 0, 0)
check("普通P1目标仍使用输入校准", abs(g.read_beat(1000)["target"]-100.09933333) < .000001)
g.level = "BeansHopper"
check("跳豆不把P1装饰行当提示", g.read_beat(1000) is None)
struct.pack_into("<i", row, 0, 2); struct.pack_into("<i", row, 8, 3)
check("跳豆不会放行其他CPU行", g.read_beat(1000) is None)
struct.pack_into("<i", row, 8, 4)
beat[2] = 1
check("专用提示同样排除炸弹", g.read_beat(1000) is None)
beat[2] = 0

header, array = bytearray(32), bytearray(40)
struct.pack_into("<Qii", header, 16, 5000, 1, 0)
struct.pack_into("<QQ", array, 24, 1, 1000)
g.reader.parts[4000] = header; g.reader.parts[5000] = array
g.beans_targets = {}
g.state = lambda: {}
score = [59]; dsp = [99]
g.beans_score = lambda state: score[0]
g.clock = lambda: (dsp[0], 1)
first, _ = g.beats(4000)
check("提前记录提示目标", len(first) == 1)
struct.pack_into("<ii", header, 24, 0, 1)
dsp[0] = 100.04
cached, _ = g.beats(4000)
check("CPU已消费提示后仍保留未来输入", cached == first)
score[0] = 60
check("60分停止跳跃等待自然结算", g.beats(4000) == ([], 0))
score[0] = 59; dsp[0] = 100.5
check("过期提示不反复输入", g.beats(4000) == ([], 0))

spec = importlib.util.spec_from_file_location("runtime", ROOT/"rhythm-doctor-runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

g.instance, g.conductor = 6000, 7000
g.layout = {"GC": 999}
g.gf = {"levelFinishedLoading": 0}
g.cf = {"barNumber": 0}
g.object_fields = lambda pointer: {"levelType": 0, "i1": 4, "f2": 8}
g.beans_score = lambda state: g.reader.i32(8004)
g.reader.parts.update({6000: bytearray([1]), 7000: bytearray(struct.pack("<i", 2)),
                       8000: bytearray(struct.pack("<iif", 1, 0, 0)), 9000: bytearray([0])})
g.s = {"state": 3, "paused": False, "failed": False, "ignore_input": False, "level_object": 8000}
g.state = lambda: dict(g.s)
g.rank = 0
g.rank_stage = lambda state: g.rank
original_metadata, original_static = api.objmod.metadata, api.sample.static_base
api.objmod.metadata = lambda *args: {"fields": [{"name": "twoPlayerMode", "offset": 0}]}
api.sample.static_base = lambda *args: 9000
try:
    ready = runtime.observe_scene(api, g)
    check("跳豆第2小节被识别为准备而非运行", ready["phase"] == "ready")
    for allow_attach in (False, True):
        session = runtime.SessionGate(3)
        for second in (0, 1, 2):
            check(f"跳豆白金{allow_attach}三秒前不开始-{second}",
                  session.observe(ready, True, False, second, allow_attach, allow_attach)[0] is None)
        check(f"跳豆白金{allow_attach}三秒后开始",
              session.observe(ready, True, False, 3, allow_attach, allow_attach)[0] == "start")
        check(f"跳豆白金{allow_attach}不重复开始",
              session.observe(ready, True, False, 4, allow_attach, allow_attach)[0] is None)
        session.finished(ready["scene"], "error", retry_ready=True)
        check(f"跳豆白金{allow_attach}开始前失焦重新倒计时",
              session.observe(ready, True, False, 5, allow_attach, allow_attach)[1] == 3)
        check(f"跳豆白金{allow_attach}恢复后仍等足三秒",
              session.observe(ready, True, False, 8, allow_attach, allow_attach)[0] == "start")
        session.finished(ready["scene"], "error")
        check(f"跳豆白金{allow_attach}重试耗尽不无限重复",
              session.observe(ready, True, False, 12, allow_attach, allow_attach)[0] is None)
    for flag in ("paused", "failed", "ignore_input"):
        g.s[flag] = True
        check("跳豆禁止启动-"+flag, runtime.observe_scene(api, g)["phase"] != "ready")
        g.s[flag] = False
    g.rank = 2
    check("跳豆结算优先于特殊开始", runtime.observe_scene(api, g)["phase"] == "finished")
    g.rank = 0
    struct.pack_into("<i", g.reader.parts[7000], 0, 3)
    check("跳豆第3小节识别为运行", runtime.observe_scene(api, g)["phase"] == "running")
    struct.pack_into("<i", g.reader.parts[7000], 0, 2)
    struct.pack_into("<f", g.reader.parts[8000], 8, 1)
    check("跳豆失败动画不误识别为开始", runtime.observe_scene(api, g)["phase"] != "ready")
finally:
    api.objmod.metadata, api.sample.static_base = original_metadata, original_static

start = api.BeansStartGate()
waiting, playing = {"ready": True, "bar": 2}, {"ready": False, "bar": 3}
check("首次发送不能直接算启动成功", start.observe(waiting, 0) == "press" and not start.confirmed)
check("开始重试间隔一秒", start.observe(waiting, .999) == "waiting" and start.attempts == 1)
check("未接收首个空格时重试", start.observe(waiting, 1) == "press" and start.attempts == 2)
check("读取第3小节后确认启动", start.observe(playing, 1.1) == "confirmed" and start.confirmed)
check("确认后不再发送开始空格", start.observe(waiting, 2) is None and start.attempts == 2)
start = api.BeansStartGate()
for second in (0, 1, 2):
    check("有限重试-"+str(second), start.observe(waiting, second) == "press")
try:
    start.observe(waiting, 3)
except RuntimeError:
    check("三次失败后明确停止", start.attempts == 3 and not start.confirmed)
else:
    raise AssertionError("重试未停止")
start = api.BeansStartGate()
check("直接接管已运行跳豆不发开始键", start.observe(playing, 0) == "confirmed" and start.attempts == 0)

summary = {"passed": len(checks), "checks": checks, "sent_real_input": False}
(ROOT/"beans-hopper-adapter-verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False))
