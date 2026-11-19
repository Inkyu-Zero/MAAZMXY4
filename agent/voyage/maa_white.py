"""用 MaaFramework 的 ColorMatch 找小地图白块。

前面手写切格/阈值一直不准，MAA 自带的 ColorMatch 实测能稳定拿到白块中心。
返回归一化坐标 (rx, ry)，再交给仓库的 map_list 做最近邻。
"""
from __future__ import annotations

import ctypes
import time

import cv2
import numpy as np
from pathlib import Path

_MAA = None
_ERR = None


def _init(hwnd: int):
    """建立 Resource + Win32Controller + Tasker（只做识别，不跑 pipeline）。"""
    global _MAA, _ERR
    if _MAA is not None or _ERR is not None:
        return _MAA
    try:
        from maa.controller import Win32Controller
        from maa.resource import Resource
        from maa.tasker import Tasker

        res = Resource()
        # 用相对 __file__ 的绝对路径：MFA 的工作目录是项目根，
        # 写 "./maa_res" 会找不到（实测报 path not exists，白块识别直接失效）。
        _res_dir = str(Path(__file__).resolve().parent / "maa_res")
        res.post_bundle(_res_dir).wait()
        ctrl = Win32Controller(hwnd)
        ctrl.post_connection().wait()
        tasker = Tasker()
        tasker.bind(res, ctrl)
        _MAA = tasker
    except Exception as exc:                      # noqa: BLE001
        _ERR = exc
    return _MAA


def white_center(image_bgr: np.ndarray, hwnd: int | None = None):
    """返回白块中心的归一化坐标 (rx, ry)；找不到返回 None。"""
    from maa.pipeline import JColorMatch, JRecognitionType

    h, w = image_bgr.shape[:2]
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    tasker = _init(hwnd) if hwnd else _MAA
    if tasker is None:
        return None
    # 小地图在右上角，给个大一点的 ROI，容错
    roi = (int(0.92 * w), int(0.01 * h), max(8, int(0.075 * w)), max(8, int(0.22 * h)))
    try:
        detail = tasker.post_recognition(
            JRecognitionType.ColorMatch,
            JColorMatch(lower=[[225, 225, 225]], upper=[[255, 255, 255]],
                        roi=roi, method=4, count=1),
            rgb,
        ).wait().get()
        rec = detail.nodes[0].recognition
        if not rec.hit or rec.box is None:
            return None
        box = rec.box
        return ((box.x + box.w / 2) / w, (box.y + box.h / 2) / h)
    except Exception:                             # noqa: BLE001
        return None
