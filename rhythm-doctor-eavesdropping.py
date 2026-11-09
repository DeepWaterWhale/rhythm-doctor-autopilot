"""AthleteTherapy 窃听成就：只读状态、等待完整对白，再用普通空格完成练习。

导入和 CLI 默认只读；只有宿主显式调用 run() 才发送按键。
run(api, pid, binding, stop_requested, event_sink, start=True) 的 binding
必须包含经 PID 与进程创建时间验证的 identity/classes。宿主负责准备界面三秒门控。
"""
import importlib.util
import json
import math
from pathlib import Path
import struct
import time

ROOT = Path(__file__).resolve().parent
LEVEL = "AthleteTherapy"
STORY_PHASES = {1: 1, 3: 2, 5: 3}
FREEFORM_KNOTS = {f"Phase{number}Freeform1" for number in (1, 2, 3)}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decide(snapshot):
    """无副作用。allow_new_press 不控制已经接住的长按；已有长按必须准时松开。"""
    def result(action, reason, **extra):
        return {"action": action, "allow_new_press": action == "play",
                "advance_dialogue": action == "dialogue", "reason": reason, **extra}

    if snapshot.get("scene_type") != "scnGame" or snapshot.get("level") != LEVEL:
        return result("unrelated", "等待进入 5-3 AthleteTherapy")
    if snapshot.get("two_player") or snapshot.get("paused") or snapshot.get("failed"):
        return result("blocked", "窃听流程需要单人、未暂停且未失败的关卡")
    if not snapshot.get("loaded"):
        return result("wait", "等待关卡加载")
    progress = snapshot["eavesdropping_progress"]
    if not 0 <= progress <= 3:
        return result("blocked", "窃听进度不在 0..3 范围内")
    if snapshot.get("rank_stage", 0) >= 2 or snapshot.get("game_state") == 6:
        return result("finished", "已进入原生结算，仍需核实 Steam 成就", progress=progress)
    phase = snapshot["phase_index"]
    required = STORY_PHASES.get(phase)
    completed_before = phase // 2 if phase in range(6) else 0
    if progress < completed_before:
        return result("blocked", "上一段对白未完成便已换阶段，需要重开本关", progress=progress)
    dialogue = snapshot.get("therapy_dialogue", {})
    knot = dialogue.get("knot", "")
    if dialogue.get("playing") and knot in FREEFORM_KNOTS:
        return result("wait", "等待完整窃听对白 · " + knot, progress=progress)
    if required and progress < required and snapshot["reps"] >= 5:
        return result("wait", f"已做五次练习，等待第 {required} 段对白结束", progress=progress)
    if dialogue.get("playing") and dialogue.get("active"):
        if dialogue.get("choices", 0) > 1:
            return result("blocked", "当前对白包含多个选项")
        if not dialogue.get("visible"):
            return result("wait", "等待引导对白显示")
        return result("dialogue", "推进引导对白 · " + knot)
    if snapshot.get("game_state") == 0:
        return result("ready", "等待宿主准备界面开始门控")
    if snapshot.get("ignore_input"):
        return result("wait", "等待游戏重新接受输入")
    if phase not in range(6):
        return result("wait", "等待练习阶段切换")
    if snapshot["reps"] >= snapshot["rep_goal"]:
        return result("wait", "本阶段练习已足够，等待原生结束计时器")
    if snapshot.get("bar", 0) >= 24:
        return result("wait", "已到成就判定处，等待原生结算")
    return result("play", f"练习阶段 {phase} · {snapshot['reps']}/{snapshot['rep_goal']}")


class EavesdroppingReader:
    def __init__(self, api, pid, binding):
        if "identity" not in binding or "classes" not in binding:
            raise ValueError("需要包含 identity/classes 的已验证 binding")
        self.api = api
        self.binding = binding
        self.runtime = _load("rd_eaves_runtime", "rhythm-doctor-runtime.py")
        navigation = _load("rd_eaves_navigation", "rhythm-doctor-navigation.py")
        self.scene = navigation.SceneReader(api, pid, binding["classes"])
        try:
            if self.runtime.process_identity(self.scene.reader) != binding["identity"]:
                raise RuntimeError("窃听模块的进程身份与地址缓存不匹配")
        except Exception:
            self.close()
            raise

    def close(self):
        self.scene.close()

    def snapshot(self):
        s = self.scene
        result = s.snapshot()
        if result["scene_type"] != "scnGame" or result.get("level") != LEVEL or not result.get("loaded"):
            return result
        obj = result["scene_pointer"]
        level = s.get(obj, "currentLevel")
        cls = s.cls(level)
        meta = s.meta(cls)
        if meta["name"] != "Level_AthleteTherapy":
            raise RuntimeError("当前关卡类不是 Level_AthleteTherapy")
        fields = s.fields(cls)
        raw = s.reader.read(level, meta["instance_size"])

        def value(name, fmt):
            return struct.unpack_from("<" + fmt, raw, fields[name][0]["offset"])[0]

        result.update(level_pointer=level, phase_index=value("i0", "i"), reps=value("i1", "i"),
                      rep_goal=value("i2", "i"), real_phase=bool(value("b0", "B")),
                      spawning_beats=bool(value("b1", "B")), long_holds=bool(value("b2", "B")),
                      eavesdropping_progress=value("eavesdroppingProgress", "i"),
                      timer_destination=value("timerDestination", "f"),
                      timer_tag=s.get(level, "tagToRunAfterTimer") or "",
                      ignore_input=s.get(obj, "ignoreInput"))
        conductor = s.get(None, "_instance", self.binding["classes"]["scrConductor"])
        result["bar"] = s.get(conductor, "barNumber")
        result["play_style"] = s.get(conductor, "playStyle")
        manager = s.get(None, "instance", self.binding["classes"]["scrGameManager"])
        ink = s.get(manager, "inkDialogue")
        result["therapy_dialogue"] = {
            "playing": bool(ink and s.get(ink, "isPlaying")),
            "active": bool(ink and s.get(ink, "activeDialoguePresent")),
            "visible": bool(ink and s.get(ink, "visible")),
            "knot": s.get(ink, "currentKnot") if ink else "",
            "file": s.get(ink, "file") if ink else "",
            "type": s.get(ink, "type") if ink else None,
            "choices": result["dialogue"].get("choices", 0),
        }
        if s.reader.u64(s.slot) != obj or s.get(obj, "currentLevel") != level:
            raise RuntimeError("读取窃听状态时场景已经改变")
        return result


def _driven_hold(game, state, dsp):
    """允许过时但未按下的玩家驱动拍；只限本关 P1 row 0 的长按。"""
    records, _ = game.beats(state["beats"])
    for record in records:
        if record["row"] != 0 or not record["hold"]:
            continue
        pointer = int(record["pointer"], 16)
        if not game.reader.read(pointer + game.bf["playerDrives7thBeat"], 1)[0]:
            continue
        driven = struct.unpack("<d", game.reader.read(pointer + game.bf["playerDriven7thBeatHitTime"], 8))[0]
        if driven != 0 or dsp < record["target"]:
            continue
        duration = record["release"] - record["target"]
        if not math.isfinite(duration) or not .1 <= duration <= 10:
            raise RuntimeError("玩家驱动长按的持续时间异常")
        return record
    return None


def run(api, pid, binding, stop_requested=None, event_sink=None, *, start=False,
        max_seconds=1800, run_name=None):
    """由宿主显式调用的独占 runner；宿主不得同时运行其他输入线程。

    start=True 表示宿主已经完成准备画面 3 秒门控。本函数只在实时状态仍为
    ready 时按一次空格；输入状态已运行则接管。返回常驻 app 可识别的结果字段。
    progress==3 仅证明三段对白结束；最终 Steam 状态需由宿主另行验证。
    """
    stop_requested = stop_requested or (lambda: False)
    result = {"level": LEVEL, "status": "running", "actions": 0, "holds_accepted": 0,
              "holds_released_perfect": 0, "holds_release_unverified": 0,
              "eavesdropping_progress": 0, "grant_point_reached": False, "metrics": None}
    if run_name is None:
        run_name = "eavesdropping-play-" + time.strftime("%Y%m%d-%H%M%S")
    if Path(run_name).name != run_name:
        raise ValueError("run_name 必须是不含路径的文件名")
    log_path = ROOT / (run_name + ".jsonl")
    reader = game = keyboard = None
    begin = time.monotonic()
    log = log_path.open("a", encoding="utf-8")
    sink_failed = False

    def emit(kind, **values):
        nonlocal sink_failed
        event = {"event": kind, "elapsed": time.monotonic() - begin, **values}
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
        log.flush()
        if event_sink and not sink_failed:
            try:
                event_sink(event)
            except Exception as error:
                # 日志通知失败不能阻止释放输入或关闭内存句柄。
                sink_failed = True
                result["event_sink_error"] = str(error)

    try:
        reader = EavesdroppingReader(api, pid, binding)
        game = api.Game(pid, binding["classes"])
        if game.level != LEVEL:
            raise RuntimeError("窃听 runner 只允许在 AthleteTherapy 运行")
        keyboard = api.keyboard_mod.Keyboard(pid)
        if not keyboard.is_foreground():
            result.update(status="focus_lost", interruption="focus_lost", error="游戏不在前台")
            return result
        if api.keyboard_mod.U.GetAsyncKeyState(0x20) & 0x8000 or game.active_holds(game.state()):
            raise RuntimeError("已有空格或长按输入，未接管")
        if stop_requested():
            result["status"] = "cancelled"
            return result
        if start:
            observation = reader.runtime.observe_scene(api, game)
            if observation["phase"] == "ready":
                keyboard.tap_start()
                emit("start_pressed")
            elif observation["phase"] != "running":
                raise RuntimeError("准备状态已改变：" + observation["reason"])
        emit("started", level=LEVEL, playing=True, entry="start" if start else "attach")
        next_poll = next_dialogue = next_press = 0.0
        last_signature = None
        current_decision = {"action": "wait"}
        pending_hold = None
        accepted = None
        released = None
        while time.monotonic() - begin < max_seconds:
            now = time.monotonic()
            if stop_requested():
                result["status"] = "cancelled"
                break
            if game.scene_changed():
                result["status"] = "scene_changed"
                break
            if not keyboard.is_foreground():
                result.update(status="focus_lost", interruption="focus_lost")
                break
            state = game.state()
            if state["paused"]:
                result["status"] = "paused"
                break
            if state["failed"]:
                result["status"] = "level_failed"
                break
            dsp, frame = game.clock()
            holds = game.active_holds(state)
            if pending_hold:
                matching = [item for item in holds if item["pointer"] == pending_hold["pointer"]]
                if matching:
                    accepted = matching[0]
                    pending_hold = None
                    result["holds_accepted"] += 1
                    emit("hold_accepted", record=accepted)
                elif now - pending_hold["wall"] > .5:
                    raise RuntimeError("玩家驱动长按未被接住")
            if accepted and keyboard.held:
                matching = [item for item in holds if item["pointer"] == accepted["pointer"]]
                if not matching:
                    raise RuntimeError("仍持有空格时长按对象提前消失")
                accepted = matching[0]
                if dsp >= accepted["release"] - .010:
                    released = {"pointer": accepted["pointer"], "frame": frame, "wall": now,
                                "history": game.row_history(state, accepted["row"]), "row": accepted["row"]}
                    keyboard.set_space(False)
                    emit("hold_released", record=accepted, dsp=dsp)
                    accepted = None
                    next_press = now + .12
                time.sleep(.001)
                continue
            if released:
                if not any(item["pointer"] == released["pointer"] for item in holds) and frame != released["frame"]:
                    after = game.row_history(state, released["row"])
                    before = released["history"]
                    perfect = (game.hold_release_result(state) == 0 and before is not None and after is not None
                               and after["count"] > before["count"] and after["last"] == 0)
                    result["holds_released_perfect" if perfect else "holds_release_unverified"] += 1
                    emit("hold_release_checked", perfect=perfect, history=after)
                    released = None
                    next_poll = 0
                elif now - released["wall"] > .5:
                    raise RuntimeError("松开后长按对象没有释放")
                else:
                    time.sleep(.001)
                    continue
            if pending_hold:
                time.sleep(.001)
                continue
            if now >= next_poll:
                snapshot = reader.snapshot()
                current_decision = decide(snapshot)
                result["eavesdropping_progress"] = snapshot.get("eavesdropping_progress", result["eavesdropping_progress"])
                if snapshot.get("bar", 0) >= 24 and result["eavesdropping_progress"] == 3:
                    result["grant_point_reached"] = True
                signature = (current_decision["action"], snapshot.get("phase_index"), snapshot.get("reps"),
                             result["eavesdropping_progress"], snapshot.get("therapy_dialogue", {}).get("knot"))
                if signature != last_signature:
                    emit("eavesdropping_state", decision=current_decision, snapshot=snapshot)
                    last_signature = signature
                next_poll = now + 1.0
            action = current_decision["action"]
            if action == "blocked":
                result.update(status="blocked", error=current_decision["reason"])
                break
            if action in ("unrelated", "finished"):
                result["status"] = "rank_shown" if action == "finished" else "scene_changed"
                break
            if action == "dialogue" and now >= next_dialogue:
                fresh = decide(reader.snapshot())
                if fresh["advance_dialogue"] and not stop_requested():
                    keyboard.tap_key("space")
                    emit("dialogue_advanced", reason=fresh["reason"])
                    next_dialogue = now + 1
                    next_poll = 0
            elif action == "play" and now >= next_press and not state["ignore_input"]:
                candidate = _driven_hold(game, state, dsp)
                if candidate:
                    fresh = decide(reader.snapshot())
                    if fresh["allow_new_press"] and not stop_requested():
                        if api.keyboard_mod.U.GetAsyncKeyState(0x20) & 0x8000:
                            raise RuntimeError("外部空格输入仍按住，停止接管")
                        keyboard.set_space(True)
                        result["actions"] += 1
                        pending_hold = {"pointer": candidate["pointer"], "wall": now}
                        emit("hold_pressed", record=candidate, dsp=dsp)
                    else:
                        current_decision = fresh
            time.sleep(.005 if action == "play" else max(.001, min(1.0, next_poll - time.monotonic())))
        else:
            result["status"] = "timeout"
    except Exception as error:
        result.update(status="error", error=str(error))
        emit("error", error=str(error))
    finally:
        cleanup_errors = []
        if keyboard:
            try:
                keyboard.close()
            except Exception as error:
                cleanup_errors.append("keyboard: " + str(error))
                result.update(status="error", error="释放输入失败：" + str(error))
                result.pop("interruption", None)
        if game:
            try:
                if not game.scene_changed():
                    result["metrics"] = game.metrics(game.state())
            except Exception as error:
                result["metrics_error"] = str(error)
            finally:
                try:
                    game.reader.close()
                except Exception as error:
                    cleanup_errors.append("game_reader: " + str(error))
        if reader:
            try:
                reader.close()
            except Exception as error:
                cleanup_errors.append("scene_reader: " + str(error))
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
        result["elapsed_seconds"] = time.monotonic() - begin
        result["file"] = str(ROOT / (run_name + "-result.json"))
        try:
            emit("finished", **result)
            (ROOT / (run_name + "-result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            log.close()
    return result


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    api = _load("rd_eaves_api", "rhythm-doctor-live-autoplay.py")
    binding = json.loads((ROOT / "app-runtime-layout.json").read_text(encoding="utf-8"))
    observer = EavesdroppingReader(api, binding["identity"][0], binding)
    try:
        snapshot = observer.snapshot()
        print(json.dumps({"snapshot": snapshot, "decision": decide(snapshot), "sent_input": False}, ensure_ascii=False))
    finally:
        observer.close()
