"""全局截图缩放：把所有任务的截图统一到短边 720（约 1147x720）。

项目里所有 pipeline 的 roi 都是按 1147x720 写的。只要截图尺寸一致，识别
就一致；尺寸变了（造梦盒子默认窗口是 940x590）roi 就全部错位。

MAA 的 Controller 自带缩放，而且默认是开的。项目里的 `_grab` 曾经把它关成
`set_screenshot_use_raw_size(True)`，才导致换台机器就认不出东西。

这里提供 `ensure_view_scale()`：把控制器设成「吃缩放、短边 720」。
任务入口直接调它；sink 只是兜底。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# 截图短边缩放到多少像素。造梦盒子客户区比例固定 1.593，
# 短边 720 -> 长边必然是 1147，正好是本项目 roi 的基准尺寸。
SHORT_SIDE = 720

# 0=NEAREST 1=LINEAR 2=CUBIC 3=AREA 4=LANCZOS4。缩小时 AREA 最平滑，
# 对模板匹配和 OCR 都更友好。
RESIZE_METHOD = 3

# controller id -> 上次设置的时间戳，只给 sink 的节流用
_LAST_AT: dict[int, float] = {}


def ensure_view_scale(controller, quiet: bool = True, throttle: float = 0.0) -> bool:
    """把控制器截图缩放到短边 720。

    每次都重新设一遍而不是只设一次：`set_screenshot_use_raw_size` 是
    controller 级别的全局开关，九转炼丹会临时把它打开，收工后必须靠这里
    再关回去；只设一次会漏掉那次恢复。

    Args:
        controller: MAA 的 Controller 实例。
        quiet: True 时不打日志。
        throttle: 大于 0 时，同一控制器在该秒数内重复调用直接返回。
            sink 每条事件都会调这里，靠它避免每秒打几十次 API；
            任务入口保持默认 0，保证 jzld 收工能立刻恢复。

    Returns:
        True 表示设置成功（或被节流跳过）。
    """
    if controller is None:
        return False

    key = id(controller)
    if throttle > 0:
        now = time.time()
        if now - _LAST_AT.get(key, 0.0) < throttle:
            return True
        _LAST_AT[key] = now

    try:
        controller.set_screenshot_use_raw_size(False)
        controller.set_screenshot_target_short_side(SHORT_SIDE)
        controller.set_screenshot_resize_method(RESIZE_METHOD)
    except Exception as error:  # noqa: BLE001
        if not quiet:
            logger.warning("设置截图缩放失败：%s", error)
        return False

    return True


def install_sink() -> None:
    """注册控制器事件 sink，作为兜底：任何 Controller 事件都把缩放设一遍。

    它只是兜底。真正的时机还是各任务入口主动调 `ensure_view_scale`——
    sink 要等到控制器发出第一个事件才有机会执行，那时第一次截图可能已经发生。
    """
    try:
        from maa.agent.agent_server import AgentServer
        from maa.controller import ControllerEventSink
    except Exception as error:  # noqa: BLE001
        logger.warning("注册截图缩放 sink 失败：%s", error)
        return

    class _ViewScaleSink(ControllerEventSink):
        def on_controller_action(self, controller, noti_type, detail):
            ensure_view_scale(controller, throttle=1.0)

        def on_raw_notification(self, controller, msg: str, details: dict) -> None:
            ensure_view_scale(controller, throttle=1.0)

    try:
        AgentServer.add_controller_sink(_ViewScaleSink())
    except Exception as error:  # noqa: BLE001
        logger.warning("挂载截图缩放 sink 失败：%s", error)
