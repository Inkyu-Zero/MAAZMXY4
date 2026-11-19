"""用 MaaFramework 的 FramePool 截图。

用户明确要求：只用 FramePool，不要 PrintWindow。
（PrintWindow 实测 40~74ms/帧、均值 49ms，是帧率只有 8 的主因之一。）

这里只做一件事：给一张和窗口客户区同尺寸的 BGR 图。
不涉及鼠标键盘，也不改游戏设置。
"""
from __future__ import annotations

import cv2
import numpy as np

_CTRL = None
_ERR = None


def _init(hwnd: int):
    global _CTRL, _ERR
    if _CTRL is not None or _ERR is not None:
        return _CTRL
    try:
        from maa.controller import Win32Controller
        from maa.define import MaaWin32ScreencapMethodEnum

        ctrl = Win32Controller(
            hwnd, screencap_method=MaaWin32ScreencapMethodEnum.FramePool
        )
        ctrl.post_connection().wait()
        # 尽量拿原始像素，别再让它缩放（缩放等于白丢细节，还多一次重采样）
        try:
            ctrl.set_screenshot_use_raw_size(True)
        except Exception:                                    # noqa: BLE001
            pass
        _CTRL = ctrl
    except Exception as exc:                                 # noqa: BLE001
        _ERR = exc
    return _CTRL


def grab(hwnd: int, w: int, h: int, tries: int = 3) -> np.ndarray:
    """抓一帧 BGR，尺寸强制成 (w, h)。连续失败抛异常（交给上层重试）。"""
    ctrl = _init(hwnd)
    if ctrl is None:
        raise RuntimeError("FramePool 截图不可用：%s" % _ERR)
    last = "None"
    for _ in range(tries):
        img = ctrl.post_screencap().wait().get()
        if img is None:
            last = "None"
            continue
        img = np.ascontiguousarray(img)
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        if img.shape[1] != w or img.shape[0] != h:
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
        return img
    raise RuntimeError("FramePool 连续 %d 次没拿到画面（%s）" % (tries, last))
