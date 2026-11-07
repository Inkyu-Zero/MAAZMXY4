"""Background hover cleanup shared by navigation, games and playback endings.

No Maa registration, screenshot, polling, physical-cursor movement or clicks.
"""
import ctypes
from ctypes import wintypes


def bound_hwnd(controller):
    return _hwnd_from_info(controller.info)


def _hwnd_from_info(info):
    value = info.get('hwnd') if isinstance(info, dict) else getattr(info, 'hwnd', None)
    if isinstance(value, str):
        value = int(value, 16 if value.lower().startswith('0x') else 10)
    if type(value) is not int or not 0 < value < 2 ** (ctypes.sizeof(ctypes.c_void_p) * 8):
        raise ValueError('控制器未提供已绑定的窗口句柄，未选择其它窗口')
    return value


def post_move(hwnd, x=20, y=20):
    api = ctypes.windll.user32
    api.IsWindow.argtypes, api.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    api.PostMessageW.restype = wintypes.BOOL
    if not api.IsWindow(hwnd):
        return False, '绑定窗口已关闭'
    point = ((int(y) & 0xffff) << 16) | (int(x) & 0xffff)
    if not api.PostMessageW(hwnd, 0x200, 0, point):
        return False, '后台鼠标移动消息发送失败'
    return True, '已后台移开悬停'


def move_off_target(controller):
    """One message and no screenshots. Non-window controllers need no hover."""
    info = getattr(controller, 'info', None)
    if info is None or isinstance(info, dict) and 'hwnd' not in info:
        return True, '非窗口控制器，无需移开悬停'
    return post_move(_hwnd_from_info(info), 20, 20)


def finish_mouse_operation(controller):
    """Call only after a complete click/drag, or after a whole recording."""
    ok, reason = move_off_target(controller)
    if not ok:
        raise RuntimeError(reason)
