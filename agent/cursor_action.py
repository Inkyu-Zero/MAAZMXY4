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

from mouse_input import bound_hwnd as _bound_hwnd, post_move as _post_move


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


@AgentServer.custom_action('click_and_clear_hover')
class ClickAndClearHover(CustomAction):
    """Pipeline Click equivalent with one background move after button release."""
    def run(self, context, argv):
        try:
            if context.tasker.stopping:
                return False
            x, y, width, height = list(argv.box)
            if width <= 0 or height <= 0:
                raise ValueError('点击缺少有效的当前识别框。')
            result = context.tasker.controller.post_click(int(x + width // 2), int(y + height // 2)).wait()
            if not result.succeeded or context.tasker.stopping:
                return False
            ok, detail = _post_move(_bound_hwnd(context.tasker.controller), 20, 20)
            if not ok:
                print('点击后的悬停未移开：' + detail, flush=True)
            return ok
        except Exception as error:
            print('点击并移开悬停失败：' + str(error), flush=True)
            return False
