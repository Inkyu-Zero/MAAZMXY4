"""Keep AIR's synthetic hover stable while one owned input operation runs."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from pathlib import Path
import threading

_library = None
_library_lock = threading.Lock()


def _native():
    global _library
    with _library_lock:
        if _library is None:
            path = Path(__file__).resolve().parents[1]/'runtimes/win-x64/native/MaaBackgroundMouseGuard.dll'
            if not path.is_file():
                raise RuntimeError('缺少后台鼠标保护模块，请更新完整的小助手运行文件。')
            lib = ctypes.WinDLL(str(path), use_last_error=True)
            lib.MaaMouseGuardVersion.restype = ctypes.c_uint
            if lib.MaaMouseGuardVersion() != 1:
                raise RuntimeError('后台鼠标保护模块版本不兼容。')
            lib.MaaMouseGuardBegin.argtypes = [wintypes.HWND]
            lib.MaaMouseGuardBegin.restype = ctypes.c_void_p
            lib.MaaMouseGuardEnd.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
            lib.MaaMouseGuardEnd.restype = wintypes.BOOL
            _library = lib
        return _library


def _air_window(hwnd):
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    name = ctypes.create_unicode_buffer(128)
    if not api.GetClassNameW(hwnd, name, len(name)):
        raise RuntimeError('绑定窗口已关闭，未发送鼠标输入。')
    return name.value == 'ApolloRuntimeContentWindow'


@contextmanager
def hold_background_mouse(controller=None, *, hwnd=None):
    """No screenshots, cursor/window movement, keyboard interception, or retries.

    Non-Win32 and non-AIR controllers retain their original behavior. Native
    begin pins the target process and excludes simultaneous leases. End runs
    even when an action fails or cancellation is raised.
    """
    if hwnd is None:
        info = getattr(controller, 'info', None)
        if info is None or isinstance(info, dict) and 'hwnd' not in info:
            yield
            return
        from mouse_input import bound_hwnd
        hwnd = bound_hwnd(controller)
    if not hwnd or not _air_window(hwnd):
        yield
        return
    lib = _native()
    session = lib.MaaMouseGuardBegin(hwnd)
    if not session:
        raise RuntimeError(f'无法启用目标窗口的后台鼠标保护（Windows {ctypes.get_last_error()}），未发送输入。')
    try:
        yield
    finally:
        filtered = wintypes.DWORD()
        if not lib.MaaMouseGuardEnd(session, ctypes.byref(filtered)):
            raise RuntimeError(f'后台鼠标保护清理未确认（Windows {ctypes.get_last_error()}），已停止后续操作。')
