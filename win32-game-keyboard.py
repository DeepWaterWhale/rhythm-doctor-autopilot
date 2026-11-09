"""通过 SendInput 向已验证的游戏前台窗口发送按键。"""
import ctypes as C
from ctypes import wintypes as W
import time

U = C.WinDLL("user32", use_last_error=True)
K = C.WinDLL("kernel32", use_last_error=True)

class KI(C.Structure):
    _fields_ = [("vk", W.WORD), ("scan", W.WORD), ("flags", W.DWORD), ("time", W.DWORD), ("extra", C.c_size_t)]
class MI(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("data", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD), ("extra", C.c_size_t)]
class HI(C.Structure):
    _fields_ = [("message", W.DWORD), ("low", W.WORD), ("high", W.WORD)]
class IU(C.Union):
    _fields_ = [("keyboard", KI), ("mouse", MI), ("hardware", HI)]
class INPUT(C.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", IU)]

U.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
U.SendInput.restype = W.UINT
U.GetForegroundWindow.restype = W.HWND
U.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
U.GetWindowThreadProcessId.restype = W.DWORD
U.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
U.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
U.IsWindowVisible.argtypes = [W.HWND]
U.IsWindowVisible.restype = W.BOOL
U.SetForegroundWindow.argtypes = [W.HWND]
U.SetForegroundWindow.restype = W.BOOL
U.BringWindowToTop.argtypes = [W.HWND]
U.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
U.AttachThreadInput.restype = W.BOOL
U.GetAsyncKeyState.argtypes = [C.c_int]
U.GetAsyncKeyState.restype = C.c_short
ENUM = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
U.EnumWindows.argtypes = [ENUM, W.LPARAM]
K.GetCurrentThreadId.restype = W.DWORD

class Keyboard:
    def __init__(self, pid):
        self.pid = pid
        self.held = False
        self.navigation_held = set()
        candidates = []
        @ENUM
        def visit(hwnd, _):
            owner = W.DWORD()
            U.GetWindowThreadProcessId(hwnd, C.byref(owner))
            if owner.value == pid and U.IsWindowVisible(hwnd):
                title = C.create_unicode_buffer(256)
                U.GetWindowTextW(hwnd, title, len(title))
                if title.value == "Rhythm Doctor":
                    candidates.append(hwnd)
            return True
        U.EnumWindows(visit, 0)
        if len(candidates) > 1:
            foreground = U.GetForegroundWindow()
            if foreground in candidates:
                candidates = [foreground]
            else:
                unity_windows = []
                for hwnd in candidates:
                    class_name = C.create_unicode_buffer(256)
                    U.GetClassNameW(hwnd, class_name, len(class_name))
                    if class_name.value == "UnityWndClass":
                        unity_windows.append(hwnd)
                candidates = unity_windows
        if len(candidates) != 1 or C.sizeof(INPUT) != 40:
            raise RuntimeError("目标窗口数量或 INPUT 布局不符合预期")
        self.hwnd = candidates[0]

    def is_foreground(self):
        owner = W.DWORD()
        U.GetWindowThreadProcessId(U.GetForegroundWindow(), C.byref(owner))
        return owner.value == self.pid

    def activate(self):
        U.SetForegroundWindow(self.hwnd)
        time.sleep(0.1)
        if not self.is_foreground():
            tid = U.GetWindowThreadProcessId(U.GetForegroundWindow(), None)
            mine = K.GetCurrentThreadId()
            attached = U.AttachThreadInput(mine, tid, True)
            try:
                U.BringWindowToTop(self.hwnd)
                U.SetForegroundWindow(self.hwnd)
            finally:
                if attached:
                    U.AttachThreadInput(mine, tid, False)
            time.sleep(0.1)
        if not self.is_foreground():
            raise RuntimeError("游戏窗口未获得前台焦点")
        if U.GetAsyncKeyState(0x20) & 0x8000:
            raise RuntimeError("用户当前按住空格，未接管输入")

    def set_space(self, down):
        if self.held == down:
            return
        if down and not self.is_foreground():
            raise RuntimeError("游戏失去前台焦点，停止输入")
        event = INPUT(type=1, keyboard=KI(vk=0, scan=0x39, flags=0x8 if down else 0xA))
        if U.SendInput(1, C.byref(event), C.sizeof(INPUT)) != 1:
            raise C.WinError(C.get_last_error())
        self.held = down

    def tap_start(self):
        self.set_space(True)
        try:
            time.sleep(0.08)
        finally:
            self.set_space(False)

    def close(self):
        self.set_space(False)
        for key in list(self.navigation_held):
            self.set_key(key, False)

    def set_key(self, name, down):
        if name == "space":
            self.set_space(down)
            return
        if (name in self.navigation_held) == down:
            return
        if down and (self.held or not self.is_foreground()):
            raise RuntimeError("目标不在前台或仍持有游玩按键，取消导航")
        scan, extended = {"left": (0x4B, 1), "right": (0x4D, 1),
                          "up": (0x48, 1), "down": (0x50, 1), "escape": (0x01, 0),
                          "skip": (0x1F, 0), "quit": (0x10, 0)}[name]
        item = INPUT(type=1, keyboard=KI(vk=0, scan=scan, flags=0x8 | extended | (0 if down else 2)))
        if U.SendInput(1, C.byref(item), C.sizeof(INPUT)) != 1:
            raise C.WinError(C.get_last_error())
        self.navigation_held.add(name) if down else self.navigation_held.discard(name)

    def tap_key(self, name):
        if name == "space":
            self.tap_start()
            return
        if self.held or not self.is_foreground():
            raise RuntimeError("目标不在前台或仍持有游玩按键，取消导航")
        self.set_key(name, True)
        try:
            time.sleep(.08)
        finally:
            self.set_key(name, False)
