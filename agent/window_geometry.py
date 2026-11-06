"""One geometry policy for opening, recording and playback. No pixel access.

The lease owns only sizing style bits and a process-held file lock. Window
movement stays available; focus, user settings and recording files are untouched.
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import threading
import time

STANDARD_BASE = (1148, 720)
SIZING_STYLE = 0x00040000 | 0x00010000  # WS_THICKFRAME | WS_MAXIMIZEBOX


def validate_base(base):
    if (not isinstance(base, (list, tuple)) or len(base) != 2
            or any(type(v) is not int or not 1 <= v <= 8192 for v in base)
            or min(base) != 720):
        raise ValueError('脚本窗口基准必须为短边 720 的有效尺寸。')
    return tuple(base)


class Win32Geometry:
    def __init__(self):
        from script_studio.capture import _Win32WindowAPI
        self.windows = _Win32WindowAPI()
        self.api = self.windows.user32
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'GetWindowRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            'GetWindowLongW': ([wintypes.HWND, ctypes.c_int], ctypes.c_long),
            'SetWindowLongW': ([wintypes.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long),
            'SetWindowPos': ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint], wintypes.BOOL),
            'IsZoomed': ([wintypes.HWND], wintypes.BOOL),
            'ShowWindow': ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.api, name)
            method.argtypes, method.restype = args, result
        signatures = {
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = args, result

    def inspect(self, hwnd):
        return self.windows.inspect(hwnd)

    def identity(self, hwnd):
        if not self.api.IsWindow(hwnd):
            raise RuntimeError('原窗口已关闭。')
        pid, name = wintypes.DWORD(), ctypes.create_unicode_buffer(256)
        self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        self.api.GetClassNameW(hwnd, name, len(name))
        return dict(pid=int(pid.value), class_name=name.value)

    def claim(self, hwnd, pid):
        # Byte-range locks are process owned and released by the OS on a crash.
        # Unlike a semaphore, a dead worker cannot leave the window claimed.
        import msvcrt
        directory = Path(tempfile.gettempdir()) / 'MAAZMXY4-window-locks'
        directory.mkdir(exist_ok=True)
        stream = (directory / f'{pid}-{hwnd}.lock').open('a+b')
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            raise RuntimeError('此窗口正在录制或播放，请先停止原任务。') from None
        process = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, prevents PID reuse
        if not process:
            stream.close()
            raise RuntimeError('无法核验游戏进程。')
        return stream, process

    def alive(self, claim):
        return self.kernel.WaitForSingleObject(claim[1], 0) == 258

    def release(self, claim):
        claim[0].close()
        self.kernel.CloseHandle(claim[1])

    def style(self, hwnd):
        return self.api.GetWindowLongW(hwnd, -16) & 0xffffffff

    def set_style(self, hwnd, value):
        ctypes.set_last_error(0)
        previous = self.api.SetWindowLongW(hwnd, -16, ctypes.c_long(value).value)
        if previous == 0 and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.api.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020):
            raise ctypes.WinError(ctypes.get_last_error())

    def resize(self, hwnd, base):
        with self.windows._physical_pixels():
            # SW_SHOWNOACTIVATE restores a maximized window without taking focus.
            if self.api.IsZoomed(hwnd):
                self.api.ShowWindow(hwnd, 4)
            current = self.inspect(hwnd)
            outer = wintypes.RECT()
            if not self.api.GetWindowRect(hwnd, ctypes.byref(outer)):
                raise ctypes.WinError(ctypes.get_last_error())
            width = base[0] + outer.right - outer.left - current['width']
            height = base[1] + outer.bottom - outer.top - current['height']
            if not self.api.SetWindowPos(hwnd, None, 0, 0, width, height, 0x0002 | 0x0004 | 0x0010):
                raise ctypes.WinError(ctypes.get_last_error())


class WindowLease:
    """Scoped size lock; verify identity and geometry before every input.

    Ordinary dragging/maximize is disabled. External geometry changes receive a
    bounded correction; a persistent mismatch fails closed. Callers subtract
    correction time from their playback clock so recovery cannot burst inputs.
    """
    def __init__(self, hwnd, base=STANDARD_BASE, *, api=None):
        if type(hwnd) is not int or hwnd <= 0:
            raise ValueError('请明确选择目标游戏窗口。')
        self.hwnd, self.base = hwnd, validate_base(base)
        self.api = api or Win32Geometry()
        self._mutex = threading.RLock()
        self._closed, self._claim, self._style = False, None, None
        self.paused_seconds = 0.0
        self.identity = self.api.inspect(hwnd)
        try:
            self._claim = self.api.claim(hwnd, self.identity['pid'])
            self._style = self.api.style(hwnd) & SIZING_STYLE
            self.api.set_style(hwnd, self.api.style(hwnd) & ~SIZING_STYLE)
            self.ensure()
        except BaseException:
            self.close()
            raise

    def require_identity(self):
        if self._closed or not self._claim or not self.api.alive(self._claim):
            raise RuntimeError('游戏窗口或所属进程已退出。')
        current = (self.api.identity(self.hwnd) if hasattr(self.api, 'identity') else self.api.inspect(self.hwnd))
        if any(current[key] != self.identity[key] for key in ('pid', 'class_name')):
            raise RuntimeError('游戏窗口身份已改变，未向新窗口发送输入。')

    def _same_target(self):
        self.require_identity()
        current = self.api.inspect(self.hwnd)
        return current

    def ensure(self):
        with self._mutex:
            current = self._same_target()
            if ((current['width'], current['height']) == self.base
                    and not self.api.style(self.hwnd) & SIZING_STYLE):
                return current
            started = time.monotonic()
            try:
                for _ in range(3):
                    self._same_target()
                    self.api.set_style(self.hwnd, self.api.style(self.hwnd) & ~SIZING_STYLE)
                    self.api.resize(self.hwnd, self.base)
                    time.sleep(.04)
                    current = self._same_target()
                    if (current['width'], current['height']) == self.base:
                        return current
                raise RuntimeError(f'无法将游戏客户区稳定调整为 {self.base[0]}×{self.base[1]}，已停止输入。')
            finally:
                self.paused_seconds += time.monotonic() - started

    def clock(self):
        return time.monotonic() - self.paused_seconds

    def close(self):
        with self._mutex:
            if self._closed:
                return
            try:
                if self._claim and self._style is not None:
                    try:
                        if not self.api.alive(self._claim):
                            raise RuntimeError('进程已退出。')
                        current = (self.api.identity(self.hwnd) if hasattr(self.api, 'identity')
                                   else self.api.inspect(self.hwnd))
                        if any(current[key] != self.identity[key] for key in ('pid', 'class_name')):
                            raise RuntimeError('窗口身份改变。')
                    except RuntimeError:
                        pass  # Never restore style on a recycled HWND/PID.
                    else:
                        self.api.set_style(self.hwnd, (self.api.style(self.hwnd) & ~SIZING_STYLE) | self._style)
                        # Re-adding borders must not change the game coordinate space.
                        try:
                            self.api.inspect(self.hwnd)
                        except RuntimeError:
                            pass  # A minimized original window still gets its style back.
                        else:
                            self.api.resize(self.hwnd, self.base)
            finally:
                self._closed = True
                if self._claim:
                    self.api.release(self._claim)
                    self._claim = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def prepare_window(hwnd, base=STANDARD_BASE):
    with WindowLease(hwnd, base) as lease:
        return lease.ensure()


def controller_hwnd(controller):
    info = getattr(controller, 'info', None)
    value = info.get('hwnd') if isinstance(info, dict) else None
    if value is None:
        return None  # Memory controllers have no OS window.
    if isinstance(value, str):
        value = int(value, 16 if value.lower().startswith('0x') else 10)
    if type(value) is not int or value <= 0:
        raise RuntimeError('控制器的已绑定窗口无效。')
    return value


def prepare_controller(controller, base):
    """Return whether a native frame cache must be invalidated after resizing."""
    hwnd = controller_hwnd(controller)
    if hwnd is None:
        return False
    api = Win32Geometry()
    before = api.inspect(hwnd)
    prepare_window(hwnd, base)
    return (before['width'], before['height']) != tuple(base)
