"""只读 Rhythm Weightlifter 状态，并以普通 J/F 按键完成十二轮。

run_campaign(api, pid, layout, stop_requested=..., event_sink=...) 不切换前台。
直接运行默认只读；只有 --play 才发送输入。没有写进程、存档或 Steam API。
"""
import argparse
import ctypes
import importlib.util
import json
import math
from pathlib import Path
import struct
import time

ROOT = Path(__file__).resolve().parent
SCENE = "scnRhythmWeightlifter"
SCREENS = ("intro", "level_select", "gameplay", "ending", "delete_data", "language", "extra")


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WeightlifterReader:
    """通过经验证的 scnBase._instance 找到小游戏；不依赖固定对象地址。"""
    def __init__(self, api, pid, layout):
        navigation = load_module("rd_wl_navigation", "rhythm-doctor-navigation.py")
        self.nav = navigation.SceneReader(api, pid, layout.get("classes", layout))
        self.reader = self.nav.reader
        self.field_cache = {}
        self.level_cache = None
        self.level_cache_time = 0.0
        self.last_levels_pointer = None

    def close(self):
        self.nav.close()

    def fields(self, cls):
        if cls not in self.field_cache:
            self.field_cache[cls] = {name: pair[0] for name, pair in self.nav.fields(cls).items()}
        return self.field_cache[cls]

    def block(self, obj):
        if not obj:
            raise ValueError("小游戏对象尚未就绪")
        cls = self.nav.cls(obj)
        meta = self.nav.meta(cls)
        return self.fields(cls), self.reader.read(obj, meta["instance_size"])

    @staticmethod
    def value(block, name):
        fields, data = block
        field = fields[name]
        kind, at = field["mono_type"], field["offset"]
        if field["static"] or at < 0:
            raise ValueError("不能从对象读取静态或无效字段：" + name)
        fmt = {2: "B", 8: "i", 9: "I", 10: "q", 11: "Q", 12: "f", 13: "d", 17: "i"}.get(kind, "Q")
        value = struct.unpack_from("<" + fmt, data, at)[0]
        if kind == 2:
            if value not in (0, 1):
                raise ValueError("布尔字段无效：" + name)
            return bool(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("浮点字段无效：" + name)
        return value

    def tween_playing(self, obj):
        if not obj:
            return False
        b = self.block(obj)
        values = {}
        for key in ("active", "isPlaying"):
            name = next((name for name in (key, "<" + key + ">k__BackingField") if name in b[0]), None)
            if name is None:
                raise ValueError("无法验证场景过渡状态：" + key)
            values[key] = self.value(b, name)
        return values["active"] and values["isPlaying"]

    def dialogue(self, obj):
        if not obj:
            return {"playing": False, "knot": ""}
        b = self.block(obj)
        playing = self.value(b, "isPlaying")
        knot_ptr = self.value(b, "currentKnot")
        return {"playing": playing, "knot": self.reader.managed_string(knot_ptr) if knot_ptr else ""}

    def weight(self, obj):
        b = self.block(obj)
        pointer = self.value(b, "hitTimes")
        count = self.reader.i32(pointer + 24) if pointer else 0
        if not 0 <= count <= 10000:
            raise ValueError("举重输入记录数量异常")
        array = self.reader.u64(pointer + 16) if count else 0
        first = struct.unpack("<f", self.reader.read(array + 32, 4))[0] if count else None
        return {"pointer": obj, "weight": self.value(b, "_currentWeight"),
                "y": self.value(b, "_y"), "inconsistent": self.value(b, "_inconsistency"),
                "showing": self.value(b, "showing"), "hit_timer": self.value(b, "hitTimer"),
                "hit_list": pointer, "hit_count": count, "last_hit_game_time": first}

    def levels(self, pointer, force=False):
        now = time.perf_counter()
        if not force and pointer == self.last_levels_pointer and self.level_cache is not None and now - self.level_cache_time < .25:
            return self.level_cache
        count = self.reader.u64(pointer + 24) if pointer else 0
        if count != 12:
            raise ValueError(f"当前版本小游戏关卡数量不是十二：{count}")
        result = []
        for i in range(count):
            obj = self.reader.u64(pointer + 32 + i * 8)
            b = self.block(obj)
            result.append({name: self.value(b, name) for name in
                           ("index", "player1Weight", "player2Weight", "bpm", "consistency", "speed", "score")})
        self.level_cache, self.level_cache_time, self.last_levels_pointer = result, now, pointer
        return result

    def snapshot(self):
        obj = self.reader.u64(self.nav.slot)
        meta = self.nav.meta(self.nav.cls(obj)) if obj else {"name": "None", "namespace": ""}
        result = {"scene_type": meta["name"], "scene_pointer": obj}
        if meta["name"] != SCENE:
            return result
        if meta["namespace"] != "RhythmWeightlifter":
            raise ValueError("小游戏场景 namespace 不符")
        b = self.block(obj)
        mapping = {"screen_id": "screen", "level_index": "_levelIndex", "last_unlocked": "lastUnlockedLevelIndex",
                   "can_receive_input": "canReceiveInput", "paused": "paused", "showing_score": "_showingScore",
                   "game_state": "_gameState", "failure": "failure", "infinite": "isInfiniteMode",
                   "should_advance": "shouldAdvanceLevel", "should_show_ending": "shouldShowEnding",
                   "extra_slot": "currentExtraSlot", "last_dialogue_game_time": "lastTimeDialogueWasPlayed"}
        result.update({name: self.value(b, field) for name, field in mapping.items()})
        screen = result["screen_id"]
        if not 0 <= screen < len(SCREENS):
            raise ValueError("小游戏画面状态无效")
        result["screen"] = SCREENS[screen]
        result["transition_playing"] = self.tween_playing(self.value(b, "sceneTransitionTween"))
        result["dialogues"] = [self.dialogue(self.value(b, field)) for field in ("inGameInk", "outGameInk")]
        result["dialogue_playing"] = any(item["playing"] for item in result["dialogues"])
        bb = self.block(self.value(b, "barbell"))
        result["progress"] = self.value(bb, "progress")
        result["p1"] = self.weight(self.value(bb, "p1Weight"))
        result["p2"] = self.weight(self.value(bb, "p2Weight"))
        result["levels"] = self.levels(self.value(b, "levels"), force=result["showing_score"])
        result["campaign_complete"] = result["last_unlocked"] >= len(result["levels"]) and all(level["score"] > 0 for level in result["levels"])
        # 在读取过程中发生场景切换时，不让调用方使用已离开的场景作输入决策。
        if self.reader.u64(self.nav.slot) != obj:
            return {"scene_type": "scene_changed", "scene_pointer": 0}
        return result


class RatioSchedule:
    """每侧频率与重量成正比，共同相位避免两侧累计高度偏离。"""
    def __init__(self, weights, now, base_hz=1.8):
        if len(weights) != 2 or any(type(w) is not int or not 1 <= w <= 6 for w in weights):
            raise ValueError("不支持的举重重量")
        if not 1.0 <= base_hz <= 1.8:
            raise ValueError("举重基础频率超出验证范围")
        self.periods = [1.0 / (base_hz * weight) for weight in weights]
        self.origin = now + .06
        self.counts = [0, 0]

    def due(self, now):
        result = []
        for i, period in enumerate(self.periods):
            deadline = self.origin + self.counts[i] * period
            if now >= deadline:
                if now - deadline > min(.06, period * .45):
                    raise RuntimeError("输入调度延迟超过安全范围，已停止以避免连续补键")
                result.append("p1" if i == 0 else "p2")
                self.counts[i] += 1
        return result


class WeightlifterKeys:
    """J=P1、F=P2。所有按住状态均有截止时间，close 无条件释放。"""
    SCANS = {"p1": (0x24, 0), "p2": (0x21, 0), "escape": (0x01, 0),
             "left": (0x4B, 1), "right": (0x4D, 1)}

    def __init__(self, keyboard_module, pid):
        self.mod = keyboard_module
        self.window = keyboard_module.Keyboard(pid)
        self.held = {}

    def is_foreground(self):
        return self.window.is_foreground()

    def check_idle(self):
        if any(self.mod.U.GetAsyncKeyState(vk) & 0x8000 for vk in (0x4A, 0x46, 0x1B, 0x25, 0x27, 0x10, 0x11, 0x12, 0x5B, 0x5C)):
            raise RuntimeError("用户仍按住游玩键、方向键或修饰键，未接管小游戏")

    def send(self, names, down):
        names = list(names)
        if not names:
            return
        if down and not self.is_foreground():
            raise RuntimeError("游戏失去前台焦点")
        events = (self.mod.INPUT * len(names))()
        for i, name in enumerate(names):
            scan, extended = self.SCANS[name]
            events[i] = self.mod.INPUT(type=1, keyboard=self.mod.KI(vk=0, scan=scan, flags=0x8 | extended | (0 if down else 2)))
        sent = self.mod.U.SendInput(len(events), events, ctypes.sizeof(self.mod.INPUT))
        # 部分成功也要把可能按下的键登记，以便 finally 尝试释放全部。
        if down:
            for name in names:
                self.held.setdefault(name, math.inf)
        if sent != len(events):
            raise ctypes.WinError(ctypes.get_last_error())
        if not down:
            for name in names:
                self.held.pop(name, None)

    def press(self, names, now, duration=.032):
        names = [name for name in names if name not in self.held]
        self.send(names, True)
        for name in names:
            self.held[name] = now + duration

    def release_due(self, now):
        self.send([name for name, deadline in self.held.items() if now >= deadline], False)

    def close(self):
        self.send(list(self.held), False)


def run_campaign(api, pid, layout, *, stop_requested=None, event_sink=None, max_seconds=600,
                 log_path=None, exit_when_complete=True, base_hz=1.8):
    """完成当前存档剩余轮次；不主动激活窗口。event_sink 接收字典。

    status: campaign_complete / cancelled / focus_lost / scene_changed /
            timed_out / blocked。campaign_complete 要求十二轮各有非零成绩。
    exit_when_complete=True 时正常经过结尾并退出到游戏选关，再返回。
    """
    stop_requested = stop_requested or (lambda: False)
    event_sink = event_sink or (lambda event: None)
    reader = keys = logfile = timer = None
    started = time.perf_counter()
    latest = None
    finished = False
    rounds = []
    status, reason = "blocked", "未开始"
    timer_started = False

    def emit(kind, **data):
        event = {"type": "weightlifter", "event": kind, "elapsed": time.perf_counter() - started, **data}
        if logfile:
            logfile.write(json.dumps(event, ensure_ascii=False) + "\n")
            logfile.flush()
        event_sink(event)

    try:
        reader = WeightlifterReader(api, pid, layout)
        keys = WeightlifterKeys(api.keyboard_mod, pid)
        if not keys.is_foreground():
            return {"status": "focus_lost", "reason": "游戏不在前台", "level": "RhythmWeightlifter", "rounds": []}
        keys.check_idle()
        if log_path:
            logfile = Path(log_path).open("w", encoding="utf-8")
        timer = ctypes.WinDLL("winmm")
        timer_started = timer.timeBeginPeriod(1) == 0
        schedule = None
        attempt = None
        next_read = next_report = next_ui = 0.0
        quiet_until = started + 1.1
        previous_state = None
        failure_seen = False
        failures = 0
        completed_keys = set()
        initial_hits = [0, 0]
        exit_requested_at = None
        exit_read_error = None
        while time.perf_counter() - started < max_seconds:
            now = time.perf_counter()
            if stop_requested():
                status, reason = "cancelled", "调用方请求停止"
                break
            if not keys.is_foreground():
                status, reason = "focus_lost", "游戏失去前台焦点"
                break
            keys.release_due(now)
            if now >= next_read:
                try:
                    observed = reader.snapshot()
                except (OSError, ValueError, IndexError) as error:
                    # Escape 已触发退出时，Unity 可能先销毁旧场景对象，
                    # 后替换 scnBase._instance。只在该明确过渡窗口内重读。
                    if not finished or exit_requested_at is None or now - exit_requested_at >= 5:
                        raise
                    keys.close()
                    if exit_read_error is None:
                        exit_read_error = str(error)
                        emit("scene_transition_wait", reason=exit_read_error)
                    next_read = now + .05
                    time.sleep(.002)
                    continue
                now = time.perf_counter()
                next_read = now + .012
                if observed["scene_type"] in ("None", "scene_changed") and finished and exit_requested_at is not None:
                    keys.close()
                    if now - exit_requested_at >= 5:
                        raise RuntimeError("退出后五秒内未能确认新场景")
                    next_read = now + .05
                    time.sleep(.002)
                    continue
                latest = observed
                if latest["scene_type"] != SCENE:
                    status = "campaign_complete" if finished else "scene_changed"
                    reason = "十二轮已完成并退出小游戏" if finished else "场景已离开小游戏"
                    break
                finished = finished or latest["campaign_complete"]
                state = (latest["screen"], latest["level_index"], latest["game_state"], latest["showing_score"], latest["dialogue_playing"])
                if state != previous_state:
                    emit("state", snapshot=latest)
                    previous_state = state
                if now >= next_report:
                    emit("progress", snapshot=latest)
                    next_report = now + 1
                if latest["infinite"] or latest["screen"] in ("delete_data", "language", "extra"):
                    status, reason = "blocked", "当前不在十二轮普通举重流程"
                    break
                if latest["paused"]:
                    status, reason = "blocked", "小游戏已暂停，停止接管"
                    break
                if latest["showing_score"]:
                    index = latest["level_index"]
                    score = latest["levels"][index]
                    key = (index, score["score"])
                    if key not in completed_keys:
                        completed_keys.add(key)
                        rounds.append(dict(score))
                        emit("round_complete", level=index + 1, score=score)
                if finished and not exit_when_complete:
                    status, reason = "campaign_complete", "十二轮正常通关成绩已写入游戏状态"
                    break
                blocked = (not latest["can_receive_input"] or latest["transition_playing"] or latest["dialogue_playing"])
                if blocked:
                    quiet_until = now + 1.15
                failed_now = latest["failure"] != 0 or latest["game_state"] == 2
                if failed_now and not failure_seen:
                    failures += 1
                    emit("round_failed", level=latest["level_index"] + 1, failure=latest["failure"], failures=failures)
                failure_seen = failed_now
                if failures >= 4:
                    status, reason = "blocked", "小游戏连续失败，停止以供检查"
                    break
                playing = (latest["screen"] == "gameplay" and latest["game_state"] == 0 and not latest["showing_score"]
                           and not blocked and not failed_now and not finished and now >= quiet_until)
                current_attempt = (latest["scene_pointer"], latest["level_index"], latest["p1"]["hit_list"], latest["p2"]["hit_list"])
                if not playing:
                    if schedule is not None:
                        keys.close()
                    schedule, attempt = None, None
                elif schedule is None or attempt != current_attempt:
                    keys.close()
                    schedule = RatioSchedule([latest["p1"]["weight"], latest["p2"]["weight"]], now, base_hz)
                    attempt = current_attempt
                    initial_hits = [latest[p]["hit_count"] for p in ("p1", "p2")]
                    emit("schedule_started", level=latest["level_index"] + 1,
                         weights=[latest["p1"]["weight"], latest["p2"]["weight"]], periods=schedule.periods)
                if schedule is not None:
                    for i, player in enumerate(("p1", "p2")):
                        if schedule.counts[i] >= 4 and latest[player]["hit_count"] <= initial_hits[i]:
                            raise RuntimeError(f"{player} 按键未被小游戏接受，停止输入")

            if latest is None:
                continue
            if exit_requested_at is not None:
                # 已请求退出后只观察新场景，禁止依据旧 Intro 快照继续发键。
                if now - exit_requested_at >= 5:
                    status, reason = "blocked", "正常退出请求后五秒内未能确认新场景"
                    break
                time.sleep(.002)
                continue
            if schedule is not None:
                due = schedule.due(now)
                if any(name in keys.held for name in due):
                    raise RuntimeError("下一次举重时前次按键仍未释放")
                keys.press(due, now)
            elif now >= next_ui and latest["can_receive_input"] and not latest["transition_playing"]:
                screen = latest["screen"]
                names, duration, action = [], .08, ""
                if screen == "gameplay" and latest["dialogue_playing"]:
                    names, action = ["escape"], "dismiss_dialogue"
                elif screen == "intro":
                    names, duration, action = (["escape"], .08, "exit") if finished else (["p1", "p2"], .65, "hold_to_start")
                elif screen == "level_select":
                    if finished:
                        names, action = ["escape"], "exit"
                    elif latest["extra_slot"] >= 0:
                        raise RuntimeError("当前位于额外模式入口，未执行普通关卡输入")
                    else:
                        target = min(latest["last_unlocked"], 11)
                        direction = "left" if latest["level_index"] > target else "right"
                        names, action = ([direction], "select_next_level") if latest["level_index"] != target else (["p1"], "start_level")
                elif screen == "gameplay" and latest["showing_score"] and now >= quiet_until:
                    names, action = ["p1"], "continue_score"
                elif screen == "ending" and finished:
                    # Ending 自身以 endingDownArrow.activeSelf 为闸门；动画中按键被忽略。
                    names, action = ["p1"], "continue_ending"
                if names:
                    keys.press(names, now, duration)
                    if action == "exit" and exit_requested_at is None:
                        exit_requested_at = now
                    next_ui = now + max(.85, duration + .25)
                    emit("ui_input", action=action, keys=names)
            time.sleep(.002)
        else:
            status, reason = "timed_out", "小游戏运行超过限定时间"
    except Exception as error:
        status, reason = "blocked", str(error)
    finally:
        if keys is not None:
            try:
                keys.close()
            except Exception as error:
                status, reason = "blocked", "释放按键失败：" + str(error)
        if reader is not None:
            reader.close()
        if timer_started:
            timer.timeEndPeriod(1)
        if logfile:
            logfile.close()
    return {"status": status, "reason": reason, "level": "RhythmWeightlifter", "elapsed": time.perf_counter() - started,
            "campaign_complete": finished, "rounds": rounds, "snapshot": latest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--play", action="store_true", help="发送正常 J/F 键，完成当前存档剩余轮次")
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--no-exit", action="store_true")
    parser.add_argument("--log", type=Path, default=ROOT / "weightlifter-session.jsonl")
    args = parser.parse_args()
    api = load_module("rd_wl_api", "rhythm-doctor-live-autoplay.py")
    runtime = load_module("rd_wl_runtime", "rhythm-doctor-runtime.py")
    pid = args.pid or runtime.game_pid()
    if not pid:
        raise RuntimeError("未找到游戏进程")
    binding = runtime.resolve_layout(api, pid, lambda text: None)
    if args.play:
        result = run_campaign(api, pid, binding, max_seconds=args.seconds, log_path=args.log,
                              exit_when_complete=not args.no_exit)
    else:
        reader = WeightlifterReader(api, pid, binding)
        try:
            result = reader.snapshot()
        finally:
            reader.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
