"""查询坐标处顶层窗口的进程权限，并在输入前检查完整性级别。"""

from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process
import win32security


class WindowPermissionError(PermissionError):
    """目标窗口权限不足或无法查询，取消本次任务。"""


@dataclass(frozen=True)
class ProcessPermissions:
    pid: int
    elevated: bool
    integrity_level: int


def get_process_permissions(pid=None):
    """pid 省略时查询自身；句柄仅申请查询权限，并在退出时关闭。"""
    own_process = pid is None
    if own_process:
        pid = win32api.GetCurrentProcessId()
    process = (win32api.GetCurrentProcess() if own_process else
               win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid))
    try:
        token = win32security.OpenProcessToken(process, win32security.TOKEN_QUERY)
        try:
            elevated = bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
            level = sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
            return ProcessPermissions(pid, elevated, level)
        finally:
            token.Close()
    finally:
        if not own_process:
            process.Close()


def get_window_permissions(point):
    """point 为屏幕坐标 (x, y)，查询该处顶层窗口所属进程。"""
    hwnd = win32gui.WindowFromPoint(tuple(point))
    if not hwnd:
        raise WindowPermissionError("指定坐标处没有可查询的窗口，请保持游戏画面可见后重试。")
    hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return get_process_permissions(pid)


def ensure_window_permission(point):
    """自身完整性级别低于目标时抛出异常，不发送输入。"""
    try:
        target = get_window_permissions(point)
        current = get_process_permissions()
    except WindowPermissionError:
        raise
    except win32api.error as exc:
        raise WindowPermissionError(f"无法查询游戏窗口权限，本次任务已取消：{exc}") from exc
    if current.integrity_level < target.integrity_level:
        raise WindowPermissionError(
            "助手权限低于游戏窗口，已取消本次任务。请关闭助手，"
            "右键选择“以管理员身份运行”后重试"
        )
