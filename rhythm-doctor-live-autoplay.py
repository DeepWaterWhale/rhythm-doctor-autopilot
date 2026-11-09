"""当前安装版本、当前进程的单人关卡自动输入实验；只读内存并发送普通空格。"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path
import statistics
import struct
import sys
import time

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
objmod = load_module("rd_obj", "inspect-mono-object.py")
sample = objmod.sample
keyboard_mod = load_module("rd_keys", "win32-game-keyboard.py")

def fields(meta):
    return {f["name"]: f["offset"] for f in meta["fields"]}
def unpack(data, offset, fmt):
    return struct.unpack_from("<" + fmt, data, offset)[0]

class BeansStartGate:
    """发键后以离开第 2 小节确认开始；未接收时最多重试三次。"""
    def __init__(self):
        self.attempts = 0
        self.next_attempt = 0.0
        self.confirmed = False
        self.wait_since = None

    def observe(self, sample, now):
        if self.confirmed:
            return None
        if sample["bar"] >= 3:
            self.confirmed = True
            return "confirmed"
        if self.wait_since is None:
            self.wait_since = now
        if now < self.next_attempt:
            return "waiting"
        if self.attempts >= 3 or now - self.wait_since >= 10:
            raise RuntimeError("跳豆未确认开始：空格未使关卡离开开始界面")
        if not sample["ready"]:
            return "waiting"
        self.attempts += 1
        self.next_attempt = now + 1.0
        return "press"

class Game:
    def __init__(self, pid, layout=None):
        self.reader = sample.ins.mem.Reader(pid)
        try:
            self.initialize(layout)
        except Exception:
            self.reader.close()
            raise

    def initialize(self, layout):
        self.layout = layout or {"scnGame": 0x265F6EBFD00, "scrConductor": 0x265F6EBFAC0,
                                 "Beat": 0x265F6F0FC48, "Row": 0x265F6F0F920,
                                 "RDCalibration": 0x265F6F039D0, "MistakesManager": 0x265F714ABC0,
                                 "Rankscreen": 0x265F72922A8}
        self.context = sample.resolve(self.reader, self.layout["scnGame"])
        self.instance = self.context["instance"]
        self.meta = self.context["metadata"]
        self.gf = fields(self.meta)
        self.static = self.context["game_static_base"]
        self.conductor_meta = objmod.metadata(self.reader, self.layout["scrConductor"])
        self.cf = fields(self.conductor_meta)
        self.conductor = self.reader.u64(sample.static_base(self.reader, self.conductor_meta) + self.cf["_instance"])
        if self.reader.u64(self.reader.u64(self.conductor)) != int(self.conductor_meta["address"], 16):
            raise RuntimeError("计时器对象类型验证失败")
        self.beat_meta = objmod.metadata(self.reader, self.layout["Beat"])
        self.bf = fields(self.beat_meta)
        self.row_meta = objmod.metadata(self.reader, self.layout["Row"])
        self.rf = fields(self.row_meta)
        self.calibration_meta = objmod.metadata(self.reader, self.layout["RDCalibration"])
        self.calibration_base = sample.static_base(self.reader, self.calibration_meta)
        self.calibration_offset = fields(self.calibration_meta)["calibration_i"]
        self.mm = objmod.metadata(self.reader, self.layout["MistakesManager"])
        self.mf = fields(self.mm)
        self.rank_meta = objmod.metadata(self.reader, self.layout["Rankscreen"])
        self.rank_offset = fields(self.rank_meta)["mRank"]
        self.gameover_offset = fields(self.rank_meta)["trueGameover"]
        self.known_beat_classes = {}
        self.object_fields_cache = {}
        self.beans_targets = {}
        self.level = self.reader.managed_string(self.reader.u64(self.instance + self.gf["levelIdentifier"]))

    def scene_changed(self):
        return self.reader.u64(self.context["instance_slot"]) != self.instance

    def refresh_scene(self):
        context = sample.resolve(self.reader, self.layout["scnGame"])
        offsets = fields(context["metadata"])
        instance = context["instance"]
        if not self.reader.read(instance + offsets["levelFinishedLoading"], 1)[0]:
            raise RuntimeError("新场景尚未加载完成")
        level = self.reader.managed_string(self.reader.u64(instance + offsets["levelIdentifier"]))
        if not level:
            raise RuntimeError("新场景尚未设置关卡名")
        conductor = self.reader.u64(sample.static_base(self.reader, self.conductor_meta) + self.cf["_instance"])
        if not conductor or self.reader.u64(self.reader.u64(conductor)) != int(self.conductor_meta["address"], 16):
            raise RuntimeError("新场景计时器尚未就绪")
        self.context, self.instance, self.meta = context, instance, context["metadata"]
        self.gf, self.static, self.level = offsets, context["game_static_base"], level
        self.conductor = conductor
        self.beans_targets.clear()

    def object_fields(self, pointer):
        cls = self.reader.u64(self.reader.u64(pointer))
        if cls not in self.object_fields_cache:
            current, result = cls, {}
            for _ in range(24):
                result.update(fields(objmod.metadata(self.reader, current)))
                current = self.reader.u64(current + 0x30)
                if not current:
                    break
            self.object_fields_cache[cls] = result
        return self.object_fields_cache[cls]

    def pointers(self, array, limit=64):
        if not array:
            return []
        count = self.reader.u64(array + 24)
        if count > limit:
            raise RuntimeError("对象数组长度异常")
        return struct.unpack("<" + "Q" * count, self.reader.read(array + 32, count * 8)) if count else []

    def clock(self):
        data = self.reader.read(self.conductor + self.cf["audioPosCache"], 12)
        return struct.unpack("<di", data)

    def state(self):
        if self.reader.u64(self.context["instance_slot"]) != self.instance:
            raise RuntimeError("场景已经切换，停止读取旧实例")
        data = self.reader.read(self.instance, self.meta["instance_size"])
        def value(name, fmt):
            return unpack(data, self.gf[name], fmt)
        return {"state": value("_gameState", "i"), "paused": bool(value("paused", "B")),
                "failed": bool(value("failedLevel", "B")), "ignore_input": bool(value("ignoreInput", "B")),
                "beats": value("beats", "Q"), "mistakes": value("mistakesManager", "Q"),
                "rankscreen": value("rankscreen", "Q"), "level_object": value("currentLevel", "Q"),
                "rows": value("rows", "Q"), "row_offsets": value("rowsHitOffsets", "Q"),
                "caught_holds": value("playerCaughtHoldBeat", "Q")}

    def rank_stage(self, state):
        return self.reader.i32(state["rankscreen"] + self.gameover_offset)

    def beans_start_state(self, state):
        level = state["level_object"]
        lf = self.object_fields(level)
        bar = self.reader.i32(self.conductor + self.cf["barNumber"])
        ending = unpack(self.reader.read(level + lf["f2"], 4), 0, "f")
        ready = (state["state"] == 3 and not state["paused"] and not state["failed"]
                 and not state["ignore_input"] and bar == 2
                 and self.beans_score(state) == 0 and ending == 0)
        return {"ready": ready, "bar": bar}

    def hold_release_result(self, state):
        obj = state["level_object"]
        return self.reader.i32(obj + self.object_fields(obj)["lastHeldRelease"])

    def release_margin(self, state):
        mode = self.reader.i32(self.static + self.gf["p1DefibMode"])
        margin = .4 if mode in (0, 1) else .12 if mode == 2 else .08
        obj = state["level_object"]
        multiplier = struct.unpack("<f", self.reader.read(obj + self.object_fields(obj)["hitMarginMultiplier"], 4))[0]
        return margin * multiplier if multiplier > 0 else margin

    def row_history(self, state, row):
        lists = self.pointers(state["row_offsets"])
        pointer = lists[row] if 0 <= row < len(lists) else 0
        if not pointer:
            return {"count": 0, "last": None}
        header = self.reader.read(pointer + 16, 16)
        array, count, version = struct.unpack("<Qii", header)
        if not 0 <= count <= 100000:
            raise RuntimeError("行判定数量异常")
        last = self.reader.i32(array + 32 + (count - 1) * 8 + 4) if count else None
        if self.reader.read(pointer + 16, 16) != header:
            return None
        return {"count": count, "last": last}

    def active_holds(self, state):
        holds = []
        for row in self.pointers(state["rows"]):
            if not row or self.reader.i32(row + self.rf["singleplayer"]) != 0:
                continue
            entity = self.reader.u64(row + self.rf["ent"])
            if not entity:
                continue
            box = self.reader.u64(entity + self.object_fields(entity)["playerBox"])
            if not box:
                continue
            box_fields = self.object_fields(box)
            pointer = self.reader.u64(box + box_fields["currentHoldBeat"])
            if pointer:
                beat = self.read_beat(pointer, active_hold=True)
                if not beat:
                    raise RuntimeError("当前长按对象类型不符合预期")
                beat["hold_assist"] = bool(self.reader.read(box + box_fields["holdAssist"], 1)[0])
                holds.append(beat)
        return holds

    def is_beat(self, pointer):
        cls = self.reader.u64(self.reader.u64(pointer))
        if cls in self.known_beat_classes:
            return self.known_beat_classes[cls]
        original = cls
        for _ in range(12):
            if cls == int(self.beat_meta["address"], 16):
                self.known_beat_classes[original] = True
                return True
            cls = self.reader.u64(cls + 0x30)
            if not cls:
                break
        self.known_beat_classes[original] = False
        return False

    def beats(self, list_pointer):
        beans = self.level == "BeansHopper"
        if beans:
            state = self.state()
            if self.beans_score(state) >= 60:
                # 此小游戏在自然结束时按分数保存评价；60 分已达到 S 门槛。
                # 停止跳跃并等待游戏自身结算，不能直接退出或改写评分。
                return [], 0
            dsp, _ = self.clock()
            self.beans_targets = {key: record for key, record in self.beans_targets.items()
                                  if record["target"] >= dsp - .15}
        header = self.reader.read(list_pointer, 32)
        array = unpack(header, 16, "Q")
        count, version = struct.unpack_from("<ii", header, 24)
        if not 0 <= count <= 1024:
            raise RuntimeError("节奏事件数量异常")
        if not count:
            cached = sorted(self.beans_targets.values(), key=lambda record: record["target"]) if beans else []
            return cached, len(cached)
        capacity = self.reader.u64(array + 24)
        if count > capacity or capacity > 65536:
            return [], count
        pointers = struct.unpack("<" + "Q" * count, self.reader.read(array + 32, count * 8))
        records = []
        for pointer in pointers:
            record = self.read_beat(pointer)
            if record:
                records.append(record)
        # 读取期间 List 被修改时，丢弃该快照，下一轮重新读取。
        final_header = self.reader.read(list_pointer + 16, 16)
        if struct.unpack("<Qii", final_header) != (array, count, version):
            return [], count
        if beans:
            # CPU 会自行消费提示拍；保留提前读取的视觉目标，直至发键窗口结束。
            for record in records:
                self.beans_targets[(record["pointer"], record["raw_target"])] = record
            records = list(self.beans_targets.values())
        return sorted(records, key=lambda x: x["target"]), count

    def beans_score(self, state):
        obj = state["level_object"]
        return self.reader.i32(obj + self.object_fields(obj)["i1"])

    def read_beat(self, pointer, active_hold=False):
        if not pointer or not self.is_beat(pointer):
            return None
        data = self.reader.read(pointer, self.beat_meta["instance_size"])
        def bv(name, fmt):
            return unpack(data, self.bf[name], fmt)
        if not active_hold and any(bv(name, "B") for name in ("dead", "unhittable", "bomb", "hasPulsed7thBeat")):
            return None
        row = bv("row", "Q")
        if not row:
            return None
        row_data = self.reader.read(row, self.row_meta["instance_size"])
        row_id = unpack(row_data, self.rf["<id>k__BackingField"], "i")
        player = unpack(row_data, self.rf["singleplayer"], "i")
        beans = self.level == "BeansHopper"
        if (beans and (row_id != 4 or player != 2)) or (not beans and player != 0):
            return None
        if not active_hold and unpack(row_data, self.rf["dead"], "B"):
            return None
        calibration_offset = fields(self.calibration_meta)["calibration_v"] if beans else self.calibration_offset
        calibration = struct.unpack("<f", self.reader.read(self.calibration_base + calibration_offset, 4))[0]
        target_raw, release_raw = bv("inputTimeSansCalibration", "d"), bv("releaseTimeSansCalibration", "d")
        driven_time = bv("playerDriven7thBeatHitTime", "d")
        driven = bv("playerDrives7thBeat", "B") and driven_time != 0
        target = (driven_time if driven else target_raw) + calibration
        release = driven_time + release_raw - target_raw if driven else release_raw + calibration
        if not math.isfinite(target) or not math.isfinite(release):
            return None
        return {"pointer": hex(pointer), "row": row_id,
                "target": target, "raw_target": target_raw, "release": release,
                "hold": bool(bv("isHeldClap", "B")), "calibration": calibration}

    def metrics(self, state):
        data = self.reader.read(state["mistakes"], self.mm["instance_size"])
        mistakes = unpack(data, self.mf["mistakesCountP1"], "f") + unpack(data, self.mf["mistakesCountP2"], "f")
        hit_list = self.reader.u64(self.static + self.gf["p1HitTimes"])
        header = self.reader.read(hit_list, 32)
        hit_count = unpack(header, 24, "i")
        if not 0 <= hit_count <= 100000:
            raise RuntimeError("命中数量异常")
        offsets = []
        if hit_count:
            array = unpack(header, 16, "Q")
            offsets = list(struct.unpack("<" + "f" * hit_count, self.reader.read(array + 32, hit_count * 4)))
        rank = self.reader.i32(state["rankscreen"] + self.rank_offset)
        result = {"mistakes": mistakes, "successful_hits": hit_count, "rank_raw": rank,
                "mean_abs_error_ms": statistics.fmean(offsets) * 1000 if offsets else None,
                "max_abs_error_ms": max(offsets) * 1000 if offsets else None,
                "under_40ms": sum(x < .04 for x in offsets)}
        if self.level == "BeansHopper":
            result["special_score"] = self.beans_score(state)
        return result


def main(argv=None, stop_requested=None, event_sink=None, start_validator=None):
    stop_requested = stop_requested or (lambda: False)
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=27896)
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--start", action="store_true")
    entry.add_argument("--start-or-attach", action="store_true")
    parser.add_argument("--play", action="store_true")
    entry.add_argument("--attach-running", action="store_true")
    parser.add_argument("--follow-scenes", action="store_true")
    parser.add_argument("--no-activate", action="store_true")
    parser.add_argument("--layout-json")
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--lead-ms", type=float, default=10)
    parser.add_argument("--run-name", default="autoplay-trial-01")
    args = parser.parse_args(argv)
    layout = json.loads(Path(args.layout_json).read_text(encoding="utf-8"))["classes"] if args.layout_json else None
    game = Game(args.pid, layout)
    keyboard = None
    begin = time.perf_counter()
    events_path = ROOT / (args.run_name + ".jsonl")
    result = {"pid": args.pid, "level": game.level, "instance": hex(game.instance), "lead_ms": args.lead_ms,
              "started_utc": datetime.now(timezone.utc).isoformat(), "actions": 0, "status": "running",
              "holds_accepted": 0, "holds_released_perfect": 0, "holds_release_unverified": 0,
              "completed_segments": []}
    last_state = None
    last_metrics = {}
    log = events_path.open("x", encoding="utf-8")
    def event(kind, **values):
        item = {"event": kind, "elapsed": time.perf_counter() - begin, **values}
        log.write(json.dumps(item, ensure_ascii=False) + "\n")
        log.flush()
        if event_sink:
            event_sink(item)
        if sys.stdout is not None and kind in ("started", "progress", "state", "scene_changed", "scene_attached", "hold_accepted", "hold_release_checked", "finished", "error"):
            print(json.dumps(item, ensure_ascii=False), flush=True)
    try:
        state = game.state()
        if game.level == "BeansHopper":
            result["special_start_pending"] = game.beans_start_state(state)["bar"] < 3
            result["special_start_confirmed"] = False
        allowed_states = (0, 1, 2, 3) if args.start_or_attach else (2, 3) if args.attach_running else (0,)
        if state["state"] not in allowed_states or state["paused"]:
            raise RuntimeError("游戏不在本轮要求的未暂停状态")
        # 预先解析行/实体字段，避免首个节拍前才做元数据遍历。
        game.active_holds(state)
        game.release_margin(state)
        last_metrics = game.metrics(state)
        result["initial_metrics"] = last_metrics
        if args.start or args.start_or_attach or args.play:
            keyboard = keyboard_mod.Keyboard(args.pid)
            if args.no_activate:
                if not keyboard.is_foreground():
                    result["interruption"] = "focus_lost"
                    raise RuntimeError("游戏不在前台，未接管输入")
            else:
                keyboard.activate()
        if args.start_or_attach:
            # 用户可能在最后一次监听与执行之间按下开始，避免再发一次空格。
            entry_deadline = time.perf_counter() + 2
            while game.state()["state"] == 1:
                if stop_requested():
                    result["status"] = "cancelled"
                    return result
                if not keyboard.is_foreground() or time.perf_counter() >= entry_deadline:
                    raise RuntimeError("开始流程尚未结束或游戏失去前台，未接管输入")
                time.sleep(.02)
            entry_state = game.state()["state"]
            if entry_state not in (0, 2, 3):
                raise RuntimeError("执行前关卡状态已变化，取消输入")
            args.start, args.attach_running = entry_state == 0, entry_state in (2, 3)
        if args.start or args.attach_running:
            if stop_requested():
                result["status"] = "cancelled"
                return result
            if start_validator is not None and not start_validator(game):
                raise RuntimeError("执行前关卡状态已变化，取消开始或接管")
        if args.attach_running and keyboard:
            neutral_deadline = time.perf_counter() + 15
            while game.active_holds(game.state()) or keyboard_mod.U.GetAsyncKeyState(0x20) & 0x8000:
                if stop_requested():
                    result["status"] = "cancelled"
                    return result
                if not keyboard.is_foreground():
                    result["interruption"] = "focus_lost"
                    raise RuntimeError("等待松开时游戏失去前台，未接管输入")
                if start_validator is not None and not start_validator(game):
                    raise RuntimeError("等待松开时关卡状态已变化，未接管输入")
                if time.perf_counter() >= neutral_deadline:
                    raise RuntimeError("等待用户松开空格超时，尚未接管输入")
                time.sleep(.02)
            state = game.state()
            if (state["state"] not in (2, 3) or state["paused"] or state["failed"]
                    or game.rank_stage(state) >= 2 or not keyboard.is_foreground()
                    or (start_validator is not None and not start_validator(game))):
                raise RuntimeError("接管前关卡状态已变化，取消输入")
        if args.start:
            if keyboard_mod.U.GetAsyncKeyState(0x20) & 0x8000:
                raise RuntimeError("用户仍按住空格，取消开始")
            keyboard.tap_start()
            start_deadline = time.perf_counter() + 2
            while game.state()["state"] == 0:
                if stop_requested():
                    result["status"] = "cancelled"
                    return result
                if time.perf_counter() >= start_deadline:
                    raise RuntimeError("开始按键未被游戏接收")
                time.sleep(.025)
        result["entry"] = "attach" if args.attach_running else "start" if args.start else "observe"
        event("started", level=game.level, playing=args.play, entry=result["entry"], initial_metrics=last_metrics)
        consumed = set()
        if args.attach_running:
            attach_dsp, _ = game.clock()
            records, _ = game.beats(game.state()["beats"])
            for record in records:
                if record["target"] <= attach_dsp + .02:
                    consumed.add((record["pointer"], record["raw_target"]))
                    event("attach_skipped_past_target", dsp=attach_dsp, record=record)
        tap_down_frame = None
        awaiting_hold = None
        release_wait = None
        accepted_holds = {}
        hold_release_target = None
        last_up_frame = None
        last_frame = -1
        beans_start = BeansStartGate()
        last_frame_wall = None
        frame_period = 1 / 60
        next_progress = begin
        while time.perf_counter() - begin < args.max_seconds:
            if stop_requested():
                result["status"] = "cancelled"
                break
            wall = time.perf_counter()
            if keyboard and not keyboard.is_foreground():
                result["interruption"] = "focus_lost"
                raise RuntimeError("游戏失去前台焦点，停止本轮输入")
            if game.scene_changed():
                if keyboard:
                    keyboard.set_space(False)
                result["completed_segments"].append({"level": game.level, "instance": hex(game.instance), "last_metrics": last_metrics})
                event("scene_changed", previous_level=game.level)
                if not args.follow_scenes:
                    result["status"] = "scene_changed"
                    break
                deadline = time.perf_counter() + 15
                while True:
                    try:
                        game.refresh_scene()
                        break
                    except (RuntimeError, ValueError, OSError):
                        if time.perf_counter() >= deadline:
                            raise RuntimeError("场景切换后未找到已加载的游玩关卡")
                        time.sleep(.025)
                consumed.clear()
                accepted_holds.clear()
                tap_down_frame = awaiting_hold = release_wait = hold_release_target = None
                last_frame, last_state = -1, None
                beans_start = BeansStartGate()
                last_frame_wall = None
                frame_period = 1 / 60
                last_up_frame = None
                next_progress = time.perf_counter()
                state = game.state()
                last_metrics = game.metrics(state)
                result["current_level"] = game.level
                event("scene_attached", level=game.level, instance=hex(game.instance), state=state["state"], metrics=last_metrics)
                if args.play and state["state"] == 0:
                    # 等待开始画面进入稳定状态，避免输入落在进场动画中。
                    time.sleep(1)
                    if game.state()["state"] == 0:
                        keyboard.tap_start()
                continue
            dsp, frame = game.clock()
            if frame == last_frame and wall < next_progress:
                time.sleep(0.001)
                continue
            if last_frame_wall is not None and frame > last_frame:
                observed_period = (wall - last_frame_wall) / (frame - last_frame)
                if 1 / 500 <= observed_period <= .1:
                    frame_period = .8 * frame_period + .2 * observed_period
            if frame != last_frame:
                last_frame_wall = wall
            last_frame = frame
            state = game.state()
            key = (state["state"], state["paused"], state["failed"])
            if key != last_state:
                event("state", dsp=dsp, state=state["state"], name=sample.STATE_NAMES.get(state["state"]), paused=state["paused"], failed=state["failed"])
                last_state = key
            if state["paused"]:
                raise RuntimeError("游戏已暂停，停止本轮输入")
            rank_stage = game.rank_stage(state)
            if wall >= next_progress or state["state"] == 6 or rank_stage >= 2 or state["failed"]:
                last_metrics = game.metrics(state)
                event("progress", dsp=dsp, rank_stage=rank_stage, holds_accepted=result["holds_accepted"], **last_metrics)
                next_progress = wall + 10
            if state["failed"]:
                result["status"] = "level_failed"
                break
            if state["state"] == 6 or rank_stage >= 2:
                result["status"] = "rank_shown"
                if keyboard:
                    keyboard.set_space(False)
                time.sleep(0.2)
                last_metrics = game.metrics(game.state())
                break
            if game.level == "BeansHopper" and not beans_start.confirmed and args.play:
                start_sample = game.beans_start_state(state)
                start_action = beans_start.observe(start_sample, wall)
                if start_action == "confirmed":
                    result["special_start_confirmed"] = True
                    result["special_start_pending"] = False
                    event("minigame_started", level=game.level, bar=start_sample["bar"], attempts=beans_start.attempts)
                else:
                    if start_action == "press":
                        if keyboard_mod.U.GetAsyncKeyState(0x20) & 0x8000:
                            raise RuntimeError("用户仍按住空格，取消跳豆开始")
                        keyboard.tap_start()
                        last_up_frame = game.clock()[1]
                        event("minigame_start_attempt", level=game.level, attempt=beans_start.attempts)
                    time.sleep(.001)
                    continue
            holds = game.active_holds(state)
            current_pointers = {record["pointer"] for record in holds}
            for record in holds:
                if record["pointer"] not in accepted_holds:
                    result["holds_accepted"] += 1
                    event("hold_accepted", dsp=dsp, record=record)
                accepted_holds[record["pointer"]] = record
            if awaiting_hold:
                awaiting_hold["unseen"].difference_update(current_pointers)
                if not awaiting_hold["unseen"]:
                    awaiting_hold = None
                    tap_down_frame = None
                elif wall - awaiting_hold["wall"] > .25:
                    raise RuntimeError("长按按下后未确认所有目标被接住：" + str(sorted(awaiting_hold["unseen"])))
            if release_wait:
                released = release_wait["pointers"] - current_pointers
                if released == release_wait["pointers"] and frame != release_wait["frame"]:
                    histories = {row: game.row_history(state, row) for row in release_wait["histories"]}
                    last_release = game.hold_release_result(state)
                    after = game.metrics(state)
                    perfect = last_release == 0 and after["mistakes"] == release_wait["mistakes"]
                    perfect = perfect and all(
                        old is not None and histories[row] is not None
                        and histories[row]["count"] > old["count"] and histories[row]["last"] == 0
                        for row, old in release_wait["histories"].items())
                    count_released = len(release_wait["pointers"])
                    result["holds_released_perfect" if perfect else "holds_release_unverified"] += count_released
                    event("hold_release_checked", dsp=dsp, count=count_released, perfect=perfect,
                          last_held_release=last_release, histories=histories,
                          mistakes_before=release_wait["mistakes"], mistakes_after=after["mistakes"])
                    for pointer in released:
                        accepted_holds.pop(pointer, None)
                    release_wait = None
                    hold_release_target = None
                elif wall - release_wait["wall"] > .35 and not any(record["hold_assist"] for record in holds):
                    raise RuntimeError("松开后游戏仍保留长按状态")
            if keyboard and keyboard.held and not awaiting_hold:
                if holds:
                    manual = [record for record in holds if not record["hold_assist"]]
                    if manual:
                        margin = game.release_margin(state)
                        low = max(record["release"] - margin for record in manual)
                        high = min(record["release"] + margin for record in manual)
                        if high - low < .020:
                            raise RuntimeError("同一玩家多个长按没有足够的共同松开窗口")
                        updated_target = (low + high) / 2
                    else:
                        updated_target = dsp
                    if hold_release_target is None or abs(updated_target - hold_release_target) > .0001:
                        event("hold_target", dsp=dsp, release_target=updated_target, records=holds)
                    hold_release_target = updated_target
                    if dsp >= hold_release_target - args.lead_ms / 1000:
                        release_wait = {"pointers": current_pointers, "wall": wall, "frame": frame,
                                        "histories": {record["row"]: game.row_history(state, record["row"]) for record in holds},
                                        "mistakes": game.metrics(state)["mistakes"]}
                        keyboard.set_space(False)
                        released_dsp, last_up_frame = game.clock()
                        event("up", dsp=released_dsp, frame=last_up_frame, hold_release_target=hold_release_target, records=holds)
                        tap_down_frame = None
                elif accepted_holds:
                    keyboard.set_space(False)
                    released_dsp, last_up_frame = game.clock()
                    event("hold_ended_without_manual_release", dsp=released_dsp, frame=last_up_frame,
                          records=list(accepted_holds.values()))
                    result["holds_release_unverified"] += len(accepted_holds)
                    accepted_holds.clear()
                    tap_down_frame = hold_release_target = None
                elif tap_down_frame is not None and frame != tap_down_frame:
                    # 输入状态在下一帧被游戏采样后即可释放。固定 45 ms
                    # 持有再等 20 ms 会拖慢相隔 31.25 / 46.875 ms 的连击。
                    keyboard.set_space(False)
                    released_dsp, last_up_frame = game.clock()
                    event("up", dsp=released_dsp, frame=last_up_frame, hold_release_target=None)
                    tap_down_frame = None
            if awaiting_hold or release_wait or holds:
                time.sleep(0.001)
                continue
            records, count = game.beats(state["beats"])
            if state["ignore_input"] or state["state"] not in (2, 3) and not count:
                time.sleep(0.001)
                continue
            pending = [record for record in records if (record["pointer"], record["raw_target"]) not in consumed]
            # 松开也必须跨一帧；同一帧内释放并再次按下可能合并成一次输入。
            if pending and not (keyboard and keyboard.held) and frame != last_up_frame:
                first = pending[0]
                # 在目标前一帧左右提交输入，让下一次游戏输入采样落在目标附近。
                # 只用固定 10 ms 提前量，在 60 FPS 下仍可能多等一帧而偏晚。
                input_lead = max(args.lead_ms / 1000, frame_period * 1.5)
                if dsp >= first["target"] - input_lead:
                    group = [record for record in pending if abs(record["target"] - first["target"]) < .009]
                    if dsp - first["target"] > .150:
                        event("expired", dsp=dsp, records=group)
                    else:
                        # 读取事件列表可能跨帧，按发送前的时钟记录，且先发键再写日志。
                        sent_dsp, sent_frame = game.clock()
                        if args.play:
                            keyboard.set_space(True)
                            result["actions"] += 1
                            holds = [record for record in group if record["hold"]]
                            if holds:
                                awaiting_hold = {"unseen": {record["pointer"] for record in holds}, "wall": wall}
                                tap_down_frame = None
                            else:
                                tap_down_frame = sent_frame
                        event("down" if args.play else "would_press", dsp=sent_dsp, frame=sent_frame,
                              predicted_error_ms=(sent_dsp-first["target"])*1000,
                              input_lead_ms=input_lead*1000, frame_period_ms=frame_period*1000, records=group)
                    for record in group:
                        consumed.add((record["pointer"], record["raw_target"]))
            time.sleep(0.001)
        else:
            result["status"] = "timeout"
        result["metrics"] = last_metrics
        event("finished", **result)
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
        event("error", error=str(error))
        try:
            last_metrics = game.metrics(game.state())
        except Exception:
            pass
    finally:
        result["metrics"] = last_metrics
        try:
            if keyboard:
                keyboard.close()
        finally:
            game.reader.close()
            log.close()
            result["elapsed_seconds"] = time.perf_counter() - begin
            sample.ins.mem.save(args.run_name + "-result.json", result)
    return result


if __name__ == "__main__":
    sys.exit(1 if main()["status"] == "error" else 0)
