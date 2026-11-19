"""Windows 全局停止热键；独立消息线程，不依赖 GUI 焦点。"""

import ctypes
from ctypes import wintypes
import threading


CTRL_WARNING = "不支持 Ctrl 组合键：Ctrl 与程序自动输入的 W 组合会关闭游戏窗口。"
MODIFIERS = {"Alt": 0x0001, "Shift": 0x0004}
KEYS = {**{f"F{i}": 0x6F + i for i in range(1, 25) if i != 12},
        **{char: ord(char) for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
        "Space": 0x20, "Pause": 0x13, "Insert": 0x2D, "Delete": 0x2E,
        "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
        "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28}


def parse_hotkey(text):
    if not isinstance(text, str):
        raise ValueError("快捷键格式无效")
    parts = text.split("+")
    if "Ctrl" in parts[:-1]:
        raise ValueError(CTRL_WARNING)
    if (parts[-1] not in KEYS or any(part not in MODIFIERS for part in parts[:-1])
            or len(set(parts[:-1])) != len(parts[:-1])):
        raise ValueError("请使用功能键或 Alt、Shift 与字母、数字等按键的组合（不支持 Ctrl 和 F12）")
    if len(parts) == 1 and parts[-1] in "WASDJ":
        raise ValueError("W、A、S、D、J 用于自动操作，请添加修饰键或选择其他按键")
    modifiers = sum(MODIFIERS[part] for part in parts[:-1])
    return modifiers, KEYS[parts[-1]]


class StopHotkey:
    def __init__(self):
        self.triggered = threading.Event()
        self.error = None
        self._ready = threading.Event()
        self._thread = None
        self._thread_id = None

    def start(self, text):
        modifiers, key = parse_hotkey(text)
        self._thread = threading.Thread(target=self._listen, args=(modifiers, key), daemon=True)
        self._thread.start()
        self._ready.wait()
        if self.error:
            self.close()
            raise OSError(self.error)

    def _listen(self, modifiers, key):
        registered = False
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self._user32.RegisterHotKey.restype = wintypes.BOOL
            self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            self._user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                               wintypes.UINT, wintypes.UINT]
            self._user32.GetMessageW.restype = ctypes.c_int
            self._user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            message = wintypes.MSG()
            # 先创建线程消息队列，确保关闭时 PostThreadMessage 可用。
            self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            if not self._user32.RegisterHotKey(None, 1, modifiers | 0x4000, key):
                code = ctypes.get_last_error()
                if code == 1409:
                    raise OSError("该停止快捷键已被其他程序占用，请更换后重试。")
                raise ctypes.WinError(code)
            registered = True
            self._ready.set()
            while True:
                status = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status == 0:
                    break
                if status == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if message.message == 0x0312 and message.wParam == 1:  # WM_HOTKEY
                    self.triggered.set()
        except Exception as exc:
            self.error = str(exc)
            self.triggered.set()
        finally:
            if registered:
                self._user32.UnregisterHotKey(None, 1)
            self._ready.set()

    def close(self):
        if self._thread is not None and self._thread.is_alive():
            self._user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            self._thread.join()
