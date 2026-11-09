"""按进程生命周期缓存 Mono 类型地址；仅使用只读进程权限。"""
import ctypes as C
from ctypes import wintypes as W
import json
import importlib.util
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "app-runtime-layout.json"
K = C.WinDLL("kernel32", use_last_error=True)


class ProcessEntry(C.Structure):
    _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                ("exe", W.WCHAR * 260)]


K.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
K.CreateToolhelp32Snapshot.restype = W.HANDLE
K.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(ProcessEntry)]
K.Process32NextW.argtypes = [W.HANDLE, C.POINTER(ProcessEntry)]
K.CloseHandle.argtypes = [W.HANDLE]
K.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
K.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
K.CreateMutexW.restype = W.HANDLE


def game_pid():
    handle = K.CreateToolhelp32Snapshot(2, 0)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    entry = ProcessEntry(size=C.sizeof(ProcessEntry))
    found = []
    try:
        more = K.Process32FirstW(handle, C.byref(entry))
        while more:
            if entry.exe.casefold() == "rhythm doctor.exe":
                found.append(entry.pid)
            more = K.Process32NextW(handle, C.byref(entry))
    finally:
        K.CloseHandle(handle)
    if len(found) > 1:
        raise RuntimeError("检测到多个游戏进程，请保留一个")
    return found[0] if found else None


def process_identity(reader):
    times = [W.FILETIME() for _ in range(4)]
    if not K.GetProcessTimes(reader.handle, *(C.byref(t) for t in times)):
        raise C.WinError(C.get_last_error())
    return [reader.pid, (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime]


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def resolve_layout(api, pid, notify, force_scan=False):
    reader = api.sample.ins.mem.Reader(pid)
    try:
        identity = process_identity(reader)
        def verify(name, address):
            meta = api.objmod.metadata(reader, address)
            if meta["name"] != name or meta["namespace"] != "":
                raise ValueError("类型身份不匹配：" + name)
            if name == "scnGame" and meta["token"] != "0x20001c9":
                raise ValueError("当前游戏版本的类型标识不匹配")
            return meta

        if CACHE.exists() and not force_scan:
            try:
                saved = json.loads(CACHE.read_text(encoding="utf-8"))
                required = {"scnGame", "scrConductor", "RDCalibration", "GC", "scrGameManager", "Beat", "Row", "MistakesManager", "Rankscreen"}
                if saved.get("identity") == identity and required <= saved["classes"].keys():
                    for name, address in saved["classes"].items():
                        verify(name, address)
                    return saved
            except (OSError, ValueError, RuntimeError, IndexError, KeyError):
                pass

        # 旧实验地址只作为启动提示，使用前仍在当前进程逐一验证。
        hints = {"scnGame": 0x265F6EBFD00, "scrConductor": 0x265F6EBFAC0,
                 "RDCalibration": 0x265F6F039D0, "GC": 0x265F71FA540}
        classes = {}
        if pid == 27896 and not force_scan:
            for name, address in hints.items():
                try:
                    verify(name, address)
                    classes[name] = address
                except (OSError, ValueError, RuntimeError, IndexError):
                    pass
        missing = [name for name in ("scnGame",) if name not in classes]
        if missing:
            notify("正在定位游戏内存（每次启动游戏只需一次）")
            names = reader.scan([name.encode() + b"\0" for name in missing])
            if names.get("truncated"):
                raise RuntimeError("类型名称扫描超过上限")
            refs = reader.scan([struct.pack("<Q", hit["address"]) for hit in names["hits"]], alignment=8)
            if refs.get("truncated"):
                raise RuntimeError("类型引用扫描超过上限")
            candidates = {name: {} for name in missing}
            for ref in refs["hits"]:
                address = ref["address"] - 0x48
                try:
                    name = reader.cstring(reader.u64(address + 0x48))
                    if name not in candidates or reader.u32(address + 0x58) >> 24 != 2:
                        continue
                    meta = verify(name, address)
                    if name == "GC" and not any(f["name"] == "twoPlayerMode" for f in meta["fields"]):
                        continue
                    candidates[name][address] = meta
                except (OSError, ValueError, RuntimeError, IndexError):
                    continue
            for name, found in candidates.items():
                if len(found) != 1:
                    raise RuntimeError(f"尚未唯一定位 {name}，等待游戏加载后重试")
                classes[name] = next(iter(found))

        missing = [name for name in ("scrConductor", "RDCalibration", "GC", "scrGameManager") if name not in classes]
        if missing:
            notify("正在读取游戏类型布局")
            spec = importlib.util.spec_from_file_location("rd_cli_names", ROOT / "managed-type-name-offsets.py")
            parser = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(parser)
            assembly = Path(reader.path).with_name("Rhythm Doctor_Data") / "Managed" / "Assembly-CSharp.dll"
            offsets = parser.read_type_name_offsets(assembly, missing)["types"]
            anchor_name = reader.u64(classes["scnGame"] + 0x48)
            image = reader.u64(classes["scnGame"] + 0x40)
            wanted = {}
            for name in missing:
                address = anchor_name + offsets[name]["delta_from_anchor"]
                if reader.cstring(address) != name:
                    raise ValueError("程序集名称表与运行时不匹配")
                wanted[address] = name
            refs = reader.scan([struct.pack("<Q", address) for address in wanted], alignment=8)
            if refs.get("truncated"):
                raise RuntimeError("类型指针扫描超过上限")
            found = {name: set() for name in missing}
            for ref in refs["hits"]:
                address = ref["address"] - 0x48
                try:
                    name = wanted.get(reader.u64(address + 0x48))
                    if not name or reader.u64(address + 0x40) != image:
                        continue
                    meta = verify(name, address)
                    if int(meta["token"], 16) == int(offsets[name]["token"], 16):
                        found[name].add(address)
                except (OSError, ValueError, RuntimeError, IndexError):
                    continue
            for name, candidates in found.items():
                if len(candidates) != 1:
                    raise RuntimeError(f"尚未唯一定位 {name}，等待游戏加载后重试")
                classes[name] = next(iter(candidates))

        meta = verify("scnGame", classes["scnGame"])
        linked = {f["name"]: f for f in meta["fields"]}
        for name, field in {"Beat": "playerCaughtHoldBeat", "Row": "rows",
                            "MistakesManager": "mistakesManager", "Rankscreen": "rankscreen"}.items():
            address = int(linked[field]["type_data"], 16)
            verify(name, address)
            classes[name] = address
        saved = {"identity": identity, "classes": classes}
        atomic_json(CACHE, saved)
        return saved
    finally:
        reader.close()


def observe_scene(api, game):
    """读取准备、运行和结算状态；是否允许接管由会话门控另行决定。"""
    state = game.state()
    level = state["level_object"]
    observed = {"scene": (game.instance, level, game.level) if level else None,
                "game_state": state["state"], "level_type": None, "rank_stage": None,
                "phase": "waiting", "reason": "等待关卡加载"}
    def done(phase, reason):
        observed.update(phase=phase, reason=reason)
        return observed
    if not level:
        return observed
    lf = game.object_fields(level)
    level_type = game.reader.i32(level + lf["levelType"])
    observed["level_type"] = level_type
    if level_type not in (1, 2, 3, 4, 5, 6):
        return done("blocked", "当前为教学或剧情，等待正式关卡")
    checks = [(state["paused"], "游戏已暂停"), (state["failed"], "本局已失败")]
    for blocked, reason in checks:
        if blocked:
            return done("blocked", reason)
    if not game.reader.read(game.instance + game.gf["levelFinishedLoading"], 1)[0]:
        return done("waiting", "等待关卡加载")
    observed["rank_stage"] = game.rank_stage(state)
    if state["state"] == 6 or observed["rank_stage"] >= 2:
        return done("finished", "本关已结算，等待下一关")
    gc_meta = api.objmod.metadata(game.reader, game.layout["GC"])
    gc_field = next(f for f in gc_meta["fields"] if f["name"] == "twoPlayerMode")
    if game.reader.read(api.sample.static_base(game.reader, gc_meta) + gc_field["offset"], 1)[0]:
        return done("blocked", "当前只支持单人模式")
    if state["ignore_input"]:
        return done("waiting", "等待关卡接受输入")
    if game.level == "BeansHopper" and game.beans_start_state(state)["ready"]:
        return done("ready", "跳豆已准备")
    if state["state"] in (2, 3):
        return done("running", "关卡已运行，等待可接管的会话")
    if state["state"] != 0:
        return done("waiting", "等待开始流程完成" if state["state"] == 1 else "等待剧情结束")
    if game.reader.read(level + lf["startImmediately"], 1)[0]:
        return done("waiting", "当前关卡自动开始")
    if game.reader.read(game.instance + game.gf["persistenceStartImmediately"], 1)[0]:
        return done("waiting", "等待场景过渡")
    hand = game.reader.u64(game.instance + game.gf["handControllerOnTop"])
    if not hand or game.reader.read(hand + game.object_fields(hand)["slidingIn"], 1)[0]:
        return done("waiting", "等待开始动画结束")
    return done("ready", "关卡已准备")


def preparation(api, game):
    observed = observe_scene(api, game)
    return observed["scene"], observed["phase"] == "ready", observed["reason"]


class ReadyGate:
    """连续就绪三秒，每轮准备只发起一次开始请求。"""
    def __init__(self, delay=3.0):
        self.delay = delay
        self.identity = None
        self.deadline = None
        self.fired = False

    def observe(self, identity, eligible, now):
        if identity != self.identity:
            self.identity, self.deadline, self.fired = identity, None, False
        if identity is None or not eligible:
            self.deadline = None
            return False, None
        if self.fired:
            return False, None
        if self.deadline is None:
            self.deadline = now + self.delay
        remaining = max(0.0, self.deadline - now)
        if remaining == 0:
            self.fired = True
            return True, 0.0
        return False, remaining


class SessionGate:
    """只接管已观察到的准备关卡，或本程序完成关卡后的新场景。"""
    def __init__(self, delay=3.0):
        self.ready_gate = ReadyGate(delay)
        self.prepared = None
        self.predecessor = None
        self.handled = set()

    def gap(self):
        # 加载期间的读失败不清除已处理记录，避免同一局反复接管。
        self.ready_gate.observe(None, False, 0)
        self.prepared = None

    def suspend(self):
        self.gap()
        self.predecessor = None

    def finished(self, scene, status, retry_ready=False):
        self.prepared = None
        self.predecessor = scene if status in ("rank_shown", "scene_changed") else None
        if retry_ready:
            self.handled.discard(scene)
            self.ready_gate = ReadyGate(self.ready_gate.delay)

    def observe(self, observed, foreground, space_down, now, allow_attach=True, attach_current=False):
        scene, phase = observed["scene"], observed["phase"]
        if not foreground:
            self.suspend()
            return None, None
        if scene != self.prepared:
            self.prepared = None
        if scene is None:
            self.gap()
            return None, None
        if scene in self.handled:
            self.ready_gate.observe(scene, False, now)
            return None, None
        if phase == "ready":
            if not space_down:
                self.prepared = scene
            trigger, remaining = self.ready_gate.observe(scene, not space_down, now)
            if not trigger:
                return None, remaining
            action = "start"
        else:
            self.ready_gate.observe(scene, False, now)
            if phase in ("blocked", "finished"):
                self.prepared = self.predecessor = None
            continuation = self.predecessor is not None and scene != self.predecessor
            if not allow_attach or phase != "running" or not (attach_current or scene == self.prepared or continuation):
                return None, None
            action = "attach"
        self.handled.add(scene)
        self.prepared = self.predecessor = None
        return action, None
