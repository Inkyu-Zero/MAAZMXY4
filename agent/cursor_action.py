"""把光标挪开（虚拟输入：只给游戏发鼠标消息，**不动真实光标**）。

为什么需要：点击关卡图标后如果游戏卡顿没进去，光标会一直停在图标上，
图标进入高亮态，而 `万世冥阁场景.png` / `七叶树窟场景.png` 这些模板拍的
都是**未高亮**的样子 —— 下一轮 `识别XXX场景` 就会只匹配到 0.55，然后
整条任务被判失败（用户 m10908）。

不能用 Swipe 代替：那个地图是可以拖动的，按住划会把地图拽走
（用户 m10920「地图是可以拉动的，不能让鼠标按着划，只能直接划」）。

为什么不用 MAA 的 `post_relative_move`：实测在 Win32 controller 上
**行为不可预测** —— 连续三次 `(-150,-100)` 让光标落到了
(304,525) → (51,333) → (428,407)，跟请求的位移毫无关系
（2026-10-31 02:54 实测）。

为什么不用 `SetCursorPos`：它移动的是**真实光标**，用户正在用电脑时会被抢走
（用户 m11771「不行，必须虚拟输入，影响我前台」）。所以这里改成往游戏窗口
PostMessage 一条 `WM_MOUSEMOVE`，坐标给窗口客户区的左上角 —— 游戏的悬停
判定跟着这条消息走，而系统光标一动不动。
"""
import ctypes
import ctypes.wintypes
import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

WM_MOUSEMOVE = 0x0200


def _bound_hwnd(controller):
    """Use only the controller's explicit target; never enumerate another instance."""
    info = controller.info
    value = info.get('hwnd') if isinstance(info, dict) else getattr(info, 'hwnd', None)
    if isinstance(value, bool):
        raise ValueError('控制器窗口句柄无效')
    if isinstance(value, str):
        value = int(value, 16 if value.lower().startswith('0x') else 10)
    if not isinstance(value, int) or not 0 < value < 2 ** (ctypes.sizeof(ctypes.c_void_p) * 8):
        raise ValueError('控制器未提供已绑定的窗口句柄，未选择其它窗口')
    return value


def _post_move(hwnd, x, y):
    """给窗口发一条 WM_MOUSEMOVE（客户区坐标），返回 (ok, 说明)。"""
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindow.restype = ctypes.wintypes.BOOL
    user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.PostMessageW.restype = ctypes.wintypes.BOOL
    if not user32.IsWindow(hwnd):
        return False, '绑定窗口已关闭'
    lparam = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
    ok = user32.PostMessageW(int(hwnd), WM_MOUSEMOVE, 0, lparam)
    if not ok:
        return False, 'PostMessageW 返回 0（窗口可能已关闭）'
    return True, '客户区 (%d,%d)' % (x, y)


@AgentServer.custom_action('move_cursor')
class MoveCursor(CustomAction):
    """给游戏发一条鼠标移动消息，把悬停判定挪到角落，不动真实光标。"""

    def run(self, context, argv):
        try:
            if context.tasker.stopping:
                return CustomAction.RunResult(success=False)
            raw = getattr(argv, 'custom_action_param', None) or '{}'
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            x, y = int(args.get('x', 20)), int(args.get('y', 20))
            if not 0 <= x <= 32767 or not 0 <= y <= 32767:
                raise ValueError('虚拟鼠标坐标无效')
            hwnd = _bound_hwnd(context.tasker.controller)
            if context.tasker.stopping:
                return CustomAction.RunResult(success=False)
            ok, detail = _post_move(hwnd, x, y)
            print('move_cursor 虚拟移开 → %s %s' % ('成功' if ok else '失败', detail), flush=True)
            return CustomAction.RunResult(success=ok)
        except Exception as error:  # noqa: BLE001
            print('move_cursor 已停止：%s: %s' % (type(error).__name__, error), flush=True)
            return CustomAction.RunResult(success=False)
