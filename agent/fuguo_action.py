# -*- coding: utf-8 -*-
"""福果来（造梦西游4 V59.2 限时活动）自动化入口。

pipeline 链路见 resource/base/pipeline/福果来.json：
  福果来_进入副本  -- OCR 找到活动页的「进入副本」按钮并点击
  福果来_副本内    -- action:Custom / name=fuguo_run，由本文件接管整局
  福果来_结束      -- StopTask

本文件负责副本内那一局：高频抓帧（MaaFramework FramePool，不走 PrintWindow），
按画面决定 A/D 走位接果子、按 Y/U/I/O 技能，守护兽预警时按 I 让悟空停手，
结算界面出现「领取奖励」就点掉。

视觉参数按用户录屏 QQ20261125-192632.mp4（1920x1196）标定，见 VIS 注释。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
import traceback

import numpy as np
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JOCR

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

_HERE_LOG = None
_SHOP_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 视觉参数
# 全部是「按窗口宽高比例」写的，跟实际分辨率无关；面积阈值按 1920x1196 标定，
# 运行时再按 (w*h)/(1920*1196) 缩放。
VIS = {
    # 普通果：亮黄发光小果，接 +10 / 漏 -5。下落中的果子在 1920x1196 上约 62x44、面积 1900。
    "yellow_hsv": ((20, 140, 235), (35, 255, 255)),   # V 下限 235：普通果发光 V≈248，黄金果只有 112~215
    "yellow_min_area": 450,        # 只找辉光「亮心」，面积比整颗果小得多
    # 黄金果：棕橙色大果，接 +20 / 漏 -10。约 42x38、面积 700~2200。
    "brown_hsv": ((6, 140, 90), (30, 255, 235)),   # H 10~27 / S 174~247 / V 112~215；V 上限 235 与普通果分开
    "brown_min_area": 700,
    "brown_max_area": 3200,        # 实测最大 810，原上限折算只到 785
    "brown_aspect": (0.7, 1.6),
    # 找果子的纵向区间：上边界压在树冠下方，树冠上挂着的果子不接（等它掉下来再管）；
    # 下边界压在技能栏上方，左下 Y/U/I/O 图标本身是金色的，会被当成普通果。
    # 0.45 会漏掉刚脱手那一小段，实测放到 0.40 每题只多 0.2 个目标、没有误检暴增。
    "fruit_band": (0.05, 0.93),
    # 左下技能栏兜底跳过（万一分辨率不同导致上一条没盖住）。
    "skip_rect": (0.0, 0.0, 0.0, 0.0),   # 不再按区域排除：左下技能栏在接果区里，会误伤真果子；靠「静止」判据过滤
    # 八戒：头上一圈粉红头巾，是画面里唯一的粉色块，比深蓝身体好认得多。
    "pink_hsv": ((150, 55, 110), (180, 220, 255)),
    "pink_band": (0.60, 0.96),
    "pink_x_band": (0.06, 0.96),
    "pink_min_area": 400,
    # 守护兽预警：左上角冒白色思考泡泡（里面一条蓝龙 + 两个感叹号）。
    # 实测有泡泡时该区域白色占比 0.14，没泡泡时 0.0001，阈值给 0.02 足够稳。
    "guard_rect": (0.01, 0.10, 0.26, 0.45),
    "guard_white_sv": (45, 225),
    "guard_white_ratio": 0.008,
    # 泡泡消失就说明守护兽走开了，可以立刻按 I 继续摇树；留 0.3 秒只是防止
    # 气泡消失动画中间闪一下导致来回切换。
    "guard_recover_delay": 0.2,
    "pause_max": 8.0,
    # 果子区域前后两帧的灰度差均值要达到这个数才算「在动」；
    # 用来筛掉灌木/树冠上挂着不动的装饰果（它们和真果子长得一模一样）。
    "move_diff": 5.0,
    # 接果地面在画面高度上的位置（果子落到这条线就判定/扣分）。
    "ground_y_ratio": 0.82,
    # 录屏实测：果子下落 420 px/s、八戒横移 717 px/s（都按 1920 宽画面标定）。
    "fall_speed": 420.0,
    "move_speed": 650.0,          # 717 是理论峰值（起停、加减速都不算），按 520 估才不吃亏
    # 来得及的余量：t_fall - t_move 至少要有这么多，才算「跑得过去」。
    "min_slack": 0.1,           # 判定「来得及」多留点余量，别承诺了却接不到
    # 果子价值：接黄金果 +20、漏了 -10，里外差 30；普通果接 +10、漏 -5，差 15。
    # 果子接不完，所以按「价值 / 跑过去要花的时间」排优先级，黄金果天然是普通果的两倍。
    "value_gold": 48.0,
    "value_normal": 15.0,          # 金色 = 普通 ×3.2：1 金顶 3 普，平手时优先去拿金色
    # 八戒怀里那个米色大袋子：果子只有砸到袋子上才加分，砸到头上不算，
    # 所以走位的目标点取袋子中心而不是头巾中心。袋子是画面里唯一成片的
    # 低饱和高亮块（实测 HSV 均值 H20 S57 V189，p10 S48 V149），就在八戒身边，
    # 用「头巾左右 260px」这个范围把它圈住。
    "bag_hsv": ((0, 15, 130), (180, 68, 255)),
    "bag_min_area": 4000,
    "bag_band": (0.70, 0.95),
    "bag_search_radius": 260,
    # 袋子中心离目标果子多少像素以内就不再挪：袋子本身约 120px 宽，半宽 60。
    "reach_tolerance": 55,
    # 一局 90 秒，但不再按时间收工：识别到「挑战成功」就停。这个值只做防卡死兜底。
    "round_timeout": 200.0,
    # 每帧之间睡多久（秒）。
    "frame_sleep": 0.005,
    # 每隔多久做一次「领取奖励」OCR（秒），OCR 比较贵，不用每帧跑。
    "claim_probe_interval": 1.5,
    # 技能调度：三个技能各管各的，不再共用一个节流；下面这个只是同一技能的防连按
    "repeat_cooldown": 3.0,
    "u_cooldown": 1.2,
    "dir_hold": 0.12,
    "park_tol": 60,                  # 停摇时离屏幕中线多近算「已经站好位」           # 方向至少保持 0.45 秒，防止原地横跳          # U 单独更勤：袋子一缩就补，不然半径砍半
    # 离目标近到这个距离就改成短按微调（一帧全速走 65px，按住会冲过头）
    "fine_zone": 150,
    # 一帧的实际耗时（8 FPS 约 0.125，留点余量）
    "frame_step": 0.13,
    "many_fruit": 3,         # 画面上同时有这么多颗果子，就优先放 U 撑宽袋子
}

# 结算界面「领取奖励」先不点：用户要先确认副本内能不能正常跑完一局，
# 确认稳定后把这里改成 True，_find_claim + _claim 就会自动接回来。
CLAIM_REWARD = False

# 技能键：Y 加速 / U 袋子变大 / I 停摇（也是躲守护兽）/ O 更多果子
_SKILL_KEYS = {"Y": 0x59, "U": 0x55, "I": 0x49, "O": 0x4F}
_MOVE_KEYS = {"A": 0x41, "D": 0x44}
ALL_KEYS = tuple(_SKILL_KEYS.values()) + tuple(_MOVE_KEYS.values())

# 标定基准分辨率
_REF_AREA = 1920.0 * 1196.0


def _as_dict(value) -> dict:
    """custom_action_param 在 binding 里是 JSON 字符串，不是 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _write_gui(msg: str) -> None:
    """写一行给 pipeline 的 focus 用。覆盖写，不累积——
    focus 把整个文件读进 GUI，文件越长越难看。"""
    try:
        from pathlib import Path

        d = Path(__file__).resolve().parent.parent / "debug" / "fuguo"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "score.md", "w", encoding="utf-8") as fp:
            fp.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def _write_shop_gui(msg: str) -> None:
    """写一行给神秘商店的 focus 用。覆盖写。"""
    try:
        from pathlib import Path

        d = Path(__file__).resolve().parent.parent / "debug" / "shop"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "buy.md", "w", encoding="utf-8") as fp:
            fp.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def _shop_item_name(context, box, frame=None) -> str:
    """读购买按钮上方的商品名（取最上面一行文字，跳过纯数字和「灵魂/点券」价格行）。"""
    try:
        from maa.pipeline import JOCR

        from script_task import capture

        x, y, w, h = (int(v) for v in list(box)[:4])
        roi = [max(0, x - 40), max(0, y - 152), w + 80, 120]
        # box 是 MAA 给识别框（窗口原始坐标系），帧也必须是原始的
        if frame is None:
            frame = _grab_raw(context)
        if frame is None:
            return ""
        detail = context.run_recognition_direct("OCR", JOCR(roi=roi), frame)
        if detail is None:
            return ""
        items = []
        for item in _boxes_of(detail):
            text = (item[0] or "").strip()
            box_ = item[1]
            if not text or text.isdigit():
                continue
            if "灵魂" in text or "点券" in text:
                continue
            items.append((box_[1], text))
        if not items:
            return ""
        items.sort(key=lambda pair: pair[0])
        return items[0][1]
    except Exception:  # noqa: BLE001
        return ""


def _log(msg: str) -> None:
    """只写 debug/fuguo.log，不 print —— 输出到 stdout 会进 MFA 的 GUI 日志窗。"""
    global _HERE_LOG
    try:
        if _HERE_LOG is None:
            from pathlib import Path

            d = Path(__file__).resolve().parent.parent / "debug"
            d.mkdir(parents=True, exist_ok=True)
            _HERE_LOG = d / "fuguo.log"
        with open(_HERE_LOG, "a", encoding="utf-8") as fp:
            fp.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


_DEBUG_DIR = None


def _dump_debug_frame(img, px, bag_x, reach, targets, t):
    """把当前帧连同识别结果存到 debug/fuguo_frames/，供事后对图分析。
    只落文件，不进 GUI。"""
    global _DEBUG_DIR
    try:
        if _DEBUG_DIR is None:
            from pathlib import Path

            d = Path(__file__).resolve().parent.parent / "debug" / "fuguo_frames"
            d.mkdir(parents=True, exist_ok=True)
            _DEBUG_DIR = d
        from pathlib import Path

        vis = img.copy()
        # 袋子可接范围
        if bag_x is not None and reach:
            cv2.line(vis, (int(bag_x - reach), int(img.shape[0] * VIS["ground_y_ratio"])),
                     (int(bag_x + reach), int(img.shape[0] * VIS["ground_y_ratio"])), (0, 255, 255), 3)
        # 检测到的果子
        for (tx, ty, kind) in targets:
            color = (0, 0, 255) if kind == "gold" else (0, 255, 0)
            cv2.circle(vis, (tx, ty), 26, color, 3)
            cv2.putText(vis, kind[0].upper(), (tx - 8, ty - 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        # 八戒
        if px is not None:
            cv2.line(vis, (px, 0), (px, img.shape[0]), (255, 0, 255), 2)
        out = Path(_DEBUG_DIR) / ("t%03d_n%d.png" % (int(t), len(targets)))
        # 路径里有中文（MAA造梦西游4），cv2.imwrite 会静默返回 False，必须用 imencode+tofile
        out.write_bytes(cv2.imencode(".png", vis)[1].tobytes())
    except Exception as exc:  # noqa: BLE001
        _log("存调试图失败：%r" % (exc,))


# ---------------------------------------------------------------- 窗口 / 按键
def _find_hwnd() -> int:
    """找标题带「造梦」的可见窗口，取面积最大的那个。"""
    u32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    found: list[tuple[int, int]] = []

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        u32.GetWindowTextW(hwnd, buf, 256)
        if "造梦" in buf.value and u32.IsWindowVisible(hwnd):
            rect = ctypes.create_string_buffer(16)
            u32.GetClientRect(hwnd, rect)
            w, h = int.from_bytes(rect[8:12], "little"), int.from_bytes(rect[12:16], "little")
            found.append((w * h, int(hwnd)))
        return True

    u32.EnumWindows(CB(_cb), 0)
    if not found:
        return 0
    return max(found)[1]


def _post(hwnd: int, vk: int, down: bool) -> None:
    """往游戏窗口投键；用 PostMessage 是因为 keybd_event 是全局键，窗口不在前台就全打空。"""
    if not hwnd:
        return
    ctypes.windll.user32.PostMessageW(hwnd, 0x100 if down else 0x101, vk, 0)


def _tap(hwnd: int, vk: int, hold: float = 0.01) -> None:
    """按一下。hold 是按住时长——游戏对 I（停摇）似乎按帧采样，
    30ms 常常一个逻辑帧都没覆盖到，所以 I 单独给长一点的 hold。"""
    _post(hwnd, vk, True)
    time.sleep(hold)
    _post(hwnd, vk, False)


def _release_all(hwnd: int) -> None:
    for vk in ALL_KEYS:
        _post(hwnd, vk, False)


# ---------------------------------------------------------------- 截图


def _grab(ctx: Context) -> np.ndarray | None:
    """抓一帧 BGR。MFA 给的是「画布」，游戏内容只贴在左上，黑边先裁掉。"""
    ctrl = ctx.tasker.controller
    # MAA 自带截图缩放：短边缩到 720 就等价于把窗口统一成 1147x720
    # （造梦盒子的客户区比例固定 1.593，短边 720 → 长边必然是 1147）。
    # 以前这里用 use_raw_size(True) 关掉了缩放，窗口一变形就全错位。
    # 每帧都确认一次（节流 5 秒）：九转炼丹会把全局开关临时打开，只设一次会漏。
    try:
        from view_scale import ensure_view_scale

        ensure_view_scale(ctrl, quiet=False, throttle=5.0)
    except Exception:                                     # noqa: BLE001
        pass
    job = ctrl.post_screencap()
    job.wait()
    img = job.get()
    if img is None:
        return None
    img = np.ascontiguousarray(img)
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    nz = np.any(img[:, :, :3] > 8, axis=2)
    if nz.any():
        rs = np.where(nz.any(axis=1))[0]
        cs = np.where(nz.any(axis=0))[0]
        img = img[rs.min():rs.max() + 1, cs.min():cs.max() + 1]
    return np.ascontiguousarray(img)


# ---------------------------------------------------------------- 识别
def _boxes_of(detail) -> list[tuple[str, list[int]]]:
    """从 RecognitionDetail 里尽力取出 [(文本, [x, y, w, h])]。"""
    if detail is None:
        return []
    for group in ("filtered_results", "all_results", "best_result"):
        results = getattr(detail, group, None)
        if results is None and isinstance(detail, dict):
            results = detail.get(group)
        if results is None:
            continue
        if not isinstance(results, (list, tuple)):
            results = [results]
        out = []
        for item in results:
            text = ""
            for attr in ("text", "name", "label"):
                v = getattr(item, attr, None)
                if v is None and isinstance(item, dict):
                    v = item.get(attr)
                if isinstance(v, str) and v:
                    text = v
                    break
            box = getattr(item, "box", None)
            if box is None and isinstance(item, dict):
                box = item.get("box")
            if text:
                out.append((text, [int(v) for v in box] if box else []))
        if out:
            return out
    return []


def _find_claim(ctx: Context, img: np.ndarray) -> list[int] | None:
    """结算界面有没有「领取奖励」，有就返回按钮框。"""
    try:
        detail = ctx.run_recognition("福果来_领取奖励", img)
    except Exception:  # noqa: BLE001
        return None
    for text, box in _boxes_of(detail):
        if "领取" in text and box:
            return box
    return None


def _player_x(img: np.ndarray) -> int | None:
    """用八戒头上的粉红头巾定位他；找不到返回 None。"""
    if cv2 is None or not img.size:
        return None
    h, w = img.shape[:2]
    ya, yb = VIS["pink_band"]
    xa, xb = VIS["pink_x_band"]
    roi = img[int(h * ya):int(h * yb), int(w * xa):int(w * xb)]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    lo, hi = VIS["pink_hsv"]
    mask = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _lab, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    scale = (h * w) / _REF_AREA
    min_area = VIS["pink_min_area"] * max(0.25, scale)
    best = None
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        if best is None or area > best[0]:
            best = (area, int(cents[i][0]) + int(w * xa))
    return None if best is None else best[1]


def _guard_warning(img: np.ndarray) -> bool:
    """左上角有没有冒守护兽预警的白泡泡。"""
    if cv2 is None or not img.size:
        return False
    h, w = img.shape[:2]
    xa, ya, xb, yb = VIS["guard_rect"]
    roi = img[int(h * ya):int(h * yb), int(w * xa):int(w * xb)]
    if not roi.size:
        return False
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    s_max, v_min = VIS["guard_white_sv"]
    mask = cv2.inRange(hsv, np.array((0, 0, v_min), np.uint8), np.array((180, s_max, 255), np.uint8))
    return (mask.sum() / 255.0) / mask.size > VIS["guard_white_ratio"]


def _detect_fruits(img: np.ndarray) -> list[tuple[int, int, str]]:
    """纯颜色检测：把画面里所有像果子的东西都找出来（含树上挂着不动的）。

    两类果的颜色几乎重叠，只能靠「有没有辉光」分：
    亮心（V>235）= 黄金果，剩下的暗棕块 = 普通果。
    """
    if cv2 is None or not img.size:
        return []
    h, w = img.shape[:2]
    ya, yb = VIS["fruit_band"]
    y0, y1 = int(h * ya), int(h * yb)
    roi = img[y0:y1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    scale = (h * w) / _REF_AREA
    s = max(0.25, scale)

    skip = VIS["skip_rect"]
    sx0, sy0 = int(w * skip[0]), int(h * skip[1])
    sx1, sy1 = int(w * skip[2]), int(h * skip[3])

    def _skipped(cx: int, cy: int) -> bool:
        return sx0 <= cx <= sx1 and sy0 <= cy <= sy1

    out: list[tuple[int, int, str]] = []

    lo, hi = VIS["yellow_hsv"]
    ym = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    ym = cv2.morphologyEx(ym, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(ym, 8)
    glows: list[tuple[int, int]] = []
    for i in range(1, n):
        x, y, bw, bh, area = (int(stats[i, k]) for k in range(5))
        if area < VIS["yellow_min_area"] * s:
            continue
        if bh < 8 * s or not (0.6 <= bw / bh <= 2.2):
            continue        # 地上的光带是又宽又薄的横条，八戒喊话的黄字是细笔画
        cx, cy = x + bw // 2, y + y0 + bh
        if _skipped(cx, cy):
            continue
        glows.append((cx, cy))
        out.append((cx, cy, "gold"))

    lo, hi = VIS["brown_hsv"]
    bm = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    bm = cv2.morphologyEx(bm, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    ar_lo, ar_hi = VIS["brown_aspect"]
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(bm, 8)
    for i in range(1, n):
        x, y, bw, bh, area = (int(stats[i, k]) for k in range(5))
        if area < VIS["brown_min_area"] * s or area > VIS["brown_max_area"] * s:
            continue
        if bh and not (ar_lo <= bw / bh <= ar_hi):
            continue
        cx, cy = x + bw // 2, y + y0 + bh
        if _skipped(cx, cy):
            continue
        # 离任何亮心太近就是同一颗黄金果的外圈，别重复计
        if any(abs(cx - gx) <= 55 and abs(cy - gy) <= 55 for (gx, gy) in glows):
            continue
        out.append((cx, cy, "normal"))
    # 字幕：八戒喊话时整行是亮黄文字，会被当成一串黄金果。
    # 同一高度挤了 4 个以上，按一行字处理，整行丢弃。
    rows: dict[int, list[tuple[int, int, str]]] = {}
    for item in out:
        rows.setdefault(item[1] // 12, []).append(item)
    out = [item for _key, items in rows.items() if len(items) < 4 for item in items]
    return out


def _fruit_targets(
    cur: list[tuple[int, int, str]],
    prev_fruits: list[tuple[int, int, str]] | None = None,
    dt: float | None = None,
    width: int = 1920,
) -> list[tuple[int, int, str]]:
    """只保留「正在下落」的果子，返回 [(中心x, 底部y, "gold"/"normal")]，底部 y 从大到小。

    树上挂着的装饰果和真果子长得一模一样，只能靠「这一帧它有没有往下挪」区分。
    但「挪了多少算挪」不能写死：抓帧快的时候两帧只隔 30ms（落 14px），
    慢的时候隔 130ms（落 55px），写死 15 会在快帧率下把真果子全滤掉。
    所以按实测下落速度 × 实际帧间隔算预期位移，只挪了不到 35% 的才算挂着。
    """
    if not prev_fruits:
        return []      # 第一帧没有对照，一个都不认；下一帧起靠「在下落」确认
    k = width / 1920.0
    step = dt if dt and dt > 0 else VIS["frame_step"]
    expect = VIS["fall_speed"] * k * step
    tol_dy = max(4.0, expect * 0.35)
    out: list[tuple[int, int, str]] = []
    for (x, y, kind) in cur:
        same = [oy for (ox, oy, ok) in prev_fruits if ok == kind and abs(ox - x) <= 25]
        if not same:
            continue        # 上一帧没有 → 刚出现，这一帧不认，等下一帧确认它在下落
        if min(same) >= y - tol_dy:
            continue        # 上一帧就在同一高度、这一帧还没往下走 → 挂着的
        out.append((x, y, kind))
    return sorted(out, key=lambda t: -t[1])


def _bag_x(img: np.ndarray, player_x: int | None) -> tuple[int, int] | None:
    """找八戒怀里那个米色大袋子，返回 (中心x, 宽度)；找不到返回 None。

    这是接果的判定点——果子砸到八戒头上不加分，只有落进袋子才算。
    宽度也要返回：按 U 会让袋子变宽，能接到的范围跟着变大，不能写死。
    只在「头巾左右 bag_search_radius」这个范围里找，避免把远处草地上的亮块认成袋子。
    """
    if cv2 is None or not img.size or player_x is None:
        return None
    h, w = img.shape[:2]
    k = w / 1920.0
    r = int(VIS["bag_search_radius"] * k)
    xa = max(0, int(player_x) - r)
    xb = min(w, int(player_x) + r)
    ya, yb = VIS["bag_band"]
    roi = img[int(h * ya):int(h * yb), xa:xb]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    lo, hi = VIS["bag_hsv"]
    m = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(m, 8)
    s = max(0.25, (h * w) / _REF_AREA)
    best = None
    for i in range(1, n):
        x, _y, bw, _bh, area = (int(stats[i, j]) for j in range(5))
        if area < VIS["bag_min_area"] * s:
            continue
        if best is None or area > best[0]:
            best = (area, x + bw // 2, bw)
    if best is None:
        return None
    return xa + best[1], best[2]


def _find_success(ctx: Context, img: np.ndarray) -> bool:
    """结算界面出现「挑战成功」就说明这一局打完了。"""
    try:
        detail = ctx.run_recognition("福果来_挑战成功", img)
    except Exception:  # noqa: BLE001
        return False
    for text, _box in _boxes_of(detail):
        if "挑战成功" in text:
            return True
    return False


def _fruit_value(kind: str) -> float:
    return VIS["value_gold"] if kind == "gold" else VIS["value_normal"]


def _route_value(
    targets: list[tuple[int, int, str]],
    player_x: int,
    sign: int,
    w: int,
    h: int,
    fall_speed: float | None = None,
    reach_tol: float | None = None,
) -> tuple[float, int | None]:
    """评估「只管往 sign 这一边跑」这一趟最多能捞到多少分，以及最远那颗的 x。

    果子是一串串掉的，一趟走过去往往能顺带接好几颗，所以不能一颗一颗算——
    要按「跑这一边、路上能捞到的全部」来比。跑不到（果子先落地）的不算。

    reach_tol 是袋子半宽：果子落在袋子覆盖范围内就算接到，那一段距离不用跑。
    """
    k = 1920.0 / max(1, w)
    ground = VIS["ground_y_ratio"] * h
    fs = fall_speed or VIS["fall_speed"]
    tol = VIS["reach_tolerance"] * (w / 1920.0) if reach_tol is None else reach_tol
    total = 0.0
    far = None
    for x, y, kind in targets:
        signed = (x - player_x) * sign
        if signed <= 0:
            continue
        dist = max(0.0, abs(x - player_x) - tol)
        t_move = dist * k / VIS["move_speed"]
        t_fall = max(0.0, (ground - y) * k) / fs
        if t_move > t_fall + VIS["min_slack"]:
            continue
        total += _fruit_value(kind)
        if far is None or signed > far[0]:
            far = (signed, x)
    return total, (far[1] if far else None)


def _pick_direction(
    targets,
    player_x: int,
    w: int,
    h: int,
    last_dir: int = 0,
    fall_speed: float | None = None,
    reach_tol: float | None = None,
) -> int:
    """算这一帧往哪边走：-1 左 / 0 站住 / 1 右。

    只追「最快落地」的那一颗，站到它下面就不动。金色果提前 0.6 秒参与排序，
    保证同样紧迫时先接金色。够不着的果子直接忽略，不去夠不到的地方发呆。
    """
    if not targets:
        return 0
    tol = VIS["reach_tolerance"] * (w / 1920.0) if reach_tol is None else reach_tol
    k = 1920.0 / max(1, w)
    ground = VIS["ground_y_ratio"] * h
    fs = fall_speed or VIS["fall_speed"]

    def t_fall(y: int) -> float:
        return max(0.0, (ground - y) * k) / fs

    def reachable(x: int, y: int) -> bool:
        dist = max(0.0, abs(x - player_x) - tol)
        return dist * k / VIS["move_speed"] <= t_fall(y) + VIS["min_slack"]

    cand = [t for t in targets if reachable(t[0], t[1])]
    if not cand:
        # 都来不及也不能死站着：朝最近的那颗挪过去，下一波正好赶得上
        best = min(targets, key=lambda t: abs(t[0] - player_x))
        if abs(best[0] - player_x) <= tol:
            return 0
        return 1 if best[0] > player_x else -1
    # 两颗离得近（袋子能同时盖住）就站到中点，一次接两颗——
    # 否则会出现「脚下一颗、旁边那颗差一点点漏掉」
    near = sorted((t for t in cand if abs(t[0] - player_x) <= 3 * tol), key=lambda t: t[0])
    for i in range(len(near) - 1):
        a, b = near[i], near[i + 1]
        if b[0] - a[0] > 2 * tol:
            continue
        mid = (a[0] + b[0]) // 2
        # 当前位置已经把两颗都盖住了才算到位；
        # 只看中点在不在袋子里，会出现「站 500、实际只盖到 441~559、
        # 570 那颗在外面」仍然不动的情况
        if max(abs(a[0] - player_x), abs(b[0] - player_x)) <= tol:
            return 0
        t_move = max(0.0, abs(mid - player_x) - tol) * k / VIS["move_speed"]
        if t_move <= min(t_fall(a[1]), t_fall(b[1])) + VIS["min_slack"]:
            return 1 if mid > player_x else -1

    # 按「单位时间收益」排：马上就能拿到手的先拿，
    # 不去死等一颗还要落好几秒的金色果——先接旁边的普通果，回头还来得及
    def _density(t) -> float:
        wait = t_fall(t[1]) + max(0.0, abs(t[0] - player_x) - tol) * k / VIS["move_speed"]
        return _fruit_value(t[2]) / max(0.2, wait)

    best = max(cand, key=_density)

    # 等金色果的时候，如果顺路能先去把旁边的普通果拿了再回来，
    # 而不会因此错过金色果，那就去拿——反正站着也是站着
    if best[2] == "gold" and t_fall(best[1]) > 0.6:
        land_gold = t_fall(best[1])
        best_gain, best_t = 0.0, None
        for t in cand:
            if t[2] != "normal":
                continue
            go = max(0.0, abs(t[0] - player_x) - tol) * k / VIS["move_speed"]
            back = max(0.0, abs(best[0] - t[0]) - tol) * k / VIS["move_speed"]
            if max(go, t_fall(t[1])) + back > land_gold - 0.15:
                continue          # 回来就赶不上了，不去
            if _fruit_value(t[2]) > best_gain:
                best_gain, best_t = _fruit_value(t[2]), t
        if best_t is not None:
            if abs(best_t[0] - player_x) <= tol:
                return 0
            return 1 if best_t[0] > player_x else -1
    if abs(best[0] - player_x) <= tol:
        return 0
    return 1 if best[0] > player_x else -1

def _wait_stable(context: Context, ctrl) -> None:
    """连抓两帧几乎一样就算进副本了。最多等 20 秒，等不到也往下走。"""
    deadline = time.time() + 20
    prev = None
    while time.time() < deadline:
        if context.tasker.stopping:
            return
        img = _grab(context)
        if img is None:
            time.sleep(0.1)
            continue
        if prev is not None and prev.shape == img.shape:
            diff = float(np.mean(cv2.absdiff(prev, img))) if cv2 is not None else 0.0
            if diff < 8.0:
                _log("画面已稳定（diff=%.1f）" % diff)
                return
        prev = img
        time.sleep(0.2)
    _log("等画面稳定超时，继续")


# ---------------------------------------------------------------- CustomAction


def _button_is_gray(frame, box) -> bool:
    """看「进入副本」按钮底色是黄还是灰。

    本日次数用完时这个按钮会变成灰底，点了也不会进副本。
    黄底 BGR 大约 (60, 180, 240)，灰底三通道接近 (150, 150, 150)。
    """
    x, y, w, h = (int(v) for v in box[:4])
    x0, y0 = max(0, x + w // 6), max(0, y + h // 3)
    x1, y1 = min(frame.shape[1], x + w * 5 // 6), min(frame.shape[0], y + h * 2 // 3)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return True
    flat = patch.reshape(-1, patch.shape[-1]).astype("int32")
    b, g, r = flat[:, 0], flat[:, 1], flat[:, 2]
    # 按钮中间压着「进入副本」四个黑字，取平均会被拉暗、黄底也可能判成灰，
    # 所以改成数「黄底像素」占多少：黄底 BGR 大约 (60,180,240)，灰底三通道接近。
    yellow = ((r > 170) & (r - b > 70) & (g > 120)).mean()
    _log("进入副本按钮：黄底占比 %.0f%%（均值 B=%.0f G=%.0f R=%.0f）"
         % (yellow * 100.0, float(b.mean()), float(g.mean()), float(r.mean())))
    return yellow < 0.5


@AgentServer.custom_action("fuguo_click_icon")
class FuguoClickIcon(CustomAction):
    def run(self, context, argv) -> bool:
        box = list(getattr(argv, "box", []) or [])
        if len(box) < 4:
            _log("点图标：没拿到识别框")
            return False
        x, y, w, h = (int(v) for v in box[:4])
        # 模板里带着下方「福果来」三个字，所以框中心落在文字上；
        # 图标本体在文字上方，按模板高度上移 18% 再点。
        cx = x + w // 2
        cy = y + h // 2 - int(round(h * 0.18))
        _log("点图标：框 %s -> 点击 (%d, %d)" % (box, cx, cy))
        context.tasker.controller.post_click(cx, cy).wait()
        # 面板是滑出动画，点完立刻截图会拍到半开的界面，
        # 下一步的「进入副本」OCR 就会落空。
        time.sleep(1.2)
        return True


@AgentServer.custom_action("fuguo_enter_stage")
class FuguoEnterStage(CustomAction):
    """点「进入副本」，点完看有没有真进去。

    原来靠按钮底色判断次数用完没有，但那块底色受背景、投影、抠字框影响，
    实测平均色能低到 B=G=R=96（那是面板背景不是按钮），于是每次都误判成
    「本日已完成」。改成点一下看结果：进去副本后右上角一定有「积分」。
    """

    def run(self, context: Context, argv) -> bool:
        try:
            from script_task import capture

            box = None
            for attempt in range(3):
                if attempt:
                    time.sleep(0.6)
                frame = capture(context)
                detail = context.run_recognition("福果来_读进入副本", frame)
                for text, b in _boxes_of(detail):
                    if "进入副本" in text and b[2] >= 60 and b[3] >= 20:
                        box = b
                        break
                if box is not None:
                    break
            if box is None:
                _log("没找到「进入副本」按钮")
                return False
            cx = box[0] + box[2] // 2
            cy = box[1] + box[3] // 2
            _log("进入副本：点 (%d, %d)" % (cx, cy))
            before = frame.copy()
            context.tasker.controller.post_click(cx, cy).wait()
            # 不看界面内容，只看画面有没有变：点完连着抓三帧，
            # 只要有一帧跟点之前差得够多，就说明真进了副本（换场景了）。
            moved = 0.0
            for _ in range(3):
                time.sleep(0.5)
                now_frame = capture(context)
                if now_frame.shape != before.shape:
                    moved = 999.0
                    break
                diff = cv2.absdiff(before, now_frame)
                moved = max(moved, float(diff.mean()))
            _log("进入副本：画面差异 %.1f" % moved)
            if moved >= 8.0:
                target = "福果来_副本内"
            else:
                _log("进入副本：点完画面没变，判定本日已完成")
                target = "福果来_本日已完成"
            context.override_pipeline({"福果来_检查进入副本": {"next": [target]}})
            return True
        except Exception as error:  # noqa: BLE001
            _log("进入副本判断失败：" + str(error))
            return False


@AgentServer.custom_action("fuguo_daily_done")
class FuguoDailyDone(CustomAction):
    """本日福果来活动已完成：往 GUI 日志窗推一行。"""

    def run(self, context: Context, argv) -> bool:
        msg = "本日福果来活动已完成"
        _log(msg)
        _write_gui(msg)
        return True


def _grab_raw(ctx):
    """取一帧 1147x720 的截图。FramePool 是唯一允许的方式。

    `set_screenshot_use_raw_size(True)` 不走 FramePool（21ms → 2000ms），
    还会把 controller 状态搅乱，已全部去掉。
    """
    from script_task import capture
    frame = None
    for attempt in range(1, 3):                       # 刚点完那一帧偶尔拿不到，重试一次
        try:
            frame = capture(ctx)
        except Exception as error:                    # noqa: BLE001
            _log("取帧失败（第 %d 次）：%s" % (attempt, error))
            frame = None
        if frame is not None:
            break
        time.sleep(0.2)
    if frame is None:
        return None
    img = np.ascontiguousarray(frame)
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img




_QUESTION_TPL = "richanghuodong/shenmishangdianwenhao.png"
_QUESTION_SCALES = tuple(round(0.85 + 0.05 * i, 2) for i in range(15))


def _find_question(context, roi, threshold=0.70):
    """在 roi 里找神秘商店的问号（= 这一档没开放），返回 (分数, 点击点)。

    为什么不用 pipeline 的 TemplateMatch：MAA 的识别结果一旦失败，它会先去
    试 next 里的候选，而 next 里放的是「读购买次数」（DirectHit 永远命中），
    结果「没问号 = 这一档已开放」也走了「回去补第一档」，第二档永远买不到。
    自己判、用 DirectHit + CustomAction 承载结果，命中/不命中就都由这里说了算。
    """
    if cv2 is None:
        return 0.0, None
    frame = _grab_raw(context)
    if frame is None:
        return 0.0, None
    src = str(_SHOP_ROOT / "resource" / "base" / "image" / _QUESTION_TPL)
    try:
        tpl = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as error:                            # noqa: BLE001
        _log("读问号模板失败：%s" % error)
        return 0.0, None
    if tpl is None:
        return 0.0, None
    # roi 是按 1147x720 写的，实际帧尺寸不一样时先等比放大，
    # 否则在 1410x885 的窗口上会量到隔壁那格去。
    fh, fw = int(frame.shape[0]), int(frame.shape[1])
    kx, ky = fw / 1147.0, fh / 720.0
    x, y, w, h = (int(round(v * k)) for v, k in zip(roi, (kx, ky, kx, ky)))
    sub = frame[max(0, y):max(0, y) + h, max(0, x):max(0, x) + w]
    if sub.size == 0:
        return 0.0, None
    best_score, best_pt, best_scale = 0.0, None, 0.0
    for scale in _QUESTION_SCALES:
        tw = int(round(tpl.shape[1] * scale))
        th = int(round(tpl.shape[0] * scale))
        if tw < 24 or th < 24 or tw > sub.shape[1] or th > sub.shape[0]:
            continue
        small = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(sub, small, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score > best_score:
            best_score = score
            best_scale = scale
            best_pt = (max(0, x) + loc[0] + tw // 2, max(0, y) + loc[1] + th // 2)
    _log("问号检测：roi=%s best=%.3f scale=%.2f at %s" % (roi, best_score, best_scale, best_pt))
    if best_score < threshold:
        return best_score, None
    return best_score, best_pt


@AgentServer.custom_action("shop_check_tier")
class ShopCheckTier(CustomAction):
    """看某一档有没有问号：有 = 还没开放（返回 True → next 回去补第一档），
    没有 = 已开放（返回 False → on_error 去买这一档）。"""

    def run(self, context, argv) -> bool:
        params = _as_dict(getattr(argv, "custom_action_param", None))
        tier = int(params.get("tier") or 0)
        roi = params.get("roi") or []
        if len(roi) != 4:
            _log("检查第%d档：roi 不对 %s" % (tier, roi))
            return False
        score, _pt = _find_question(context, roi)
        open_now = score < 0.70
        _log("检查第%d档：%s（%.3f）" % (tier, "已开放" if open_now else "还是问号", score))
        return not open_now


_DC_TIMES_ROI = [815, 136, 235, 46]       # 「今日剩余挑战次数 10」在 1147 基准上的位置
_DC_TIMES_TPL_RE = None


def _douchong_left(context, frame) -> int | None:
    """读斗宠大会的「今日剩余挑战次数 N」。读不到返回 None。"""
    import re as _re
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_DC_TIMES_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("斗宠读次数失败：%s" % error)
        return None
    for item in _boxes_of(detail):
        text = (item[0] or "").strip()
        if "挑战次数" not in text:
            continue
        m = _re.search(r"挑战次数\s*[：:]?\s*(\d+)", text)
        if m:
            return int(m.group(1))
    return None




# 三球头像整组的模板（119x120）。用模板匹配而不是固定 roi：
# 固定 roi 只在窗口比例恰好 1.593 时成立，别的机器窗口比例一变就整体错位。
_DC_TPL = "richanghuodong/douchong_dagang.png"
_DC_ROW_ROI = [300, 90, 660, 400]          # 搜五组头像的区域（1147 基准）
_DC_COL_STEP = 158.5                       # 组间距（1147 基准），实测 158~159
_DC_GROUPS = 5                             # 一共五组


def _douchong_reclick(context) -> bool:
    """在大地图菜单上重新找一次斗宠图标并点下去。

    图标点了没反应时（游戏卡一下 / 点击没落下去），画面会一直停在大地图，
    这时候直接重发一次点击就行，不用回入口再绕一圈。
    """
    path = os.path.join(_SHOP_ROOT, "resource", "base", "image",
                        "richanghuodong", "douchongdahui.png")
    try:
        tpl = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        frame = _grab_raw(context)
    except Exception as error:                        # noqa: BLE001
        _log("重点图标：读模板/取帧失败 %s" % error)
        return False
    if tpl is None or frame is None or getattr(frame, "shape", None) is None:
        return False
    kx = frame.shape[1] / 1147.0
    ky = frame.shape[0] / 720.0
    roi = [290, 90, 660, 500]                         # 活动菜单区域（1147 基准）
    x, y = int(round(roi[0] * kx)), int(round(roi[1] * ky))
    w, h = int(round(roi[2] * kx)), int(round(roi[3] * ky))
    sub = frame[y:y + h, x:x + w]
    if sub.size == 0:
        return False
    th = int(round(tpl.shape[0] * ky))
    tw = int(round(tpl.shape[1] * kx))
    if th > sub.shape[0] or tw > sub.shape[1]:
        return False
    scaled = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
    # 这些图标模板都是绿幕版，必须带 mask 匹配：不带的话背景图会掺进来，
    # 同一张图实测只有 0.536，带上就 0.838。
    _blue, _green, _red = cv2.split(tpl.astype(np.int16))
    matte = np.where((_green > 200) & (_red < 80) & (_blue < 80), 0, 255).astype(np.uint8)
    matte = cv2.resize(matte, (tw, th), interpolation=cv2.INTER_NEAREST)
    res = cv2.matchTemplate(sub, scaled, cv2.TM_CCOEFF_NORMED, mask=matte)
    _min_v, max_v, _min_p, max_p = cv2.minMaxLoc(res)
    _log("重点图标：匹配 best=%.3f at=(%d,%d)" % (max_v, max_p[0], max_p[1]))
    if max_v < 0.70:
        return False
    cx = (x + max_p[0] + tw / 2.0) / kx
    cy = (y + max_p[1] + th / 2.0) / ky
    # 同理：交给 MAA 换算，不要再乘窗口比例
    wx, wy = int(round(cx)), int(round(cy))
    _log("重点图标：点击 @ (%d, %d)" % (wx, wy))
    try:
        context.tasker.controller.post_swipe(wx, wy, wx, wy, 80).wait()
    except Exception as error:                        # noqa: BLE001
        _log("重点图标：点击失败 %s" % error)
        return False
    return True


_DC_WIN_ROI = [380, 0, 410, 130]          # 「挑战成功」标题（1147 基准）
_DC_BACK_ROI = [480, 545, 190, 100]        # 「返回」按钮（1147 基准）


def _douchong_won(context, frame) -> bool:
    """结算页有没有「挑战成功」四个字。"""
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_DC_WIN_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("斗宠判结算失败：%s" % error)
        return False
    for item in _boxes_of(detail):
        if "挑战成功" in (item[0] or ""):
            return True
    return False


def _douchong_back(context, frame) -> bool:
    """结算页点「返回」，回到挑战列表。"""
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_DC_BACK_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("斗宠找返回失败：%s" % error)
        return False
    for item in _boxes_of(detail):
        text = (item[0] or "").strip()
        if text != "返回" and "返回" not in text:
            continue
        box = item[1]
        cx = int(box[0]) + int(box[2]) // 2
        cy = int(box[1]) + int(box[3]) // 2
        # box 是当前帧坐标（1147 基准），post_swipe 也收 1147 基准
        try:
            context.tasker.controller.post_swipe(cx, cy, cx, cy, 80).wait()
        except Exception as error:                    # noqa: BLE001
            _log("斗宠点返回失败：%s" % error)
            return False
        _log("斗宠：点返回 @ (%d, %d)" % (cx, cy))
        return True
    return False


def _douchong_target(frame, start_group=None):
    """从第 start_group 组开始往左找，返回第一个那组的中心（1147 基准）。

    不传 start_group 就是「最右边那组」（第 5 组）。

    五组头像位置固定、每组排版和尺寸都一样，所以拿任意一组当模板都能匹配上，
    再按组间距往右推到第 5 组。模板匹配比固定 roi 稳——roi 依赖窗口比例，
    模板只依赖「这一组长什么样」。
    """
    path = os.path.join(_SHOP_ROOT, "resource", "base", "image", _DC_TPL)
    try:
        tpl = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as error:                        # noqa: BLE001
        _log("斗宠找头像：模板读不到 %s（%s）" % (path, error))
        return None
    if tpl is None:
        _log("斗宠找头像：模板解码失败 %s" % path)
        return None
    kx = frame.shape[1] / 1147.0
    ky = frame.shape[0] / 720.0
    x, y, w, h = _DC_ROW_ROI
    x, y = int(round(x * kx)), int(round(y * ky))
    w, h = int(round(w * kx)), int(round(h * ky))
    sub = frame[y:y + h, x:x + w]
    if sub.size == 0:
        _log("斗宠找头像：搜索区为空 roi=%s frame=%s" % (_DC_ROW_ROI, getattr(frame, "shape", None)))
        return None
    th = int(round(tpl.shape[0] * ky))
    tw = int(round(tpl.shape[1] * kx))
    if th > sub.shape[0] or tw > sub.shape[1]:
        _log("斗宠找头像：模板比搜索区大 tpl=%dx%d sub=%dx%d" % (tw, th, sub.shape[1], sub.shape[0]))
        return None
    scaled = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
    res = cv2.matchTemplate(sub, scaled, cv2.TM_CCOEFF_NORMED)
    _min_v, max_v, _min_p, max_p = cv2.minMaxLoc(res)
    _log("斗宠找头像：匹配 best=%.3f at=(%d,%d) 模板 %dx%d" % (max_v, max_p[0], max_p[1], tw, th))
    if max_v < 0.70:
        _log("斗宠找头像：分数不够（%.3f < 0.70）" % max_v)
        return None
    # 五组等距排开，组间距恒定：直接按组数算目标那一组的 x。
    # 不能拿搜索区右边界去试探——搜索区比整行窄，会少算一组。
    step = _DC_COL_STEP * kx
    left = max_p[0]
    hit_group = max(1, min(_DC_GROUPS, int(round(left / step)) + 1))
    want = start_group if start_group else _DC_GROUPS
    want = max(1, min(_DC_GROUPS, int(want)))
    cx = (x + left + step * (want - hit_group) + tw / 2.0) / kx
    cy = (y + max_p[1] + th / 2.0) / ky
    _log("斗宠找头像：命中第 %d 组，点第 %d 组（共 %d 组）@ (%.0f, %.0f)"
         % (hit_group, want, _DC_GROUPS, cx, cy))
    return (cx, cy)


def _scale_roi(roi, frame):
    """把 1147 基准的 roi 换算到当前帧。"""
    kx = frame.shape[1] / 1147.0
    ky = frame.shape[0] / 720.0
    x, y, w, h = roi
    return [int(round(x * kx)), int(round(y * ky)), int(round(w * kx)), int(round(h * ky))]


def _shop_left(context, frame=None) -> int | None:
    """读「今日剩余购买次数 N」，读不到返回 None。

    必须用原始尺寸帧：`神秘商店_读次数` 的 roi 会被 apply_view_scale
    按窗口尺寸放大，拿 1147 的帧去配 1410 的 roi 只会读到空气。
    """
    import re as _re

    if frame is None:
        frame = _grab_raw(context)
    if frame is None:
        return None
    detail = context.run_recognition("神秘商店_读次数", frame)
    texts = [t for t, _b in _boxes_of(detail)]
    for text in texts:
        if "购买次数" not in text:
            continue
        m = _re.search(r"购买次数\s*[：:]?\s*(\d+)", text)
        if m:
            return int(m.group(1))
    for text in texts:
        m = _re.search(r"(\d+)", text)
        if m:
            return int(m.group(1))
    return None


# 全部按 1147x720 基准写。用户当初在 1263 宽的截图上量的，这里等比缩回来，
# 运行时 _match_in 再按实际帧尺寸放大。
def _gui(msg: str) -> None:
    """推一行到 MFA 的日志窗。

    agent 的 stdout 不进 GUI，但 **stderr 会**——`Toolkit.init_option is
    deprecated` 那条 Warning 就是这么出现在日志里的。所以这里统一走 stderr，
    不依赖任何文件路径，别的用户装到哪个目录都能用。
    """
    try:
        # MFA 的 HandleOutputLine 只把 `info:` / `warn:` / `err:` 这类前缀的
        # 行放进日志窗（MaaProcessor.CheckShouldLog），没有前缀的会被当成
        # 内部日志丢掉——所以这里必须带 info:。
        print("info: [商店] " + msg, file=sys.stderr, flush=True)
    except Exception:                                     # noqa: BLE001
        pass


_SHOP_BUY_ROI = {
    1: [183, 485, 90, 40],
    2: [355, 485, 90, 40],
    3: [526, 485, 90, 40],
    4: [697, 485, 90, 40],
    5: [868, 485, 90, 40],
}
_SHOP_Q_ROI = {
    2: [305, 294, 190, 210],
    3: [475, 294, 190, 210],
    4: [646, 294, 190, 210],
    5: [817, 294, 190, 210],
}
_SHOP_SCALES = tuple(round(0.85 + 0.05 * i, 2) for i in range(15))
_SHOP_BUY_TPL = "richanghuodong/goumai.png"


def _match_in(frame, roi, tpl_rel, threshold=0.70):
    """在 roi（1147x720 基准）里多尺度找模板，返回 (分数, 点击点)。

    帧尺寸和 1147x720 不一样时自动等比放大 roi：窗口一变大，
    按 1147 写的 roi 就会偏到隔壁那格去。
    """
    if cv2 is None or frame is None:
        return 0.0, None
    src = str(_SHOP_ROOT / "resource" / "base" / "image" / tpl_rel)
    try:
        tpl = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as error:                            # noqa: BLE001
        _log("读模板失败(%s)：%s" % (tpl_rel, error))
        return 0.0, None
    if tpl is None:
        return 0.0, None
    fh, fw = int(frame.shape[0]), int(frame.shape[1])
    kx, ky = fw / 1147.0, fh / 720.0
    x, y, w, h = (int(round(v * k)) for v, k in zip(roi, (kx, ky, kx, ky)))
    sub = frame[max(0, y):max(0, y) + h, max(0, x):max(0, x) + w]
    if sub.size == 0:
        return 0.0, None
    best_score, best_pt = 0.0, None
    for scale in _SHOP_SCALES:
        tw = int(round(tpl.shape[1] * scale * kx))
        th = int(round(tpl.shape[0] * scale * ky))
        if tw < 16 or th < 16 or tw > sub.shape[1] or th > sub.shape[0]:
            continue
        small = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(sub, small, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score > best_score:
            best_score = score
            best_pt = (max(0, x) + loc[0] + tw // 2, max(0, y) + loc[1] + th // 2)
    if best_score < threshold:
        return best_score, None
    return best_score, best_pt


def _douchong_wait_left(context, tries=6, gap=0.5) -> int | None:
    """重试读斗宠挑战次数，读到就返回，超时返回 None。

    刚点完「返回」那一帧还是结算页，次数行还没画出来。
    """
    for _ in range(tries):
        try:
            frame = _grab_raw(context)
        except Exception as error:                    # noqa: BLE001
            _log("斗宠重读取帧失败：%s" % error)
            return None
        left = _douchong_left(context, frame) if frame is not None else None
        if left is not None:
            return left
        time.sleep(gap)
    return None


@AgentServer.custom_action("douchong_run")
class DouChongRun(CustomAction):
    """斗宠大会：识别右上角「今日剩余挑战次数」，不为 0 就点最右边那组三个圆的几何中心。

    次数为 0 就收工。次数读不到时按「还能打」处理——宁可多打一次，
    也不要因为一次 OCR 抖动就把当天的次数全丢了。
    """

    def run(self, context, argv) -> bool:
        rounds = 0
        pick_group = 5                          # 当前想打哪一组，默认最右边那组
        fail_streak = 0                         # 连续几次点了没进战斗
        for round_no in range(1, 60):
            try:
                frame = _grab_raw(context)
            except Exception as error:                # noqa: BLE001
                _log("斗宠第%d轮：取帧失败 %s" % (round_no, error))
                return True
            if frame is None or getattr(frame, "shape", None) is None:
                _log("斗宠第%d轮：没拿到画面" % round_no)
                return True

            # 先看是不是我已经进战斗了。进去了就等结算，不看挑战列表。
            if _douchong_won(context, frame):
                _log("斗宠第%d轮：战斗结束，挑战成功" % round_no)
                if not _douchong_back(context, frame):
                    _log("斗宠：没找到返回按钮，收工")
                    return True
                rounds += 1
                fail_streak = 0
                pick_group = 5                  # 打完一轮从第 5 组重新开始
                time.sleep(1.0)
                continue

            left = _douchong_left(context, frame)
            _log("斗宠第%d轮：剩余挑战次数 %s，这一轮打第 %d 组"
                 % (round_no, "读不到" if left is None else left, pick_group))

            if left is None:
                # 读不到次数：可能在战斗里、可能在结算页、也可能界面还没切完。
                # 三种情况都不能当成「次数用完」，先等一下再读，最多等 20 秒。
                waited = 0.0
                while left is None and waited < 20.0:
                    time.sleep(1.0)
                    waited += 1.0
                    try:
                        frame = _grab_raw(context)
                    except Exception as error:            # noqa: BLE001
                        _log("斗宠：等待中取帧失败 %s" % error)
                        continue
                    if frame is None or getattr(frame, "shape", None) is None:
                        continue
                    if _douchong_won(context, frame):
                        _log("斗宠：等待 %.0f 秒后看到结算页" % waited)
                        break
                    left = _douchong_left(context, frame)
                    if left is not None:
                        _log("斗宠：等待 %.0f 秒后读到次数 %s" % (waited, left))
                if left is None:
                    _log("斗宠：等了 %.0f 秒仍未认到次数/结算，继续下一轮" % waited)
                    continue
            if left <= 0:
                _log("斗宠：次数用完，收工（打了 %d 次）" % rounds)
                return True

            pt = _douchong_target(frame, pick_group)
            if pt is None:
                # 想打的那组没有对手，往前退一组；退到底都没有就收工
                if pick_group > 1:
                    _log("斗宠：第 %d 组没有对手，改打第 %d 组" % (pick_group, pick_group - 1))
                    pick_group -= 1
                    continue
                _log("斗宠第%d轮：一组对手都找不到，收工" % round_no)
                return True
            wx, wy = int(round(pt[0])), int(round(pt[1]))
            _log("斗宠：点击第 %d 组 @ (%d, %d)" % (pick_group, wx, wy))
            try:
                context.tasker.controller.post_swipe(wx, wy, wx, wy, 80).wait()
            except Exception as error:                # noqa: BLE001
                _log("斗宠：点击失败 %s" % error)
                return True
            time.sleep(1.5)

            # 点完看有没有进战斗：还停在挑战列表（能读到次数）就是没进去
            try:
                after = _grab_raw(context)
            except Exception as error:                # noqa: BLE001
                _log("斗宠：点击后取帧失败 %s" % error)
                return True
            if after is None or getattr(after, "shape", None) is None:
                return True
            if _douchong_won(context, after):
                _log("斗宠：第 %d 组进了战斗" % pick_group)
                fail_streak = 0
                continue
            if _douchong_left(context, after) is not None:
                fail_streak += 1
                _log("斗宠：点了第 %d 组没进战斗（连续 %d 次），下一轮换第 %d 组"
                     % (pick_group, fail_streak, max(1, pick_group - 1)))
                if pick_group > 1:
                    pick_group -= 1
                if fail_streak > 6:
                    _log("斗宠：连点 %d 次都没进战斗，收工" % fail_streak)
                    return True
            # 既不是挑战成功、也读不到次数 —— 多半是战斗界面正在打，下一轮再判
            time.sleep(1.2)
        _log("斗宠：达到 60 轮上限，收工")
        return True


# 竞技场：右上角「今日剩余挑战次数 N」
_JJ_TIMES_ROI = [750, 143, 265, 40]    # 「今日剩余挑战次数 N」实测在 y 148~175、x 772~990
_JJ_TONG_ROI = [960, 230, 180, 160]   # 第 5 组「通」附近，比图标大一圈留漂移空间
_JJ_READY_ROI = [750, 143, 265, 40]    # 「今日剩余挑战次数 N」，只有竞技场主页有

# ---------- 扫荡须弥山（城南/城东/城北，城西不做） ----------
# 入口界面：左侧四个城按钮 + 右侧副本面板（含「进入副本」和「速通」）
_XM_CITY_ROI = {
    "城南": [176, 173, 108, 54],       # 字的精确边界 x178..280 y175..224，四周留 2px
    "城东": [200, 290, 117, 54],       # x202..314 y292..341
    "城北": [200, 403, 83, 54],        # x202..280 y405..454
}
_XM_TITLE_ROI = [400, 90, 350, 70]         # 顶部中间的「守卫须弥山」大字
_XM_ENTER_ROI = [360, 495, 310, 70]         # 「进入副本」大按钮
_XM_SUTONG_TPL = "richanghuodong/sutong.png"   # 速通按钮（OCR 对这块小字读不出来，改模板匹配）
_XM_SUTONG_ROI = [540, 470, 90, 90]         # 「速通」小按钮：字在 x555..600 y520..545，带上上面的剑图标
_XM_FIGHT_ROI = [600, 300, 500, 300]        # 副本内的战斗区域，用来判是否进了副本
_XM_ST_TIMES_ROI = [280, 468, 230, 32]      # 速通弹窗里的「今天剩余次数：N」
_XM_ST_AGAIN_TPL = "richanghuodong/sutong_again.png"   # 「再次速通」按钮
_XM_ST_AGAIN_ROI = [300, 510, 190, 90]
_FG_TIMES_ROI = [400, 503, 420, 32]     # 福果来面板「今日剩余奖励次数：N次」
_XM_CLOSE_ROI = [860, 20, 100, 90]          # 弹窗右上角红叉
_XM_CHALLENGE_ROI = [320, 448, 290, 40]     # 城面板上的「今日可挑战次数：N」（字在 y452..480 x325..590）
_XM_ST_START_ROI = [320, 520, 220, 60]      # 速通界面的「开始速通」按钮


def _dump_frame(frame, tag: str) -> None:
    """把当前帧 + roi 框落盘到 debug/xumishan/。"""
    try:
        d = os.path.join(_SHOP_ROOT, "debug", "xumishan")
        os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        path = os.path.join(d, "%s_%s.png" % (stamp, tag))
        cv2.imencode(".png", frame)[1].tofile(path)
        _log("须弥山：已存图 %s" % path)
    except Exception as error:                        # noqa: BLE001
        _log("须弥山：存图失败 %s" % error)


def _sx_roi(box) -> tuple:
    """把 OCR 框格式化成 (x, y, w, h)，日志用。"""
    try:
        return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    except Exception:                                 # noqa: BLE001
        return tuple(box) if box else ()


def _xm_click_close(context, frame) -> bool:
    """点弹窗右上角的红叉关掉它。

    不用 adventure.py 里的 _find_close_x：那个依赖 cv2 多尺度模板，
    而这里只需要点一下，直接用固定点就行。
    """
    if frame is None or getattr(frame, "shape", None) is None:
        kx = ky = 1.0                      # 没帧就按 1147 基准点，不能因为没帧就动不了
    else:
        kx = frame.shape[1] / 1147.0
        ky = frame.shape[0] / 720.0
    cx, cy = int(round(905 * kx)), int(round(60 * ky))
    try:
        context.tasker.controller.post_swipe(cx, cy, cx, cy, 80).wait()
    except Exception as error:                        # noqa: BLE001
        _log("须弥山：关弹窗失败 %s" % error)
        return False
    _log("须弥山：点叉 @ (%d, %d)" % (cx, cy))
    time.sleep(1.2)
    return True


def _xm_click_text(context, frame, roi, keyword) -> bool:
    """在 roi 里 OCR 找到含 keyword 的字，点它的中心。"""
    if frame is None or getattr(frame, "shape", None) is None:
        _log("须弥山：没帧，认不了「%s」" % keyword)
        return False
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(roi, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("须弥山：认「%s」失败 %s" % (keyword, error))
        return False
    for item in _boxes_of(detail):
        text = (item[0] or "").strip()
        if keyword not in text:
            continue
        box = item[1]
        if len(box) < 4:
            continue
        cx = int(box[0]) + int(box[2]) // 2
        cy = int(box[1]) + int(box[3]) // 2
        try:
            context.tasker.controller.post_swipe(cx, cy, cx, cy, 80).wait()
        except Exception as error:                    # noqa: BLE001
            _log("须弥山：点「%s」失败 %s" % (text, error))
            return False
        _log("须弥山：点「%s」 @ (%d, %d)" % (text, cx, cy))
        return True
    _log("须弥山：roi=%s 里没认出「%s」，OCR 读到：%s"
         % (roi, keyword, [(t, _sx_roi(b)) for t, b in _boxes_of(detail)]))
    # 没认出就把这一帧落盘：只看日志分不清是 roi 偏了还是 OCR 读错
    _dump_frame(frame, "miss_%s" % keyword)
    return False


def _xm_click_template(context, frame, tpl_name, roi) -> bool:
    """用模板匹配找按钮，点它的中心。

    OCR 对「速通」这种 20px 小字不可靠（读成 Y、读成单字、读成空），
    而这个按钮的图案是固定的，模板匹配更稳。
    """
    if frame is None or getattr(frame, "shape", None) is None:
        _log("须弥山：没帧，找不了 %s" % tpl_name)
        return False
    path = os.path.join(_SHOP_ROOT, "resource", "base", "image", tpl_name)
    try:
        tpl = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as error:                        # noqa: BLE001
        _log("须弥山：读模板失败 %s" % error)
        return False
    if tpl is None:
        _log("须弥山：模板解码失败 %s" % path)
        return False
    fh, fw = frame.shape[:2]
    kx, ky = fw / 1147.0, fh / 720.0
    x, y, w, h = roi
    X, Y = int(round(x * kx)), int(round(y * ky))
    W, H = int(round(w * kx)), int(round(h * ky))
    sub = frame[Y:Y + H, X:X + W]
    if sub.size == 0:
        _log("须弥山：搜索区为空")
        return False
    tw, th = int(round(tpl.shape[1] * kx)), int(round(tpl.shape[0] * ky))
    if tw > sub.shape[1] or th > sub.shape[0]:
        _log("须弥山：模板比搜索区大 %dx%d > %dx%d"
             % (tw, th, sub.shape[1], sub.shape[0]))
        return False
    tmpl = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
    # 模板带绿幕：把纯绿当透明处理，否则绿色也会参与匹配，分数会掉到底
    _b, _g, _r = cv2.split(tpl.astype(np.int16))
    matte = np.where((_g > 200) & (_r < 80) & (_b < 80), 0, 255).astype(np.uint8)
    matte = cv2.resize(matte, (tw, th), interpolation=cv2.INTER_NEAREST)
    try:
        if matte.mean() > 10:
            res = cv2.matchTemplate(sub, tmpl, cv2.TM_CCOEFF_NORMED, mask=matte)
        else:
            res = cv2.matchTemplate(sub, tmpl, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _mp, mp = cv2.minMaxLoc(res)
    except Exception as error:                        # noqa: BLE001
        _log("须弥山：匹配失败 %s" % error)
        return False
    _log("须弥山：%s best=%.3f at=(%d,%d)" % (tpl_name, mx, X + mp[0], Y + mp[1]))
    if mx < 0.70:
        return False
    cx = X + mp[0] + tw // 2
    cy = Y + mp[1] + th // 2
    try:
        context.tasker.controller.post_swipe(cx, cy, cx, cy, 80).wait()
    except Exception as error:                        # noqa: BLE001
        _log("须弥山：点 %s 失败" % tpl_name)
        return False
    _log("须弥山：点 %s @ (%d, %d)" % (tpl_name, cx, cy))
    return True


def _xm_st_times(context, frame) -> int | None:
    """读速通界面的「今天剩余次数：N」。"""
    if frame is None or getattr(frame, "shape", None) is None:
        return None
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_XM_ST_TIMES_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("速通：读次数失败 %s" % error)
        return None
    texts = [(item[0] or "").strip() for item in _boxes_of(detail)]
    done = "次数" not in " ".join(texts)
    if done:
        return None
    for text in texts:
        m = re.search(r"\u6b21\u6570\s*[\uff1a:]?\s*(\d+)", text)
        if m:
            return int(m.group(1))
    if not done:
        for text in texts:
            if text.isdigit():
                return int(text)
    return None


def _xm_ready(context, frame) -> bool:
    """判断是不是真在须弥山界面：认顶部中间「守卫须弥山」几个大字。

    大地图菜单里的图标文字叫「守卫须弥山」，所以单纯认这四个字会误判；
    这里叠上 roi 限定在顶部中间那一块——菜单里的图标文字在第 2 行第 2 列（y≈260），不会落进来。
    """
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_XM_TITLE_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("须弥山判界面失败：%s" % error)
        return False
    for item in _boxes_of(detail):
        text = (item[0] or "").strip()
        if "守卫须弥山" in text or "须弥山" in text:
            _log("须弥山：看到「%s」，确认已进入" % text)
            return True
    return False


@AgentServer.custom_action("xumishan_run")
class XuMiShanRun(CustomAction):
    """扫荡须弥山：城南→城东→城北依次扫完，城西不做。

    每个城的流程：点城名 → 点「速通」 → 看剩余次数 → >0 就点「开始速通」。
    次数为 0 就关掉弹窗换下一个城。
    """

    def run(self, context, argv) -> bool:
        try:
            return self._run(context, argv)
        except Exception as error:                    # noqa: BLE001
            _log("须弥山：崩了 %s: %s" % (type(error).__name__, error))
            _log(traceback.format_exc().replace("\n", " | "))
            return True

    def _run(self, context, argv) -> bool:
        total = 0
        frame = _grab_raw(context)
        wait_ms = 0
        for _ in range(10):
            _log("须弥山：第 %d 次判界面" % (wait_ms // 500 + 1))
            if _xm_ready(context, frame):
                break
            time.sleep(0.5)
            wait_ms += 500
            frame = _grab_raw(context)
            if frame is None or getattr(frame, "shape", None) is None:
                break
        _log("须弥山：等界面加载 %dms 后开始判断" % wait_ms)
        if wait_ms >= 5000:
            _log("须弥山：5 秒内没看到「守卫须弥山」，没进入须弥山")
            return True

        for city, roi in _XM_CITY_ROI.items():
            _log("须弥山：进 %s" % city)
            frame = _grab_raw(context)
            if not _xm_click_text(context, frame, roi, city):
                _log("须弥山：%s 没找到城名" % city)
                continue
            # 等界面真切过去（能读到「今日可挑战次数」）再往下走
            ch, frame = _xm_wait(
                context, lambda f: _xm_challenge_times(context, f))
            _log("须弥山：%s 今日可挑战次数 %s"
                 % (city, "读不到" if ch is None else ch))
            if ch is None:
                _log("须弥山：%s 点完没切过去，跳过" % city)
                continue
            if ch <= 0:
                _xm_focus("今日 %s 已完成" % city)
                continue

            # 点速通
            if not _xm_click_template(context, frame, _XM_SUTONG_TPL, _XM_SUTONG_ROI):
                _log("须弥山：%s 没找到速通" % city)
                continue

            # 等速通弹窗弹出来（能读到「今天剩余次数」）
            left, frame = _xm_wait(
                context, lambda f: _xm_st_times(context, f))
            _log("须弥山：%s 速通剩余次数 %s"
                 % (city, "读不到" if left is None else left))
            if left is None or left <= 0:
                _xm_focus("今日 %s 已完成" % city)
                _xm_click_close(context, frame)
                time.sleep(0.8)
                continue

            # 点「开始速通」或「再次速通」
            if not _xm_click_template(context, frame, _XM_ST_AGAIN_TPL, _XM_ST_AGAIN_ROI):
                if not _xm_click_text(context, frame, _XM_ST_START_ROI, "速通"):
                    _log("须弥山：%s 没找到开始速通" % city)
                    _xm_click_close(context, frame)
                    continue
            total += 1
            _log("须弥山：%s 速通了第 %d 次" % (city, total))
            time.sleep(2.5)
            frame = _grab_raw(context)
            _xm_click_close(context, frame)
            time.sleep(1.0)
            # 关掉弹窗后再读一次次数，用完就自己跳到下一个城
            frame = _grab_raw(context)
            ch2 = _xm_challenge_times(context, frame)
            _log("须弥山：%s 速通后今日可挑战次数 %s"
                 % (city, "读不到" if ch2 is None else ch2))
            if ch2 is not None and ch2 <= 0:
                _xm_focus("今日 %s 已完成" % city)
        _log("须弥山：全部完成，共速通 %d 次" % total)
        return True


def _xm_wait(context, probe, tries=10, gap=0.4):
    """等某个 OCR 条件成立，返回 (value, frame)。

    点完一下去 sleep 几百毫秒再取帧是赔钱行为：界面没动就会读到上一层的东西。
    这里反过来：一直取帧试，直到 probe 真的能读出东西才往下走。
    """
    frame = None
    for _ in range(tries):
        frame = _grab_raw(context)
        try:
            value = probe(frame)
        except Exception as error:                    # noqa: BLE001
            _log("须弥山：探测出错 %s" % error)
            value = None
        if value is not None:
            return value, frame
        time.sleep(gap)
    return None, frame


def _xm_focus(msg: str) -> None:
    """推一行到 MFA 日志窗。

    MFA 的 MaaProcessor.CheckShouldLog 只放行带级别前缀的行
    （info: / warn: / err: …），不带前缀的 stderr 行不会进日志窗。
    """
    _log(msg)
    try:
        print("info: " + msg, file=sys.stderr, flush=True)
    except Exception:                                 # noqa: BLE001
        pass


def _xm_challenge_times(context, frame) -> int | None:
    """读城面板上的「今日可挑战次数：N」。"""
    if frame is None or getattr(frame, "shape", None) is None:
        return None
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_XM_CHALLENGE_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("须弥山：读可挑战次数失败 %s" % error)
        return None
    texts = [(item[0] or "").strip() for item in _boxes_of(detail)]
    for text in texts:
        m = re.search(r"\u53ef\u6311\u6218\u6b21\u6570\s*[\uff1a:]?\s*(\d+)", text)
        if m:
            return int(m.group(1))
    if any("挑战次数" in t for t in texts):
        for t in texts:
            if t.isdigit():
                return int(t)
    return None


def _jingjichang_left(context, frame) -> int | None:
    """读竞技场的「今日剩余挑战次数 N」。读不到返回 None。

    宁可返回 None 也不要瞎猜：之前在菜单/大地图上跑，roi 落在右侧活动区，
    里面随便一个 0 被当成次数，任务 1 秒就「次数用完」收工了。
    所以纯数字兜底必须先真的读到「挑战次数」这行才认。
    """
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_JJ_TIMES_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("竞技场读次数失败：%s" % error)
        return None
    texts = [(item[0] or "").strip() for item in _boxes_of(detail)]
    for text in texts:
        m = re.search(r"挑战次数\s*[：:]?\s*(\d+)", text)
        if m:
            return int(m.group(1))
    # 「今日剩余挑战次数」和数字被 OCR 拆成两段时，才允许取那个纯数字
    if any("挑战次数" in text for text in texts):
        for text in texts:
            if text.isdigit():
                return int(text)
    return None


def _jingjichang_tong(context, frame):
    """找最右那组旁边那颗「通」圆章，返回点击点（1147 基准）或 None。

    用模板匹配而不是固定 roi：那颗「通」的位置不固定，固定 roi 会打偏。
    roi 开得比第 5 组图标大一圈，把位置漂移的空间留出来。
    """
    path = os.path.join(_SHOP_ROOT, "resource", "base", "image",
                        "richanghuodong", "jingjichang_tong.png")
    try:
        tpl = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as error:                        # noqa: BLE001
        _log("竞技场读通模板失败：%s" % error)
        return None
    if tpl is None:
        return None
    kx = frame.shape[1] / 1147.0
    ky = frame.shape[0] / 720.0
    x, y, w, h = _JJ_TONG_ROI
    x, y = int(round(x * kx)), int(round(y * ky))
    w, h = int(round(w * kx)), int(round(h * ky))
    sub = frame[y:y + h, x:x + w]
    if sub.size == 0:
        return None
    tw = int(round(tpl.shape[1] * kx))
    th = int(round(tpl.shape[0] * ky))
    if tw > sub.shape[1] or th > sub.shape[0]:
        _log("竞技场：模板比搜索区大 tpl=%dx%d sub=%dx%d" % (tw, th, sub.shape[1], sub.shape[0]))
        return None
    scaled = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
    _b, _g, _r = cv2.split(tpl.astype(np.int16))
    matte = np.where((_g > 200) & (_r < 80) & (_b < 80), 0, 255).astype(np.uint8)
    matte = cv2.resize(matte, (tw, th), interpolation=cv2.INTER_NEAREST)
    res = cv2.matchTemplate(sub, scaled, cv2.TM_CCOEFF_NORMED, mask=matte)
    _mn, mx, _mp, mp = cv2.minMaxLoc(res)
    _log("竞技场找通：best=%.3f at=(%d,%d)" % (mx, x + mp[0], y + mp[1]))
    if mx < 0.70:
        _log("竞技场找通：分数不够（%.3f）" % mx)
        return None
    cx = (x + mp[0] + tw / 2.0) / kx
    cy = (y + mp[1] + th / 2.0) / ky
    _log("竞技场找通：点 (%.0f, %.0f)" % (cx, cy))
    return (cx, cy)


def _jingjichang_ready(context, frame) -> bool:
    """判断是不是真在竞技场界面。

    认「今日剩余挑战次数」这行字：只有竞技场主页有。
    不能认「竞技场」三个字——大地图菜单里的图标文字就叫竞技场，
    07:21 那次就是在菜单上把图标名当成了标题。
    """
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_JJ_READY_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("竞技场判界面失败：%s" % error)
        return False
    for item in _boxes_of(detail):
        text = (item[0] or "").strip()
        if "挑战次数" in text:
            _log("竞技场：看到「%s」，确认已进入" % text)
            return True
    return False


@AgentServer.custom_action("jingjichang_run")
class JingJiChangRun(CustomAction):
    """扫荡竞技场：一直点最右边那个通的圆圈，直到剩余挑战次数为 0。

    竞技场不需要处理结算页：点完通之后直接回到同一个界面，
    继续点下一个通就行。
    """

    def run(self, context, argv) -> bool:
        try:
            return self._run(context, argv)
        except Exception as error:                    # noqa: BLE001
            import traceback
            _log("竞技场：崩了 %s: %s" % (type(error).__name__, error))
            _log(traceback.format_exc().replace("\n", " | "))
            return True

    def _run(self, context, argv) -> bool:
        _log("竞技场：进入 run（box=%s）" % (list(getattr(argv, "box", []) or []),))
        rounds = 0
        for round_no in range(1, 200):
            _log("竞技场第%d轮：开始截屏" % round_no)
            try:
                frame = _grab_raw(context)
            except Exception as error:                # noqa: BLE001
                _log("竞技场：截屏异常 %s" % error)
                return True
            shape = getattr(frame, "shape", None)
            _log("竞技场第%d轮：帧 %s" % (round_no, shape))
            if frame is None or shape is None:
                _log("竞技场第%d轮：没拿到画面" % round_no)
                return True

            # 刚点完图标那一帧可能还是菜单，先等竞技场界面出来再判。
            # 认「今日剩余挑战次数」这行字——大地图菜单里没有它。
            if round_no == 1:
                wait_ms = 0
                for _ in range(10):                       # 最多等 5 秒
                    _log("竞技场：第 %d 次判界面" % (wait_ms // 500 + 1))
                    if _jingjichang_ready(context, frame):
                        break
                    time.sleep(0.5)
                    wait_ms += 500
                    try:
                        frame = _grab_raw(context)
                    except Exception as error:            # noqa: BLE001
                        _log("竞技场：等待中截屏异常 %s" % error)
                        break
                    if getattr(frame, "shape", None) is None:
                        break
                _log("竞技场：等界面加载 %dms 后开始判断" % wait_ms)
                if wait_ms >= 5000:
                    _log("竞技场：5 秒内没看到挑战次数，没进入竞技场")
                    return True

            left = _jingjichang_left(context, frame)
            _log("竞技场第%d轮：剩余挑战次数 %s" % (round_no, "读不到" if left is None else left))
            if left is not None and left <= 0:
                _log("竞技场：次数用完，收工（打了 %d 次）" % rounds)
                return True

            pt = _jingjichang_tong(context, frame)
            _log("竞技场第%d轮：通位置 %s" % (round_no, pt))
            if pt is None:
                _log("竞技场第%d轮：没找到通，收工" % round_no)
                return True
            wx, wy = int(round(pt[0])), int(round(pt[1]))
            _log("竞技场第%d轮：点通 @ (%d, %d)" % (round_no, wx, wy))
            try:
                context.tasker.controller.post_swipe(wx, wy, wx, wy, 80).wait()
            except Exception as error:                # noqa: BLE001
                _log("竞技场：点击失败 %s" % error)
                return True
            rounds += 1
            # 不再固定等 2 秒：一直取帧试，能重新认出「通」就往下走（最多等 2 秒）
            waited = 0
            while waited < 2000:
                time.sleep(0.15)
                waited += 150
                probe = _grab_raw(context)
                if probe is None or getattr(probe, "shape", None) is None:
                    continue
                if _jingjichang_tong(context, probe) is not None:
                    break
            _log("竞技场第%d轮：等了 %dms" % (round_no, waited))
        _log("竞技场：达到 200 轮上限，收工")
        return True


@AgentServer.custom_action("shop_run")
class ShopRun(CustomAction):
    """神秘商店整个购买流程，一次做完。

    为什么不用 pipeline 的 next/on_error 分支：MAA 在节点识别失败后会先逐个
    试 next 里的候选，而「回去补第一档」那条分支上的节点永远能命中，结果
    第二档永远轮不到——05:58 那次就是这么卡死在第一档的。

    性能：`set_screenshot_use_raw_size(True)` 之后截图会从 FramePool 的
    21ms 掉到 Win32 的 ~2 秒（06:15 那次每一步都是 2 秒），所以整个过程
    只在开头取一次原始尺寸帧来算缩放比，之后循环全用 1147 的快速帧。
    """

    def run(self, context, argv) -> bool:
        records: list[str] = []
        for round_no in range(1, 40):
            try:
                from script_task import capture
                frame = capture(context)
            except Exception as error:                    # noqa: BLE001
                _log("商店第%d轮：截图失败 %s" % (round_no, error))
                return self._finish(records)
            if frame is None or getattr(frame, "shape", None) is None:
                _log("商店第%d轮：没拿到画面" % round_no)
                return self._finish(records)
            left = _shop_left(context, frame)
            _log("商店第%d轮：剩余次数 %s" % (round_no, "读不到" if left is None else left))
            if left is not None and left <= 0:
                _log("商店：次数用完，收工")
                return self._finish(records)
            bought = False
            # 从第 5 档往回找：第 1 档买完会立刻补货，
            # 从前往后找的话它永远抢先，后面四档一次都轮不到。
            for tier in range(5, 0, -1):
                score, pt = _match_in(frame, _SHOP_BUY_ROI[tier], _SHOP_BUY_TPL)
                if pt is None:
                    continue
                # pt 是 1147 基准，post_swipe 也是 1147 基准（MAA 自己换算）
                cx, cy = int(round(pt[0])), int(round(pt[1]))
                name = ""
                try:
                    name = _shop_item_name(context, [cx - 30, cy + 20, 60, 20], frame)
                except Exception:                         # noqa: BLE001
                    pass
                label = "购买第%d档 %s" % (tier, name) if name else "购买第%d档" % tier
                if tier >= 4:
                    label = "**%s**" % label
                records.append(label)
                _gui(label)
                _log("商店：%s（%.3f）点击 (%d, %d)" % (label, score, cx, cy))
                try:
                    context.tasker.controller.post_swipe(cx, cy, cx, cy, 80).wait()
                except Exception as error:                # noqa: BLE001
                    _log("商店：点击失败 %s" % error)
                    return self._finish(records)
                time.sleep(0.55)
                bought = True
                break
            if not bought:
                _log("商店：五档都没有可买的了，收工")
                return self._finish(records)
        _log("商店：达到 40 轮上限，收工")
        return self._finish(records)

    @staticmethod
    def _finish(records: list[str]) -> bool:
        _write_shop_gui("\n".join(records) or "商店：没买到东西")
        return True


@AgentServer.custom_action("shop_buy")
class ShopBuy(CustomAction):
    """买一档：先看今日剩余购买次数，>0 才点识别框。

    每一档购买前都要重新读次数——次数可能在前面几档买完就到 0 了，
    再点下去只会白点。返回 False 让 pipeline 走 on_error 收工。
    """

    def run(self, context, argv) -> bool:
        left = _shop_left(context)
        _log("购买前次数检查：%s" % ("读不到" if left is None else left))
        if left is not None and left <= 0:
            _log("购买次数已用完，不买了")
            return False
        box = list(getattr(argv, "box", []) or [])
        if len(box) < 4:
            _log("购买：没拿到识别框")
            return False
        cx = int(box[0]) + int(box[2]) // 2
        cy = int(box[1]) + int(box[3]) // 2
        context.tasker.controller.post_click(cx, cy).wait()
        _log("购买：点击 (%d, %d)" % (cx, cy))
        params = _as_dict(getattr(argv, "custom_action_param", None))
        tier = int(params.get("tier") or 0)
        name = _shop_item_name(context, box)
        label = ("购买第%d档 %s" % (tier, name)) if name else ("购买第%d档" % tier)
        if tier >= 4:
            label = "**%s**" % label
        _write_shop_gui(label)
        _log("商品名：%s" % (name or "读不到"))
        time.sleep(0.4)
        return True


def _fg_left(context, frame) -> int | None:
    """读福果来面板上的「今日剩余奖励次数：N次」，读不到返回 None。"""
    if frame is None or getattr(frame, "shape", None) is None:
        return None
    try:
        detail = context.run_recognition_direct(
            "OCR", JOCR(roi=_scale_roi(_FG_TIMES_ROI, frame)), frame)
    except Exception as error:                        # noqa: BLE001
        _log("福果来读次数失败：%s" % error)
        return None
    texts = [(item[0] or "").strip() for item in _boxes_of(detail)]
    for text in texts:
        m = re.search(r"\u5956\u52b1\u6b21\u6570\s*[\uff1a:]?\s*(\d+)", text)
        if m:
            return int(m.group(1))
    # 文字和数字被 OCR 拆成两段时，才允许取那个纯数字
    if any("\u6b21\u6570" in text for text in texts):
        for text in texts:
            m = re.search(r"^(\d+)$", text)
            if m:
                return int(m.group(1))
    return None


@AgentServer.custom_action("fuguo_enter_check")
class FuguoEnterCheck(CustomAction):
    """点「进入副本」前先看剩余奖励次数。

    次数为 0 返回 False：pipeline 走 on_error 到结束节点，
    同时把「本日福果来已完成」推到 MFA 日志窗。
    读不到次数时按还有次数处理，宁可多跑一轮也不要漏跑。
    """

    def run(self, context, argv) -> bool:
        from script_task import capture

        meta = sys.argv[-1] if len(sys.argv) > 0 else ""
        del meta

        frame = capture(context)
        left = _fg_left(context, frame)
        _log("福果来次数：%s" % ("读不到" if left is None else left))
        if left is not None and left <= 0:
            print("info: [福果来] 本日福果来已完成", file=sys.stderr, flush=True)
            return False
        return True


@AgentServer.custom_action("shop_remaining")
class ShopRemaining(CustomAction):
    """读神秘商店「今日剩余购买次数 N」，返回 False 表示次数用完该收工。

    为什么不用 OCR 的 expected 直接匹配「0」：剩余次数是变量，写死一个值
    只能对上那一次。这里拿整行文字再正则抠数字，读不到时按「还有次数」处理
    （宁可多买一轮，也不要漏买）。
    """

    def run(self, context, argv) -> bool:
        try:
            from script_task import capture
            import re as _re

            frame = capture(context)
            detail = context.run_recognition("神秘商店_读次数", frame)
            text = ""
            for t, _b in _boxes_of(detail):
                if "购买次数" in t:
                    text = t
                    break
            if not text:
                for t, _b in _boxes_of(detail):
                    text = t
                    break
            m = _re.search(r"购买次数\s*[：:]?\s*(\d+)", text)
            if not m:
                m = _re.search(r"(\d+)", text)
            if not m:
                _log("购买次数：没读到数字（OCR=%r），按还有次数处理" % text)
                return True
            left = int(m.group(1))
            _log("购买次数：剩余 %d" % left)
            if left <= 0:
                _log("购买次数已用完，收工")
                return False
            return True
        except Exception as error:  # noqa: BLE001
            _log("购买次数：识别异常 %s，按还有次数处理" % error)
            return True


@AgentServer.custom_action("fuguo_check_score")
class FuguoCheckScore(CustomAction):
    """挑战成功页读「获得积分：NNN」：达标领奖，不达标回地图重跑一轮。"""

    def run(self, context: Context, argv) -> bool:
        image = getattr(argv, "image", None)
        score = None
        # run_recognition 必须给 ndarray：argv.image 是包装类型，
        # 传进去报 "value must be a numpy.ndarray"，不传又报缺参数。
        # 自己现抢一帧（script_task.capture 里就是 post_screencap 的 ndarray）。
        try:
            from script_task import capture

            frame = capture(context)
            detail = context.run_recognition("福果来_读积分", frame)
            _log("读积分：detail=%r frame=%s" % (type(detail).__name__, frame.shape))
        except Exception as error:  # noqa: BLE001
            _log("读积分调用失败：" + str(error))
            detail = None
        texts = [t for t, _b in _boxes_of(detail)]
        _log("读积分 OCR 全文：" + repr(texts))
        for text in texts:
            m = re.search(r"获得积分\s*[：:]?\s*(\d+)", text)
            if not m:
                m = re.search(r"积分\s*[：:]?\s*(\d{2,5})", text)
            if m:
                score = int(m.group(1))
                break
        if score is None:
            _log("结算页没读到积分，按不达标处理")
            score = 0
        target = "福果来_领取奖励" if score >= 600 else "福果来_重跑"
        line = "本局获得积分：%d（%s）" % (score, "达标，领奖" if score >= 600 else "不达 600，回地图重跑")
        _log(line)
        _write_gui(line)
        try:
            context.override_pipeline({"福果来_结算": {"next": [target]}})
        except Exception as error:  # noqa: BLE001
            _log("覆盖 next 失败：" + str(error))
            return score >= 600
        return True

@AgentServer.custom_action("voyage_click_icon")
class VoyageClickIcon(CustomAction):
    """点活动菜单里「航海寻宝」那颗图标。

    不用 OCR：这颗图标被金色高亮光晕罩着，黄字压在金光上读不出来
    （22:09:48 那次失败就是这原因）。它固定在第一行第五列，
    两种窗口尺寸下比例都是 (0.619, 0.187)，按比例点。
    """

    def run(self, context, argv) -> bool:
        try:
            from script_task import capture

            frame = capture(context)
            if frame is None or getattr(frame, "shape", None) is None:
                _log("点航海寻宝图标：截图失败")
                return False
            height, width = frame.shape[0], frame.shape[1]
            cx, cy = int(round(width * 0.619)), int(round(height * 0.187))
            _log("点航海寻宝图标 @ (%d, %d)" % (cx, cy))
            context.tasker.controller.post_click(cx, cy).wait()
            return True
        except Exception:  # noqa: BLE001
            _log("点航海寻宝图标失败：" + traceback.format_exc())
            return False


@AgentServer.custom_action("fuguo_click_arrow")
class FuguoClickArrow(CustomAction):
    """点工具栏那个展开/收起活动菜单的金色三角。

    不再用模板匹配：模板带着裁剪时的背景色，换一张地图（比如须弥山是浅蓝
    天空、正常野外是暗红岩石）分数就掉到阈值以下，而画面上别处的金色图标
    反而能过线，于是点歪。这个三角在所有地图上都在同一个相对位置，
    两种窗口尺寸实测都是 (0.750w, 0.076h)。
    """

    def run(self, context, argv) -> bool:
        try:
            from script_task import capture

            frame = capture(context)
            if frame is None or getattr(frame, "shape", None) is None:
                _log("点菜单箭头：截图失败")
                return False
            height, width = frame.shape[0], frame.shape[1]
            cx, cy = int(round(width * 0.750)), int(round(height * 0.076))
            _log("点菜单箭头 @ (%d, %d)" % (cx, cy))
            context.tasker.controller.post_click(cx, cy).wait()
            return True
        except Exception:  # noqa: BLE001
            _log("点菜单箭头失败：" + traceback.format_exc())
            return False


@AgentServer.custom_action("fuguo_find_icon")
class FuguoFindIcon(CustomAction):
    """在活动菜单里找那棵绿色树并点它。

    不用 OCR：菜单里高亮的图标外面罩着一圈金色光晕，黄字压在金光上，
    OCR 对比度不够读不出来；也不用模板匹配：模板尺寸固定、窗口尺寸会变，
    对不上就永远匹配不到。这棵树是画面里唯一「高饱和度的绿」，
    按颜色找连通块，两种窗口尺寸下都只有一个候选。
    """

    def run(self, context, argv) -> bool:
        try:
            return self._run_impl(context, argv)
        except Exception:  # noqa: BLE001
            _log("点图标失败：exception: " + traceback.format_exc())
            return False

    def _run_impl(self, context, argv) -> bool:
        from script_task import capture

        frame = capture(context)
        if frame is None or getattr(frame, "shape", None) is None:
            _log("点图标：截图失败")
            return False
        height, width = frame.shape[0], frame.shape[1]
        x0, y0 = int(width * 0.25), int(height * 0.10)
        roi = frame[y0:int(height * 0.78), x0:int(width * 0.82)]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (55, 150, 50), (80, 255, 190))
        count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        best = None
        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area < 600 or not (0.6 <= w / max(1, h) <= 1.8):
                continue
            if best is None or area > best[0]:
                best = (int(area), int(centroids[index][0]) + x0, int(centroids[index][1]) + y0)
        if best is None:
            _log("点图标：菜单里没找到绿色树")
            return False
        _log("点图标：绿色树 @ (%d, %d)，面积 %d" % (best[1], best[2], best[0]))
        context.tasker.controller.post_click(best[1], best[2]).wait()
        return True


@AgentServer.custom_action("fuguo_run")
class FuguoRun(CustomAction):
    """接果子的主循环：抓帧 -> 守护兽预警 -> 结算 -> 走位 -> 技能。"""

    def run(self, context: Context, argv) -> bool:
        try:
            return self._run_impl(context, argv)
        except Exception:  # noqa: BLE001
            _log("异常：\n" + traceback.format_exc())
            return False

    def _apply_direction(self, hwnd: int, holding: dict, direction: int) -> int:
        """把方向落到 A/D 按键上。

        每帧都重发一次 keydown：PostMessage 会丢包，之前只在状态变化时发，
        丢一次就一直「以为按着、其实没按」——日志里 direction=1 但十秒不动。
        """
        want = "D" if direction > 0 else ("A" if direction < 0 else None)
        for key in ("A", "D"):
            if key == want:
                _post(hwnd, _MOVE_KEYS[key], 1)
                holding[key] = True
            elif holding[key]:
                _post(hwnd, _MOVE_KEYS[key], 0)
                holding[key] = False
        return direction

    def _steer(self, hwnd: int, img, targets, holding: dict,
               target_x: int | None, reach_tol: float | None = None) -> int:
        """按果子位置移动，返回这一帧的方向。

        方向一旦选定就锁住：只要这一侧还有果子，就继续往那边走，
        不会因为对面新冒出一颗就掉头——那是「抽携」的根源。
        """
        if not targets or target_x is None:
            self._lock = 0
            self._last_dir = 0
            return self._apply_direction(hwnd, holding, 0)
        tol = VIS["reach_tolerance"] * (img.shape[1] / 1920.0) if reach_tol is None else reach_tol
        now = time.time()
        if self._lock != 0:
            ahead = [x for (x, _y, _k) in targets if (x - target_x) * self._lock > 0]
            behind = [t for t in targets if (t[0] - target_x) * self._lock < 0]
            if not ahead:
                self._lock = 0          # 这一侧没果子了
            elif min(abs(x - target_x) for x in ahead) <= tol:
                self._lock = 0          # 已经走到跟前，交给 _pick_direction 决定站不站
            elif behind and min(abs(t[0] - target_x) for t in behind) < min(abs(x - target_x) for x in ahead) * 0.6:
                self._lock = 0          # 反方向有明显更近的果子，别一条道走到黑
            elif now - self._lock_at > 0.8:
                self._lock = 0          # 锁太久了，重新算一次
        if self._lock != 0:
            direction = self._lock
        else:
            direction = _pick_direction(
                targets, target_x, img.shape[1], img.shape[0],
                self._last_dir, None, reach_tol,
            )
            if direction != 0:
                self._lock = direction
                self._lock_at = time.time()
        # 已经贴到屏幕边缘就别再往外走了，不然会一直贴着墙挤
        if target_x is not None:
            if direction > 0 and target_x > img.shape[1] * 0.94:
                direction = 0
            elif direction < 0 and target_x < img.shape[1] * 0.06:
                direction = 0
        self._last_dir = direction
        return self._apply_direction(hwnd, holding, direction)

    def _claim(self, ctrl, box) -> bool:
        x = box[0] + box[2] // 2
        y = box[1] + box[3] // 2
        _log("点领取奖励 (%d,%d)" % (x, y))
        try:
            ctrl.post_click(x, y).wait()
        except Exception as exc:  # noqa: BLE001
            _log("领取失败：%s" % exc)
            return False
        time.sleep(2.0)
        return True

    def _run_impl(self, context: Context, argv) -> bool:
        ctrl = context.tasker.controller
        hwnd = _find_hwnd()
        if not hwnd:
            _log("找不到游戏窗口")
            return False
        _log("窗口句柄 0x%X，开始接果子" % hwnd)

        # 切场景那一两秒画面是黑的，直接跑会在一张没有八戒的图上空转，
        # 表现就是「进去了但不动」。先等认出八戒（头巾）再正式开循环。
        wait_until = time.time() + 10.0
        while time.time() < wait_until:
            if context.tasker.stopping:
                _log("等待进场时被停止")
                return True
            probe = _grab(context)
            if probe is not None and _player_x(probe) is not None:
                _log("进场就绪，开始接果子")
                break
            time.sleep(0.2)
        else:
            _log("等了 10 秒仍没认出八戒，照常开始（可能这一局画面异常）")

        holding = {"A": False, "D": False}
        prev_fruits = None
        targets = []
        last_img_ts = 0.0
        last_player = None
        last_bag = None
        best_bag_w = 0
        max_bag_w = 0
        small_run = 0
        paused = False
        pause_started = 0.0
        i_check_at = 0.0
        tree_base = 0
        last_u = last_y = last_o = 0.0
        last_probe = 0.0
        last_report = 0.0
        started = time.time()
        self._lock = 0
        self._lock_at = 0.0
        self._last_dir = 0

        while True:
            now = time.time()
            if context.tasker.stopping:
                _log("收到停止请求")
                _release_all(hwnd)
                return True
            if now - started > VIS["round_timeout"]:
                _log("一局跑完（%.0fs），退出" % (now - started))
                _release_all(hwnd)
                return True

            img = _grab(context)
            if img is None:
                time.sleep(VIS["frame_sleep"])
                continue

            # ① 果子：每帧只做一次颜色检测，结果缓存给下一帧做跟踪比对
            dt = now - last_img_ts if last_img_ts else 0.0
            last_img_ts = now
            cur = _detect_fruits(img)
            targets = _fruit_targets(cur, prev_fruits, dt, img.shape[1])
            prev_fruits = cur
            # 树冠区（画面 40% 以上）的候选个数：摇树时不断有新果子从树上冒出，
            # 停住之后这个数就不再增长——用它确认「I 到底停没停住」
            tree_now = sum(1 for (_x, _y, _k) in cur if _y < img.shape[0] * 0.40)

            # ② 守护兽预警：决策窗口只有一两秒，得比走位先处理
            guard = _guard_warning(img)
            if guard and not paused:
                paused = True
                pause_started = now
                tree_base = tree_now
                i_check_at = now + 0.5
                _tap(hwnd, _SKILL_KEYS["I"], hold=0.06)
                _log("守护兽预警，按 I 停摇")
            elif paused and guard and now > i_check_at:
                # I 是「切换」键，按空了等于还在摇：0.5 秒后看树冠区还有没有新果子冒出
                i_check_at = now + 0.5
                if tree_now > tree_base:
                    _tap(hwnd, _SKILL_KEYS["I"], hold=0.06)
                    _log("停摇没生效（树冠还有新果子），补按一次 I")
                    tree_base = tree_now
            elif paused and not guard and now - pause_started >= VIS["guard_recover_delay"]:
                paused = False
                _tap(hwnd, _SKILL_KEYS["I"], hold=0.06)
                _log("泡泡消失，按 I 恢复摇树")
            elif paused and now - pause_started > VIS["pause_max"]:
                # 泡泡挂着不消失就会一直停摇，树不摇就没果子
                paused = False
                _tap(hwnd, _SKILL_KEYS["I"], hold=0.06)
                _log("暂停已超 %.0f 秒，强制恢复摇树" % VIS["pause_max"])



            # ③ 结算
            if now - last_probe > VIS["claim_probe_interval"]:
                last_probe = now
                if _find_success(context, img):
                    _log("看到挑战成功，这一局结束")
                    _release_all(hwnd)
                    return True
                if CLAIM_REWARD:
                    box = _find_claim(context, img)
                    if box and self._claim(ctrl, box):
                        _release_all(hwnd)
                        return True

            # ④ 走位
            px = _player_x(img)
            if px is None:
                px = last_player
            else:
                last_player = px
            bag = _bag_x(img, px)
            if bag:
                last_bag = bag
            use = bag or last_bag
            target_x, bag_w = use if use else (px, None)
            if bag_w:
                # U 撑大的袋子会随时间缩回去：变大立刻采纳，变小要连续 12 帧才承认
                if bag_w >= best_bag_w:
                    best_bag_w = bag_w
                    small_run = 0
                else:
                    small_run += 1
                    if small_run >= 12:
                        best_bag_w = bag_w
                        small_run = 0
                max_bag_w = max(max_bag_w, best_bag_w)
                bag_w = best_bag_w
            if bag_w:
                reach = max(30.0 * (img.shape[1] / 1920.0), bag_w * 0.5)
            else:
                reach = VIS["reach_tolerance"] * (img.shape[1] / 1920.0)

            direction = self._steer(hwnd, img, targets, holding, target_x, reach)
            if paused and direction == 0:
                # 停摇期间不再有新果子，但已经掉下来的还得接：
                # 先按果子走，走到没果子可接了再往中间靠，而不是直接发呆
                mid = img.shape[1] // 2
                if target_x is not None and abs(target_x - mid) > VIS["park_tol"]:
                    self._apply_direction(hwnd, holding, 1 if mid > target_x else -1)

            # ⑤ 技能：一帧最多放一个。_tap 要阻塞 hold 毫秒，
            # 连放三个就是 90ms 不动；而且有果子快落地时一律不放，接果子优先
            shrink = max_bag_w > 0 and bag_w < max_bag_w * 0.85
            urgent = any(y > img.shape[0] * 0.72 for (_x, y, _k) in targets)
            if not urgent:
                if (len(targets) >= VIS["many_fruit"] or shrink) and now - last_u > VIS["u_cooldown"]:
                    _tap(hwnd, _SKILL_KEYS["U"])
                    last_u = now
                    _log("放技能 U")
                elif not paused and direction != 0 and now - last_y > VIS["repeat_cooldown"]:
                    _tap(hwnd, _SKILL_KEYS["Y"])
                    last_y = now
                    _log("放技能 Y")
                elif not paused and now - last_o > VIS["repeat_cooldown"]:
                    _tap(hwnd, _SKILL_KEYS["O"])
                    last_o = now
                    _log("放技能 O")

            # ⑥ 状态行 10 秒一次，避免刷 GUI
            if now - last_report > 10.0:
                last_report = now
                _log(
                    "t=%.0fs 八戒x=%s 袋子x=%s 袋宽=%s 可接半径=%.0f 果子=%d 方向=%d 停摇=%s"
                    % (now - started, len(cur), target_x, bag_w, reach, len(targets), direction, paused)
                )
                _dump_debug_frame(img, px, target_x, reach, targets, now - started)

            time.sleep(VIS["frame_sleep"])
