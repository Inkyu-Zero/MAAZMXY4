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
import re
import time
import traceback

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

_HERE_LOG = None

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
_RES_READY = [False]


def _grab(ctx: Context) -> np.ndarray | None:
    """抓一帧 BGR。MFA 给的是「画布」，游戏内容只贴在左上，黑边先裁掉。"""
    ctrl = ctx.tasker.controller
    if not _RES_READY[0]:
        try:
            ctrl.set_screenshot_use_raw_size(True)
        except Exception:  # noqa: BLE001
            pass
        _RES_READY[0] = True
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
    """看「进入副本」按钮是黄还是灰，黄就进，灰就报本日已完成。"""

    def run(self, context: Context, argv) -> bool:
        try:
            from script_task import capture

            box = None
            frame = None
            for attempt in range(3):
                if attempt:
                    time.sleep(0.6)
                frame = capture(context)
                detail = context.run_recognition("福果来_读进入副本", frame)
                for text, b in _boxes_of(detail):
                    if "进入副本" in text:
                        # 抠字框可能落在面板的暗色背景上（B=G=R≈96），
                        # 那样算出来的「黄底占比」永远是 0，会误判成本日已完成。
                        # 真按钮至少 100×30，太小的框直接当没找到。
                        if b[2] >= 80 and b[3] >= 24:
                            box = b
                            break
                if box is not None:
                    break
            if box is None:
                _log("没找到「进入副本」按钮（面板没打开？）")
                return False
            target = "福果来_本日已完成" if _button_is_gray(frame, box) else "福果来_进入副本"
            _log("进入副本 -> " + target)
            # 正在执行的是「检查进入副本」这个节点，覆盖它的 next，
            # 覆盖「点图标」的没用——那个节点早就走完了。
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
