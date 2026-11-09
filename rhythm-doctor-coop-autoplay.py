"""双人自动输入：只读 Mono 内存，分别向两位玩家发送普通键盘输入。"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path
import statistics
import struct
import time

ROOT = Path(__file__).resolve().parent


def independent_hit_offsets(raw_offsets, rank_stage, defib_modes, previous=None):
    """还原 GetRankFromMistakes 在双人结算时追加到 P1 的 P2 判定。"""
    p1, p2 = (list(values) for values in raw_offsets)
    # 两位玩家均为 Hard 时，GetRankFromMistakes 直接给 plus，不合并列表。
    if rank_stage < 2 or tuple(defib_modes) == (4, 4) or not p2:
        return [p1, p2], "separate_player_lists"
    # trueGameover 先变为 2，随后才进入 ShowAndSaveRank。若刚好在这段
    # 过渡中读取，保留已经确认的独立列表，不能减去尚未追加的 P2 数据。
    if previous is not None and [p1, p2] == previous:
        return [p1, p2], "rank_merge_pending"
    if len(p1) < len(p2) or p1[-len(p2):] != p2:
        raise RuntimeError("双人结算列表不符合已验证的 P2 追加布局")
    own_p1 = p1[:-len(p2)]
    if previous is not None and own_p1[:len(previous[0])] != previous[0]:
        raise RuntimeError("结算列表还原后不包含已观察到的 P1 判定")
    return [own_p1, p2], "rank_p2_append_removed"


def load_api():
    spec = importlib.util.spec_from_file_location("rd_coop_base", ROOT / "rhythm-doctor-live-autoplay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_input_class(api, game):
    """独立缓存 RDInput，不修改共享 runtime 布局。"""
    runtime = api.load_module("rd_coop_runtime", "rhythm-doctor-runtime.py")
    identity = runtime.process_identity(game.reader)
    cache_path = ROOT / "coop-runtime-layout.json"

    def valid(address):
        meta = api.objmod.metadata(game.reader, address)
        if meta["name"] != "RDInput" or meta["namespace"] != "":
            raise ValueError("RDInput 类型身份不匹配")
        if not {"p1", "p2"} <= {field["name"] for field in meta["fields"]}:
            raise ValueError("RDInput 缺少玩家输入列表")
        return address

    if "RDInput" in game.layout:
        return valid(game.layout["RDInput"])
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache["identity"] == identity:
            return valid(cache["RDInput"])
    except (OSError, ValueError, RuntimeError, KeyError):
        pass
    parser = api.load_module("rd_coop_type_names", "managed-type-name-offsets.py")
    assembly = Path(game.reader.path).with_name("Rhythm Doctor_Data") / "Managed" / "Assembly-CSharp.dll"
    info = parser.read_type_name_offsets(assembly, ["RDInput"])["types"]["RDInput"]
    anchor = game.reader.u64(game.layout["scnGame"] + 0x48)
    name_pointer = anchor + info["delta_from_anchor"]
    if game.reader.cstring(name_pointer) != "RDInput":
        raise ValueError("RDInput 名称表与当前进程不匹配")
    image = game.reader.u64(game.layout["scnGame"] + 0x40)
    refs = game.reader.scan([struct.pack("<Q", name_pointer)], alignment=8)
    if refs.get("truncated"):
        raise RuntimeError("RDInput 类型引用扫描超过上限")
    found = set()
    for ref in refs["hits"]:
        address = ref["address"] - 0x48
        try:
            if (game.reader.u64(address + 0x40) == image
                    and game.reader.u32(address + 0x58) == int(info["token"], 16)):
                found.add(valid(address))
        except (OSError, ValueError, RuntimeError):
            continue
    if len(found) != 1:
        raise RuntimeError("没有唯一定位 RDInput")
    address = found.pop()
    runtime.atomic_json(cache_path, {"identity": identity, "RDInput": address})
    return address


class CoopGame:
    def __init__(self, api, pid, layout):
        self.api = api
        classes = layout.get("classes", layout)
        self.base = api.Game(pid, classes)
        try:
            self.gc_meta = api.objmod.metadata(self.reader, classes["GC"])
            self.gc_fields = api.fields(self.gc_meta)
            self.gc_static = api.sample.static_base(self.reader, self.gc_meta)
            self.input_meta = api.objmod.metadata(self.reader, resolve_input_class(api, self.base))
            self.input_fields = api.fields(self.input_meta)
            self.input_static = api.sample.static_base(self.reader, self.input_meta)
            self.calibration_fields = api.fields(self.calibration_meta)
            self.input_object_names = {}
        except Exception:
            self.reader.close()
            raise

    def __getattr__(self, name):
        return getattr(self.base, name)

    def two_player(self):
        return bool(self.reader.read(self.gc_static + self.gc_fields["twoPlayerMode"], 1)[0])

    def key_schemes(self):
        schemes = []
        for field in ("p1", "p2"):
            pointer = self.reader.u64(self.input_static + self.input_fields[field])
            array, count = self.reader.u64(pointer + 16), self.reader.i32(pointer + 24)
            if not 1 <= count <= 16:
                raise RuntimeError("玩家输入设备列表异常")
            keyboard_schemes = set()
            for index in range(count):
                obj = self.reader.u64(array + 32 + index * 8)
                cls = self.reader.u64(self.reader.u64(obj))
                if cls not in self.input_object_names:
                    self.input_object_names[cls] = self.reader.cstring(self.reader.u64(cls + 0x48))
                if self.input_object_names[cls] == "RDInputType_Keyboard":
                    keyboard_schemes.add(self.reader.i32(obj + self.object_fields(obj)["schemeIndex"]))
            if len(keyboard_schemes) != 1:
                raise RuntimeError("未找到唯一玩家键盘配置")
            schemes.append(keyboard_schemes.pop())
        if sorted(schemes) != [0, 1]:
            raise RuntimeError("两位玩家的键盘配置不独立")
        return tuple(schemes)

    def calibration(self, player):
        separate = bool(self.reader.read(self.gc_static + self.gc_fields["d_separateP2Calibration"], 1)[0])
        name = "calibration_i_P2_internal" if player == 1 and separate else "calibration_i"
        return struct.unpack("<f", self.reader.read(self.calibration_base + self.calibration_fields[name], 4))[0]

    def read_beat(self, pointer, active_hold=False):
        if not pointer or not self.is_beat(pointer):
            return None
        data = self.reader.read(pointer, self.beat_meta["instance_size"])
        value = lambda name, fmt: self.api.unpack(data, self.bf[name], fmt)
        if not active_hold and any(value(name, "B") for name in ("dead", "unhittable", "bomb", "hasPulsed7thBeat")):
            return None
        row = value("row", "Q")
        if not row:
            return None
        row_data = self.reader.read(row, self.row_meta["instance_size"])
        player = self.api.unpack(row_data, self.rf["multiplayer"], "i")
        if player not in (0, 1):
            return None
        if not active_hold and self.api.unpack(row_data, self.rf["dead"], "B"):
            return None
        calibration = self.calibration(player)
        raw, release_raw = value("inputTimeSansCalibration", "d"), value("releaseTimeSansCalibration", "d")
        driven_time = value("playerDriven7thBeatHitTime", "d")
        driven = value("playerDrives7thBeat", "B") and driven_time != 0
        target = (driven_time if driven else raw) + calibration
        release = driven_time + release_raw - raw if driven else release_raw + calibration
        if not math.isfinite(target) or not math.isfinite(release):
            return None
        return {"pointer": hex(pointer), "row": self.api.unpack(row_data, self.rf["<id>k__BackingField"], "i"),
                "player": player, "target": target, "raw_target": raw, "release": release,
                "hold": bool(value("isHeldClap", "B")), "calibration": calibration}

    def beats(self, pointer):
        header = self.reader.read(pointer + 16, 16)
        array, count, _ = struct.unpack("<Qii", header)
        if not 0 <= count <= 1024:
            raise RuntimeError("节拍列表长度异常")
        capacity = self.reader.u64(array + 24)
        if not count <= capacity <= 65536:
            return [], count
        records = []
        for index in range(count):
            record = self.read_beat(self.reader.u64(array + 32 + index * 8))
            if record:
                records.append(record)
        if self.reader.read(pointer + 16, 16) != header:
            return [], count
        return sorted(records, key=lambda record: record["target"]), count

    def active_holds(self, state):
        holds = []
        for row in self.pointers(state["rows"]):
            if not row or self.reader.i32(row + self.rf["multiplayer"]) not in (0, 1):
                continue
            ent = self.reader.u64(row + self.rf["ent"])
            if not ent:
                continue
            box = self.reader.u64(ent + self.object_fields(ent)["playerBox"])
            if not box:
                continue
            fields = self.object_fields(box)
            beat = self.reader.u64(box + fields["currentHoldBeat"])
            if beat:
                record = self.read_beat(beat, active_hold=True)
                if not record:
                    raise RuntimeError("双人长按对象类型或玩家身份异常")
                record["hold_assist"] = bool(self.reader.read(box + fields["holdAssist"], 1)[0])
                holds.append(record)
        return holds

    def release_margin(self, state, player):
        mode = self.reader.i32(self.static + self.gf["p1DefibMode" if player == 0 else "p2DefibMode"])
        margin = .4 if mode in (0, 1) else .12 if mode == 2 else .08
        obj = state["level_object"]
        multiplier = struct.unpack("<f", self.reader.read(obj + self.object_fields(obj)["hitMarginMultiplier"], 4))[0]
        return margin * multiplier if multiplier > 0 else margin

    def metrics(self, state):
        data = self.reader.read(state["mistakes"], self.mm["instance_size"])
        for attempt in range(3):
            rank_stage = self.rank_stage(state)
            raw_offsets, headers = [], []
            for player in (0, 1):
                prefix = "p1" if player == 0 else "p2"
                pointer = self.reader.u64(self.static + self.gf[prefix + "HitTimes"])
                header = self.reader.read(pointer + 16, 16)
                array, count, _ = struct.unpack("<Qii", header)
                if not 0 <= count <= 100000:
                    raise RuntimeError("玩家命中数量异常")
                offsets = list(struct.unpack("<" + "f" * count, self.reader.read(array + 32, 4 * count))) if count else []
                headers.append((pointer, header))
                raw_offsets.append(offsets)
            rank_raw = self.reader.i32(state["rankscreen"] + self.rank_offset)
            if (self.rank_stage(state) == rank_stage
                    and all(self.reader.read(pointer + 16, 16) == header for pointer, header in headers)):
                break
        else:
            raise RuntimeError("玩家判定列表持续改变，无法获得一致快照")
        modes = [self.reader.i32(self.static + self.gf[name]) for name in ("p1DefibMode", "p2DefibMode")]
        offsets_by_player, storage = independent_hit_offsets(
            raw_offsets, rank_stage, modes, self.__dict__.get("_last_unmerged_hit_offsets"))
        if rank_stage < 2:
            self._last_unmerged_hit_offsets = [list(values) for values in offsets_by_player]
        players = []
        all_offsets = []
        for player, offsets in enumerate(offsets_by_player):
            mistake_field = "mistakesCountP1" if player == 0 else "mistakesCountP2"
            players.append({"player": player, "mistakes": self.api.unpack(data, self.mf[mistake_field], "f"),
                            "successful_hits": len(offsets),
                            "max_abs_error_ms": max(offsets) * 1000 if offsets else None})
            all_offsets.extend(offsets)
        return {"mistakes": sum(player["mistakes"] for player in players),
                "successful_hits": len(all_offsets), "players": players,
                "rank_raw": rank_raw, "hit_times_storage": storage,
                "raw_hit_list_counts": [len(values) for values in raw_offsets],
                "mean_abs_error_ms": statistics.fmean(all_offsets) * 1000 if all_offsets else None,
                "max_abs_error_ms": max(all_offsets) * 1000 if all_offsets else None,
                "under_40ms": sum(offset < .04 for offset in all_offsets)}


class DualKeyboard:
    # scheme 0 属于右半键盘，scheme 1 属于左半键盘。
    SCHEMES = {0: {"key": "space", "scan": 0x39, "vk": 0x20},
               1: {"key": "f", "scan": 0x21, "vk": 0x46}}

    def __init__(self, api, pid, schemes):
        self.keys = api.keyboard_mod
        self.window = self.keys.Keyboard(pid)
        self.mapping = [self.SCHEMES[scheme] for scheme in schemes]
        self.held = [False, False]

    def is_foreground(self):
        return self.window.is_foreground()

    def activate(self):
        self.window.activate()

    def ensure_neutral(self):
        if any(self.keys.U.GetAsyncKeyState(key["vk"]) & 0x8000 for key in self.mapping):
            raise RuntimeError("用户仍按住双人游玩按键，未接管输入")

    def set_players(self, changes):
        changes = [(player, down) for player, down in sorted(changes.items()) if self.held[player] != down]
        if not changes:
            return
        if any(down for _, down in changes) and not self.is_foreground():
            raise RuntimeError("游戏失去前台焦点，停止双人输入")
        events = (self.keys.INPUT * len(changes))(*[
            self.keys.INPUT(type=1, keyboard=self.keys.KI(vk=0, scan=self.mapping[player]["scan"], flags=0x8 if down else 0xA))
            for player, down in changes])
        sent = self.keys.U.SendInput(len(events), events, self.keys.C.sizeof(self.keys.INPUT))
        for player, down in changes[:sent]:
            self.held[player] = down
        if sent != len(events):
            raise self.keys.C.WinError(self.keys.C.get_last_error())

    def close(self):
        errors = []
        # 一位玩家的释放失败不应跳过另一位玩家。
        for player in (0, 1):
            try:
                self.set_players({player: False})
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("释放双人按键失败：" + "; ".join(errors))


class Channel:
    def __init__(self, player):
        self.player = player
        self.down_frame = None
        self.up_frame = None
        self.awaiting_hold = None
        self.release_wait = None
        self.accepted = {}
        self.consumed = set()


class CoopScheduler:
    """每位玩家独立等待长按确认、松开及下一个节拍，同帧输入批量提交。"""
    def __init__(self, game, keyboard, event, result, lead_ms=10):
        self.game, self.keyboard, self.event, self.result = game, keyboard, event, result
        self.channels = [Channel(0), Channel(1)]
        self.lead_ms = lead_ms

    def step(self, state, dsp, frame, wall, frame_period):
        game, keyboard = self.game, self.keyboard
        holds = game.active_holds(state)
        records, _ = game.beats(state["beats"])
        changes, actions = {}, []
        for channel in self.channels:
            player = channel.player
            active = [record for record in holds if record["player"] == player]
            pointers = {record["pointer"] for record in active}
            for record in active:
                if record["pointer"] not in channel.accepted:
                    self.result["holds_accepted"] += 1
                    self.event("hold_accepted", player=player, dsp=dsp, record=record)
                channel.accepted[record["pointer"]] = record
            if channel.awaiting_hold:
                channel.awaiting_hold["unseen"].difference_update(pointers)
                if not channel.awaiting_hold["unseen"]:
                    channel.awaiting_hold = None
                    channel.down_frame = None
                elif wall - channel.awaiting_hold["wall"] > .25:
                    raise RuntimeError(f"P{player + 1} 长按按下后未确认接住")
            if channel.release_wait:
                wait = channel.release_wait
                if not (wait["pointers"] & pointers) and frame != wait["frame"]:
                    histories = {row: game.row_history(state, row) for row in wait["histories"]}
                    metrics = game.metrics(state)
                    perfect = metrics["players"][player]["mistakes"] == wait["mistakes"] and all(
                        old is not None and histories[row] is not None
                        and histories[row]["count"] > old["count"] and histories[row]["last"] == 0
                        for row, old in wait["histories"].items())
                    self.result["holds_released_perfect" if perfect else "holds_release_unverified"] += len(wait["pointers"])
                    self.event("hold_release_checked", player=player, dsp=dsp, perfect=perfect, histories=histories)
                    for pointer in wait["pointers"]:
                        channel.accepted.pop(pointer, None)
                    channel.release_wait = None
                elif wall - wait["wall"] > .35 and not any(record["hold_assist"] for record in active):
                    raise RuntimeError(f"P{player + 1} 松开后游戏仍保留长按状态")
            if keyboard.held[player] and not channel.awaiting_hold:
                if active:
                    manual = [record for record in active if not record["hold_assist"]]
                    if manual:
                        margin = game.release_margin(state, player)
                        low = max(record["release"] - margin for record in manual)
                        high = min(record["release"] + margin for record in manual)
                        if high - low < .020:
                            raise RuntimeError(f"P{player + 1} 多个长按没有共同松开窗口")
                        target = (low + high) / 2
                    else:
                        target = dsp
                    if dsp >= target - self.lead_ms / 1000:
                        channel.release_wait = {"pointers": pointers, "wall": wall, "frame": frame,
                            "histories": {record["row"]: game.row_history(state, record["row"]) for record in active},
                            "mistakes": game.metrics(state)["players"][player]["mistakes"]}
                        changes[player] = False
                        actions.append((channel, "up", {"hold_release_target": target, "records": active}))
                elif channel.accepted:
                    self.result["holds_release_unverified"] += len(channel.accepted)
                    channel.accepted.clear()
                    changes[player] = False
                    actions.append((channel, "up", {"hold_ended_without_manual_release": True}))
                elif channel.down_frame is not None and frame != channel.down_frame:
                    changes[player] = False
                    actions.append((channel, "up", {"hold_release_target": None}))
            if (keyboard.held[player] or channel.awaiting_hold or channel.release_wait or active
                    or state["ignore_input"] or state["state"] not in (2, 3) or frame == channel.up_frame):
                continue
            pending = [record for record in records if record["player"] == player
                       and (record["pointer"], record["raw_target"]) not in channel.consumed]
            if not pending:
                continue
            first = pending[0]
            input_lead = max(self.lead_ms / 1000, frame_period * 1.5)
            if dsp < first["target"] - input_lead:
                continue
            group = [record for record in pending if abs(record["target"] - first["target"]) < .009]
            if dsp - first["target"] > .150:
                self.event("expired", player=player, dsp=dsp, records=group)
                for record in group:
                    channel.consumed.add((record["pointer"], record["raw_target"]))
                continue
            changes[player] = True
            actions.append((channel, "down", {"records": group, "input_lead_ms": input_lead * 1000}))
        if changes:
            sent_dsp, sent_frame = game.clock()
            keyboard.set_players(changes)
            for channel, kind, info in actions:
                if kind == "down":
                    self.result["actions"] += 1
                    self.result["actions_by_player"][channel.player] += 1
                    group = info["records"]
                    for record in group:
                        channel.consumed.add((record["pointer"], record["raw_target"]))
                    caught = {record["pointer"] for record in group if record["hold"]}
                    channel.awaiting_hold = {"unseen": caught, "wall": wall} if caught else None
                    channel.down_frame = None if caught else sent_frame
                    info["predicted_error_ms"] = (sent_dsp - group[0]["target"]) * 1000
                else:
                    channel.down_frame = None
                    channel.up_frame = sent_frame
                    if channel.release_wait:
                        channel.release_wait["frame"] = sent_frame
                self.event(kind, player=channel.player, dsp=sent_dsp, frame=sent_frame, **info)


class Cancelled(Exception):
    pass


def run(api, pid, layout, stop_requested=None, event_sink=None, start=True, no_activate=True,
        max_seconds=300, run_name=None, start_validator=None, lead_ms=10,
        _game_factory=None, _keyboard_factory=None):
    """接管一次双人正式关卡；结束即释放两个键，不进行菜单导航。"""
    stop_requested = stop_requested or (lambda: False)
    name = run_name or ("coop-play-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    path = ROOT / (name + ".jsonl")
    if path.parent != ROOT:
        raise ValueError("run_name 必须是不含路径的文件名")
    result = {"pid": pid, "level": None, "status": "running", "two_player": True,
              "actions": 0, "actions_by_player": [0, 0], "holds_accepted": 0,
              "holds_released_perfect": 0, "holds_release_unverified": 0,
              "started_utc": datetime.now(timezone.utc).isoformat(), "metrics": {}}
    game = keyboard = None
    begin = time.perf_counter()
    with path.open("x", encoding="utf-8") as log:
        def event(kind, **values):
            item = {"event": kind, "elapsed": time.perf_counter() - begin, **values}
            log.write(json.dumps(item, ensure_ascii=False) + "\n")
            log.flush()
            if event_sink:
                event_sink(item)

        def check():
            if stop_requested():
                raise Cancelled()
            if keyboard and not keyboard.is_foreground():
                result["interruption"] = "focus_lost"
                raise RuntimeError("游戏失去前台焦点，停止双人输入")
            if game.scene_changed():
                raise RuntimeError("游戏场景已改变，停止双人输入")
            if not game.two_player():
                raise RuntimeError("游戏已退出双人模式，停止输入")

        try:
            if stop_requested():
                raise Cancelled()
            game = (_game_factory or CoopGame)(api, pid, layout)
            result.update(level=game.level, instance=hex(game.instance))
            state = game.state()
            if not game.two_player() or state["paused"] or state["failed"] or game.rank_stage(state) >= 2:
                raise RuntimeError("当前不是可接管的双人正式关卡")
            if state["state"] not in ((0, 1, 2, 3) if start else (2, 3)):
                raise RuntimeError("双人关卡状态不适合开始或接管")
            game.active_holds(state)
            schemes = game.key_schemes()
            keyboard = (_keyboard_factory or DualKeyboard)(api, pid, schemes)
            if not no_activate:
                keyboard.activate()
            check()
            keyboard.ensure_neutral()
            result["key_schemes"] = list(schemes)
            result["initial_metrics"] = game.metrics(state)
            result["metrics"] = result["initial_metrics"]
            if start_validator is not None and not start_validator(game):
                raise RuntimeError("双人关卡已改变，取消接管")
            # 元数据定位期间用户可能已经开始，必须重读后再决定是否发开始键。
            state = game.state()
            if (state["paused"] or state["failed"] or game.rank_stage(state) >= 2
                    or state["state"] not in ((0, 1, 2, 3) if start else (2, 3))):
                raise RuntimeError("接管前双人关卡状态已变化")
            if state["state"] == 0:
                if game.key_schemes() != schemes:
                    raise RuntimeError("双人键位已改变，取消开始")
                if state["ignore_input"]:
                    raise RuntimeError("双人开始界面尚未接受输入")
                keyboard.ensure_neutral()
                keyboard.set_players({0: True, 1: True})
                try:
                    deadline = time.perf_counter() + 3
                    while game.state()["state"] == 0:
                        check()
                        if start_validator is not None and not start_validator(game):
                            raise RuntimeError("双人开始过程中场景验证失败")
                        if time.perf_counter() >= deadline:
                            raise RuntimeError("双人开始输入未被游戏确认")
                        time.sleep(.005)
                    result["entry"] = "start"
                finally:
                    keyboard.close()
            else:
                if game.active_holds(state):
                    raise RuntimeError("当前有未结束的双人长按，未接管")
                result["entry"] = "attach"
            event("started", level=game.level, entry=result["entry"], key_schemes=schemes,
                  initial_metrics=result["initial_metrics"])
            scheduler = CoopScheduler(game, keyboard, event, result, lead_ms)
            if result["entry"] == "attach":
                now_dsp, _ = game.clock()
                records, _ = game.beats(game.state()["beats"])
                for record in records:
                    if record["target"] <= now_dsp + .02:
                        scheduler.channels[record["player"]].consumed.add((record["pointer"], record["raw_target"]))
                        event("attach_skipped_past_target", record=record)
            last_frame = -1
            last_wall = None
            frame_period = 1 / 60
            next_progress = next_mapping_check = time.perf_counter()
            last_state = None
            while time.perf_counter() - begin < max_seconds:
                check()
                dsp, frame = game.clock()
                wall = time.perf_counter()
                if frame == last_frame:
                    time.sleep(.001)
                    continue
                if last_wall is not None and frame > last_frame:
                    period = (wall - last_wall) / (frame - last_frame)
                    if 1 / 500 <= period <= .1:
                        frame_period = .8 * frame_period + .2 * period
                last_frame, last_wall = frame, wall
                state = game.state()
                if wall >= next_mapping_check:
                    if game.key_schemes() != schemes:
                        raise RuntimeError("玩家键位在游玩期间改变，停止双人输入")
                    next_mapping_check = wall + 1
                state_key = state["state"], state["paused"], state["failed"]
                if state_key != last_state:
                    event("state", dsp=dsp, state=state["state"], paused=state["paused"], failed=state["failed"])
                    last_state = state_key
                if state["paused"]:
                    raise RuntimeError("游戏已暂停，停止双人输入")
                rank_stage = game.rank_stage(state)
                if state["failed"]:
                    result["status"] = "level_failed"
                    break
                if state["state"] == 6 or rank_stage >= 2:
                    result["status"] = "rank_shown"
                    keyboard.close()
                    # 结算状态可先于 ShowAndSaveRank；短暂等待读取最终分数。
                    settle_deadline = time.perf_counter() + .2
                    while time.perf_counter() < settle_deadline:
                        if stop_requested() or game.scene_changed():
                            break
                        time.sleep(.01)
                    break
                scheduler.step(state, dsp, frame, wall, frame_period)
                if wall >= next_progress:
                    result["metrics"] = game.metrics(state)
                    event("progress", dsp=dsp, rank_stage=rank_stage, **result["metrics"])
                    next_progress = wall + 10
                time.sleep(.001)
            else:
                result["status"] = "timeout"
        except Cancelled:
            result["status"] = "cancelled"
        except Exception as error:
            result["status"] = "error"
            result["error"] = str(error)
            event("error", error=str(error))
        finally:
            if keyboard:
                try:
                    keyboard.close()
                except Exception as error:
                    result["cleanup_error"] = str(error)
                    result["status"] = "error"
            if game:
                try:
                    if not game.scene_changed():
                        result["metrics"] = game.metrics(game.state())
                except Exception as error:
                    result["metrics_error"] = str(error)
                finally:
                    game.reader.close()
            result["elapsed_seconds"] = time.perf_counter() - begin
            (ROOT / (name + "-result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            event("finished", **result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--layout-json", default=str(ROOT / "app-runtime-layout.json"))
    parser.add_argument("--play", action="store_true", help="实际发送双人输入；省略时只读检查")
    parser.add_argument("--attach-running", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--run-name")
    args = parser.parse_args()
    api = load_api()
    layout = json.loads(Path(args.layout_json).read_text(encoding="utf-8"))
    if not args.play:
        game = CoopGame(api, args.pid, layout)
        try:
            state = game.state()
            records, count = game.beats(state["beats"])
            print(json.dumps({"level": game.level, "two_player": game.two_player(),
                              "key_schemes": game.key_schemes(), "calibration": [game.calibration(0), game.calibration(1)],
                              "state": state["state"], "beats_count": count, "records": records,
                              "metrics": game.metrics(state)}, ensure_ascii=False, indent=2))
        finally:
            game.reader.close()
        return 0
    result = run(api, args.pid, layout, start=not args.attach_running, no_activate=not args.activate,
                 max_seconds=args.max_seconds, run_name=args.run_name,
                 event_sink=lambda item: print(json.dumps(item, ensure_ascii=False), flush=True)
                 if item["event"] in ("started", "progress", "finished", "error") else None)
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
