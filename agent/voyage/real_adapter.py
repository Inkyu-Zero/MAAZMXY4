"""实机适配层：把真实游戏画面转成规则引擎认识的 frame 字典。

参考 `fupcode/zmxy4-treasure-hunt` 的做法：
  - dxcam 抓游戏窗口（DXGI，比 ImageGrab 快一个数量级）
  - 自带 YOLO ONNX 模型（weights/best.onnx）识别 player / enemy / bullet / item
  - 逻辑坐标 320x200，画面按 unit_pixels 换算到 30x19 格

先只做「看」：定位窗口、抓图、推理、把结果画出来存成 debug 图。
确认识别准了再接键控。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from nav import astar_grid as _astar_grid
import zm_nav as _zn
import zm_fight as _zfight
from nav import identify_room as _identify_room
from nav import walkable as _nav_walkable
from nav import path_step as _path_step

ROOT = Path(__file__).resolve().parent
WEIGHTS = ROOT / "weights" / "best.onnx"
DEBUG = ROOT / "debug" / "real"
DEBUG.mkdir(parents=True, exist_ok=True)

# 游戏逻辑坐标（zmxy4 的 constants.py）
MAZE_WIDTH = 320
MAZE_HEIGHT = 200
# 我们的模拟一屏 30x19 格，一个格子 32 逻辑像素
UNIT_PIXELS = 32
GRID_COLS = 30
GRID_ROWS = 19


# ---------------------------------------------------------------- 窗口定位


user32 = ctypes.windll.user32
# 必须先声明 DPI aware，否则 ClientToScreen 返回逻辑坐标（150% 缩放下比
# dxcam 抓到的物理像素小 1.5 倍），裁出来的框会整体偏移。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    user32.SetProcessDPIAware()


def list_windows(keyword: str = "造梦") -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """枚举标题含关键字的顶层窗口，返回 (hwnd, title, (l, t, r, b))。"""
    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if keyword in title and user32.IsWindowVisible(hwnd):
            rect = wt.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                pt = wt.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                found.append((int(hwnd), title, (pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom)))
        return True

    user32.EnumWindows(_cb, 0)
    return found


def focus_window(hwnd: int, settle: float = 0.35) -> None:
    """把游戏窗口拉到前台，否则截出来的是盖在上面的别的窗口。"""
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(settle)


# ---------------------------------------------------------------- YOLO 推理


class OnnxDetector:
    """照抄 zmxy4 的 OnnxModel：Stretch 到 800x512、免 NMS、坐标归一化。

    跟标准 YOLOv8 导出的模型不同，这个 best.onnx 是
    - 数据集用 Stretch（直接拉伸，不补边）训练
    - 导出成 end2end 免 NMS，输出 (N, 6) = x1,y1,x2,y2,score,class_id
    - 坐标为输入空间的像素值（800x512），使用方自己除以宽高归一化
    之前按 YOLOv8 的 (1, 4+nc, 8400) 解析，框全是错的。
    """

    def __init__(self, weight_path: Path, conf: float = 0.4):
        import ast

        import onnxruntime as ort

        self.session = ort.InferenceSession(str(weight_path), providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape
        self.conf = conf
        meta = self.session.get_modelmeta()
        names = meta.custom_metadata_map.get("names", "[]")
        class_names = ast.literal_eval(names)
        if isinstance(class_names, list):
            class_names = dict(enumerate(class_names))
        self.class_names: dict[int, str] = class_names
        end2end = meta.custom_metadata_map.get("end2end", "False").lower() == "true"
        if not end2end:
            print("警告：这个模型不是免 NMS 导出的，输出格式可能对不上")

    def __call__(self, image: np.ndarray) -> list[dict]:
        import cv2

        h, w = image.shape[:2]
        in_h, in_w = int(self.input_shape[2]), int(self.input_shape[3])
        resized = cv2.resize(image, (in_w, in_h), interpolation=cv2.INTER_LINEAR)
        blob = resized.transpose(2, 0, 1).astype(np.float32) / 255.0
        blob = np.expand_dims(blob, axis=0)

        outputs = self.session.run(None, {self.input_name: blob})
        detections = outputs[0][0]
        if detections.ndim != 2 or detections.shape[1] != 6:
            raise ValueError(f"免 NMS 模型应输出 (N, 6)，实际为 {detections.shape}")

        out = []
        for x1, y1, x2, y2, score, class_id in detections:
            if float(score) <= self.conf:
                continue
            # 模型输出是输入空间的像素坐标，先归一化再乘回原图尺寸
            px1, px2 = np.clip(np.array([x1, x2]) / in_w, 0, 1) * w
            py1, py2 = np.clip(np.array([y1, y2]) / in_h, 0, 1) * h
            if px2 <= px1 or py2 <= py1:
                continue
            out.append(
                dict(
                    label=self.class_names.get(int(class_id), str(int(class_id))),
                    score=float(score),
                    box=(float(px1), float(py1), float(px2 - px1), float(py2 - py1)),
                    center=(float((px1 + px2) / 2), float((py1 + py2) / 2)),
                )
            )
        return out


# ---------------------------------------------------------------- 小地图


# 小地图在右上角，是 3 行 x 4 列的格子；当前所在房间是高亮的那一格。
MINIMAP_TOP = 30
MINIMAP_LEFT = 1280
MINIMAP_H = 120
MINIMAP_W = 130


def read_minimap(image: np.ndarray) -> tuple[int, int] | None:
    """返回当前房间在小地图上的 (行, 列)；认不出来给 None。"""
    roi = image[MINIMAP_TOP:MINIMAP_TOP + MINIMAP_H, MINIMAP_LEFT:MINIMAP_LEFT + MINIMAP_W]
    if roi.size == 0:
        return None
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # 高亮格是接近纯白的方块，找它的重心再换算成格子序号
    ys, xs = np.where(gray > 200)
    if len(ys) < 40:
        return None
    cy = float(np.mean(ys)) / (MINIMAP_H / 3.0)
    cx = float(np.mean(xs)) / (MINIMAP_W / 4.0)
    row = max(0, min(2, int(cy)))
    col = max(0, min(3, int(cx)))
    return row, col


# ---------------------------------------------------------------- 找门


def water_mask(image: np.ndarray) -> np.ndarray:
    """海面掩码。

    战斗房是黄色海面（H 15~40），入口/空房是青蓝色海面（H 85~115）。
    只认黄色的话，蓝房里 find_doors 一个门都找不到、is_failed 还会误判成失败。
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hch, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    yellow = (hch > 15) & (hch < 40)
    blue = (hch > 85) & (hch < 115)
    return (yellow | blue) & (sat > 90) & (val > 120)


def find_doors(image: np.ndarray, min_px: int = 120) -> list[tuple[str, tuple, int]]:
    """扫描画面四条边，找出「黄色海面延伸到边缘」的缺口 = 门。

    返回 [(方向, 门中心的格子坐标), ...]，方向是 "上"/"下"/"左"/"右"。
    每间房的门开在哪边是随机的，所以必须先看一眼再决定往哪开。
    """
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hch, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    water = water_mask(image)
    # min_px 是按 1410 宽标定的；PrintWindow 抓到的是原生 940 宽，
    # 门宽会按比例缩小，所以这里跟着图宽一起缩放。
    min_px = max(24, int(min_px * (w / 1410.0)))

    def longest_run(mask_1d: np.ndarray) -> tuple[int, int]:
        best_len, best_start, cur_len, cur_start = 0, 0, 0, 0
        for i, flag in enumerate(mask_1d):
            if flag:
                if cur_len == 0:
                    cur_start = i
                cur_len += 1
                if cur_len > best_len:
                    best_len, best_start = cur_len, cur_start
            else:
                cur_len = 0
        return best_start, best_len

    bands = {
        "上": (water[8:40, :], w, 30),
        "下": (water[h - 40:h - 8, :], w, 19),
        "左": (water[:, 8:40].T, h, 0),
        "右": (water[:, w - 40:w - 8].T, h, 30),
    }
    found = []
    for name, (band, span, fixed) in bands.items():
        if band.size == 0:
            continue
        frac = band.mean(axis=0)
        start, length = longest_run(frac > 0.5)
        if length < min_px:
            continue
        center_px = start + length / 2.0
        if name in ("上", "下"):
            coord = (center_px / span * 30.0, fixed)
        else:
            coord = (fixed, center_px / span * 19.0)
        found.append((name, tuple(round(v, 2) for v in coord), int(length)))
    return found


def run_explore(detector: "OnnxDetector", box, args, hwnd: int = 0) -> int:
    """空房间：找门 -> 开过去 -> 看小地图白格有没有移动。"""
    key_for = {"上": "w", "下": "s", "左": "a", "右": "d"}

    image = grab_window(box, args.backend)
    mm_before = read_minimap(image)
    doors = find_doors(image)
    print(f"找到的门：{doors if doors else '（一条都没找到）'}")
    if not doors:
        print("四条边都没看到海面缺口，可能已经在战斗/过场画面里")
        return 1

    if args.save:
        vis = image.copy()
        for name, (cx, cy) in doors:
            px, py = int(cx / 30 * image.shape[1]), int(cy / 19 * image.shape[0])
            cv2.circle(vis, (px, py), 18, (0, 255, 0), 3)
            cv2.putText(vis, name, (px + 22, py + 8), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        cv2.imwrite(os.path.join("debug", "real", "doors.png"), vis)

    # 只试最宽的那个门（列表中第一个就是最宽的）
    direction, (gx, gy), _w = max(doors, key=lambda d: d[2])
    key = key_for[direction]
    seconds = args.drive_seconds or 4.5
    if hwnd and getattr(args, "focus", False):
        focus_window(hwnd, settle=0.15)
    kb = Keyboard()
    print(f"朝 {direction} 开 {seconds:.1f} 秒（按 {key}）……")
    try:
        kb._down(key)
        time.sleep(seconds)
    finally:
        kb.release_all()
    time.sleep(0.4)

    image = grab_window(box, args.backend)
    mm_after = read_minimap(image)
    print(f"小地图 {mm_before} -> {mm_after}" + ("  ← 换房间了" if mm_before != mm_after else "  （还在同一间）"))
    if args.save:
        cv2.imwrite(os.path.join("debug", "real", f"explore_{key}.png"), image)
    return 0


# ---------------------------------------------------------------- 画面 -> frame


# 9 方向必须和 `voyage/geometry.py:8` 的 MOVES 完全一致：
#   MOVES = [(0,0), (1,0), (-1,0), (0,1), (0,-1), (1,1), (1,-1), (-1,1), (-1,-1)]
# 即 0 停 / 1 右 / 2 左 / 3 下 / 4 上 / 5 右下 / 6 右上 / 7 左下 / 8 左上。
# 注意 y 轴向下，所以 move 4 是「上」（w）、move 3 是「下」（s）。
# control() 返回位移向量，按 sign 反查 move 编号（必须和 voyage/geometry.py 的 MOVES 一致）
_DELTA_TO_MOVE = {
    (1, 0): 1, (-1, 0): 2, (0, 1): 3, (0, -1): 4,
    (1, 1): 5, (1, -1): 6, (-1, 1): 7, (-1, -1): 8,
}
_DIR_KEYS = ("", "d", "a", "s", "w", "sd", "wd", "sa", "wa")


def read_obstacles(image: np.ndarray, dets: list[dict] | None = None,
                   top: int = 40, bottom_pad: int = 55, side: int = 12) -> np.ndarray:
    """从画面里把礁石读成 30x19 的 0/1 格子图。

    礁石的 HSV 是「暗 + 暖色」（实测 V≈80~110、H≈21~31），海面是亮的黄
    （V≈184~214）。敌船船身有棕木色（H≈20）会被误判成礁石，所以把 YOLO
    检出来的 player / enemy 框附近的格子排除掉。
    """
    h, w = image.shape[:2]
    play = image[top:h - bottom_pad, side:w - side]
    hsv = cv2.cvtColor(play, cv2.COLOR_BGR2HSV)
    hch, val = hsv[:, :, 0], hsv[:, :, 2]
    rock = (val < 170) & (hch < 60)
    # 四周的礁石墙是青蓝色（H≈100），用「暗+暖色」判不出来，于是船会
    # 一直往边界上顶、以为那里是空的。这里补一条：**没有水就不是路**。
    water = water_mask(play)
    ph, pw = play.shape[:2]
    cell_h = ph / float(GRID_ROWS)
    cell_w = pw / float(GRID_COLS)
    grid = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.uint8)
    for r in range(GRID_ROWS):
        y0, y1 = int(r * cell_h), int((r + 1) * cell_h)
        for c in range(GRID_COLS):
            x0, x1 = int(c * cell_w), int((c + 1) * cell_w)
            if rock[y0:y1, x0:x1].mean() > 0.42:
                grid[r][c] = 1
            elif water[y0:y1, x0:x1].mean() < 0.12:
                # 一整格几乎没水 → 是墙（含四周边界），不是能走的地方
                grid[r][c] = 1
    # 船身木色会被误判成礁石，按检测框清掉
    for d in dets or []:
        if d["label"] not in ("player", "enemy"):
            continue
        bx, by, bw, bh = d["box"]
        c0 = max(0, int((bx - side) / cell_w))
        c1 = min(GRID_COLS - 1, int((bx + bw - side) / cell_w))
        r0 = max(0, int((by - top) / cell_h))
        r1 = min(GRID_ROWS - 1, int((by + bh - top) / cell_h))
        grid[r0:r1 + 1, c0:c1 + 1] = 0

    # 静态地图里所有房间的可走区域都只到「行 1~17 / 列 1~28」，再外面是礁石墙。
    # 画面边缘那片看着是海、实际船撞不过去，所以在清完船框之后再把最外圈钉回墙
    # （顺序很关键：先钉的话，玩家自己站在边上时会被清格子那步又清开）。
    grid[0:2, :] = 1
    grid[17:19, :] = 1
    grid[:, 0:2] = 1
    grid[:, 28:30] = 1
    # 照 zmxy4 的 init_map_template：出口不是小缺口，而是一整条敞开带。
    #   map_template[exit_up:exit_down, :] = 0   → 行 0.40~0.54，左右边敞开
    #   map_template[:, exit_left:exit_right] = 0 → 列 0.46~0.57，上下边敞开
    # 不挖开的话「最外圈钉墙」会把出口一起封死，船只会顶墙。
    _ex_up, _ex_dn = int(round(GRID_ROWS * 0.40)), int(round(GRID_ROWS * 0.54))
    _ex_lf, _ex_rt = int(round(GRID_COLS * 0.46)), int(round(GRID_COLS * 0.57))
    grid[0:2, _ex_lf:_ex_rt] = 0
    grid[GRID_ROWS - 2:GRID_ROWS, _ex_lf:_ex_rt] = 0
    grid[_ex_up:_ex_dn, 0:2] = 0
    grid[_ex_up:_ex_dn, GRID_COLS - 2:GRID_COLS] = 0
    return grid


def frame_from_detections(
    dets: list[dict],
    image: np.ndarray,
    grid: np.ndarray | None = None,
    prev: dict | None = None,
) -> dict:
    """把 YOLO 检测结果转成规则引擎要的 frame 字典（坐标换成格子坐标）。"""
    h, w = image.shape[:2]
    sx = GRID_COLS / float(w)
    sy = GRID_ROWS / float(h)

    def to_cell(box):
        x, y, bw, bh = box
        return ((x + bw / 2) * sx, (y + bh / 2) * sy)

    players = [d for d in dets if d["label"] == "player"]
    enemies = [d for d in dets if d["label"] == "enemy"]
    bullets = [d for d in dets if d["label"] == "bullet"]

    if not players:
        # 玩家船被敌船/礁石挡住时 YOLO 会整个漏掉（实测常见）。
        # 上一层每帧都在动，硬抛异常等于这一帧什么都不做；
        # 改用上一帧的位置兜底，位置会漂一点但比「不知道自己在哪」强。
        _p = (prev or {}).get("player", {}).get("position")
        if not _p:
            raise RuntimeError("没识别到玩家船（且没有上一帧位置可兜底）")
        px, py = float(_p[0]), float(_p[1])
        stale_player = True
    else:
        px, py = to_cell(players[0]["box"])
        stale_player = False

    # 子弹速度：用前后两帧的位移估（没有上一帧就给零速度，躲不掉但也打不着）
    prev_bullets = (prev or {}).get("bullets", [])
    bullet_list = []
    for b in bullets:
        bx, by = to_cell(b["box"])
        vx = vy = 0.0
        best = None
        for pb in prev_bullets:
            d = (pb["position"][0] - bx) ** 2 + (pb["position"][1] - by) ** 2
            if d < 25.0 and (best is None or d < best[0]):
                best = (d, pb)
        if best is not None:
            vx = bx - best[1]["position"][0]
            vy = by - best[1]["position"][1]
        bullet_list.append(dict(position=[bx, by], velocity=[vx, vy], hostile=True))

    return dict(
        schema=3,
        frame=(prev or {}).get("frame", 0) + 1,
        time=time.time(),
        player=dict(
            position=[px, py],
            hp=float((prev or {}).get("player", {}).get("hp", 4.0)),
            hp_max=4.0,
            radius=0.28,
            facing=[1.0, 0.0],
            cooldown=0.0,
            speed=5.25,
            upgrades=dict(attack=1, speed=1, armor=1, cannon=1),
        ),
        room=dict(
            id=int((prev or {}).get("room", {}).get("id", 5102)),
            obstacles=tuple(tuple(int(v) for v in row) for row in (grid if grid is not None else np.zeros((GRID_ROWS, GRID_COLS), dtype=int))),
            cleared=not enemies,
            size=[GRID_COLS, GRID_ROWS],
            unit_pixels=UNIT_PIXELS,
        ),
        enemies=[
            dict(
                id=i,
                position=list(to_cell(e["box"])),
                hp=float(e.get("hp", 5.0)),
                boss=False,
                facing=[1.0, 0.0],
                fleeing=False,
            )
            for i, e in enumerate(enemies)
        ],
        bullets=bullet_list,
        applied_action=[0, 0],
    )


# ---------------------------------------------------------------- 键控


class Keyboard:
    """WASD + J。

    默认用 PostMessage 把按键直接投给游戏窗口 —— 这样游戏在后台、没有焦点时
    也能收到键（keybd_event 是往「当前前台窗口」发的，游戏一旦被盖住就全打在
    聊天框上，实测就是这个现象）。没给 hwnd 时才退回 keybd_event。
    """

    VK = {"w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44, "j": 0x4A}
    WM_KEYDOWN, WM_KEYUP = 0x100, 0x101

    def __init__(self, hwnd: int | None = None):
        import win32api  # noqa: F401  仅为确认可用

        self._api = win32api
        self._hwnd = hwnd
        self.held: set[str] = set()

    def _post(self, key: str, down: bool) -> bool:
        if not self._hwnd:
            return False
        import ctypes

        user32 = ctypes.windll.user32
        msg = self.WM_KEYDOWN if down else self.WM_KEYUP
        return bool(user32.PostMessageW(self._hwnd, msg, self.VK[key], 0))

    def _down(self, key: str) -> None:
        if not self._post(key, True):
            self._api.keybd_event(self.VK[key], 0, 0, 0)

    def _up(self, key: str) -> None:
        if not self._post(key, False):
            self._api.keybd_event(self.VK[key], 0, 2, 0)  # KEYEVENTF_KEYUP

    def hold(self, key: str) -> None:
        """直接按住某些方向键（赶路用）。key 可以是 "d" 也可以是 "ds"（斜向）。

        先把不在集合里的方向键松开，再补按缺的 —— 只对变化的键发事件，
        避免每帧重复发 keydown 导致游戏抖动。
        """
        want = set(key or "")
        for other in ("w", "a", "s", "d"):
            if other not in want and other in self.held:
                self._up(other)
                self.held.discard(other)
        for k in want:
            if k not in self.held:
                self._down(k)
                self.held.add(k)

    def set_fire(self, fire: bool) -> None:
        """只控制 J（开火）这一颗键，方向键不动。"""
        if fire and "j" not in self.held:
            self._down("j")
            self.held.add("j")
        elif not fire and "j" in self.held:
            self._up("j")
            self.held.discard("j")

    def apply(self, move: int, fire: bool) -> None:
        want = set(_DIR_KEYS[int(move)]) if move else set()
        for key in list(self.held - want):
            if key == "j":
                continue          # J 单独管，别被方向键的清理逻辑顺手松开
            self._up(key)
            self.held.discard(key)
        for key in want - self.held:
            self._down(key)
            self.held.add(key)
        # J 可以长按：想开火就按住不放，不想就松开。
        # 之前写成「按下 10 毫秒再松开」，太短了，游戏根本不认。
        if fire and "j" not in self.held:
            self._down("j")
            self.held.add("j")
        elif not fire and "j" in self.held:
            self._up("j")
            self.held.discard("j")

    def release_all(self) -> None:
        for key in list(self.held):
            self._up(key)
        self.held.clear()
        self._up("j")


def run_drive(detector: "OnnxDetector", box, args, hwnd: int = 0) -> int:
    """按住一个方向键开几秒，前后各截一帧，用来看「门」通不通。"""
    key, _, secs = args.drive.partition(":")
    key = key.strip().lower()
    if key not in ("w", "a", "s", "d"):
        print(f"--drive 方向只能是 w/a/s/d，收到 {key!r}")
        return 2
    seconds = float(secs or 2.0)

    image = grab_window(box, args.backend)
    dets = detector(image)
    before = None
    mm_before = read_minimap(image)
    try:
        fr = frame_from_detections(dets, image, prev=None)
        before = tuple(round(v, 2) for v in fr["player"]["position"])
    except Exception as exc:
        print(f"开船前没认出玩家：{exc}")

    if hwnd and getattr(args, "focus", False):
        # 只有显式 --focus 才置前；PostMessage 直接投给窗口，不需要前台
        focus_window(hwnd, settle=0.15)
    kb = Keyboard()
    print(f"按住 {key} 开 {seconds:.1f} 秒……")
    try:
        kb._down(key)
        time.sleep(seconds)
    finally:
        kb.release_all()

    time.sleep(0.25)
    image = grab_window(box, args.backend)
    dets = detector(image)
    after = None
    mm_after = read_minimap(image)
    try:
        fr = frame_from_detections(dets, image, prev=None)
        after = tuple(round(v, 2) for v in fr["player"]["position"])
    except Exception as exc:
        print(f"开船后没认出玩家：{exc}")

    print(f"玩家位置 {before} -> {after}")
    print(f"小地图房间 {mm_before} -> {mm_after}" + ("  ← 换房间了" if mm_before != mm_after else "  （同一间）"))
    if args.save:
        import cv2

        vis = image.copy()
        for d in dets:
            x, y, w, h = [int(v) for v in d["box"]]
            cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 0), 2)
        out = os.path.join("debug", "real", f"drive_{key}.png")
        cv2.imwrite(out, vis)
        print(f"     -> {out}")
    return 0


def run_control(detector: "OnnxDetector", box, args, hwnd: int = 0) -> int:
    """闭环：抓图 -> 检测 -> frame -> 规则引擎 -> 按键。"""
    from voyage.hybrid.tactics import TacticalRules

    rules = TacticalRules()
    kb = Keyboard()
    prev: dict | None = None
    t_end = time.time() + args.seconds
    step = 0
    errors = 0
    print(f"\n开始控制 {args.seconds:.0f} 秒（ESC 或 Ctrl+C 停）")
    try:
        while time.time() < t_end:
            t0 = time.time()
            try:
                image = grab_window(box, args.backend)
                dets = detector(image)
                frame = frame_from_detections(dets, image, prev=prev)
                prev = frame
                action = rules.baseline(frame)
                move, fire = int(action[0]), int(action[1])
                kb.apply(move, bool(fire))
                errors = 0
            except Exception as exc:
                errors += 1
                print(f"  step {step} 跳过：{exc}")
                kb.release_all()
                if errors >= 10:
                    return 1
            step += 1
            if step % 10 == 0:
                n_enemy = len((prev or {}).get("enemies", []))
                print(f"  step {step}  {1000*(time.time()-t0):4.0f}ms  敌={n_enemy}  action={action if errors==0 else '-'}")
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n手动中断")
    finally:
        kb.release_all()
        print(f"结束，共 {step} 步")
    return 0


# ---------------------------------------------------------------- 主流程


_PW_WARNED = False


def grab_window_printwindow(hwnd: int, w: int, h: int):
    """用 PrintWindow 直接问窗口要自己的画面。

    好处：窗口被别的窗口盖住、或在后台没有焦点，都能拿到它自己该显示的内容。
    dxcam 抓的是屏幕像素，窗口一被压住就抓成别人的画面，这时候用这个兜底。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hdc_win = user32.GetWindowDC(hwnd)
    if not hdc_win:
        raise RuntimeError("GetWindowDC 失败")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    # PW_CLIENTONLY(0x1) | PW_RENDERFULLCONTENT(0x2)
    ok = user32.PrintWindow(hwnd, hdc_mem, 3)
    if not ok:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
        raise RuntimeError("PrintWindow 失败（该窗口可能禁止抓取）")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h          # 负数 = 自上而下
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)

    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    rgb = arr[:, :, :3].copy()
    # 真实内容只占左上角一块，其余全黑（客户区被 DPI 放大后的空白），
    # 按非黑区域裁掉，否则格子换算会全错。
    mask = rgb.max(axis=2) > 8
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size and cols.size:
        rgb = rgb[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    return rgb


_CAMERA = None


_GRAB_HWND = None
_USE_PW = True


def grab_window(box: tuple[int, int, int, int], backend: str):
    """整屏抓一次再裁窗口。

    注意：dxcam 的 region 参数在这台机器上只生效了「尺寸」、没生效「偏移」
    （实测传 (590,294,1530,884) 抓回来的是左上角 940x590 的画面），
    所以改成整屏 grab + numpy 切片，稳。
    """
    global _CAMERA
    # pw = PrintWindow：直接问窗口要它自己的画面。
    # 窗口被别的窗口压住、或在后台没焦点时，dxcam 抓的是屏幕上盖住它的那个窗口，
    # 只有 PrintWindow 才拿得到游戏自己的内容，也就不用抢前台了。
    if _USE_PW and _GRAB_HWND:
        return grab_window_printwindow(_GRAB_HWND, box[2] - box[0], box[3] - box[1])

    import dxcam

    if _CAMERA is None:
        _CAMERA = dxcam.create(backend=backend) if backend != "auto" else dxcam.create()
    full = None
    for _ in range(6):
        full = _CAMERA.grab()
        if full is not None:
            break
        time.sleep(0.02)
    if full is None:
        raise RuntimeError("dxcam.grab 连续 6 次返回 None（画面未变化或捕获失败）")
    left, top, right, bottom = box
    h, w = full.shape[:2]
    left = max(0, min(left, w - 1)); right = max(left + 1, min(right, w))
    top = max(0, min(top, h - 1)); bottom = max(top + 1, min(bottom, h))
    return full[top:bottom, left:right].copy()


def run_auto(detector: "OnnxDetector", box, args, hwnd: int = 0) -> int:
    """有敌人就交给规则引擎打，没敌人就找门开过去。"""
    from voyage.hybrid.tactics import TacticalRules

    rules = TacticalRules()
    kb = Keyboard(hwnd)
    seconds = float(args.seconds)
    tick = 0.1
    total = int(seconds / tick)
    prev = None
    log = []
    explore_key = None
    explore_key = None
    explore_last = None
    explore_stuck = 0
    explore_flip = 0
    explore_path = None
    explore_room = None
    seg_sig = None
    explore_room = None
    explore_path = None
    explore_last_dir = None
    explore_last_pos = None
    explore_stuck = 0
    explore_side_flip = False
    explore_until = 0.0
    _zlast_dir = None
    _zprev_bullets = []
    try:
        _zfight.start(int(getattr(args, "map_index", 6)))
        print(f"仓库地图：第 {int(getattr(args, 'map_index', 6)) + 1} 套，"
              f"路线 {_zfight.ZM.route}", flush=True)
    except Exception as _exc:
        print(f"仓库地图初始化失败：{_exc}", flush=True)
    last_action = None
    last_pos = None
    stuck_frames = 0
    forced_until = 0.0
    forced_move = 0
    _zn.start(int(getattr(args, "map_index", 0) or 0))
    route = parse_route(getattr(args, "route", "") or "")
    route_index = 0
    if route:
        _names = {"w": "上", "s": "下", "a": "左", "d": "右"}
        print("固定路径：" + " -> ".join(_names.get(k, k) for k, _ in route), flush=True)
    for step in range(total):
        # 先确认还在副本里 —— 主城/背包/活动面板一律不按键。
        # 之前没有这道检查，在主城画面上把头像认成玩家船，对着它按了 280 帧。
        if step % 5 == 0:
            try:
                if not in_voyage(image):
                    kb.release_all()
                    if step % 20 == 0:
                        print(f"[{step:>3}] 不在副本里（海面太少），不按键", flush=True)
                    prev = None
                    step += 1
                    time.sleep(tick)
                    continue
            except Exception:
                pass
        # 注意：这里**不要**再周期性 focus_window()。
        # grab_window 走 dxcam 抓屏幕，只要窗口没被别的窗口盖住就能抓到；
        # 而每隔 2 秒把游戏抢到最前，等于用户在干别的事时一直被弹回游戏。
        # 需要的话只在开始时调一次（见 main()），循环里不再动窗口。
        image = grab_window(box, args.backend)
        if is_failed(image):
            fail_frames = locals().get("fail_frames", 0) + 1
            if fail_frames >= 3:
                print(f"[{step:>3}] 识别到「挑战失败」，停止", flush=True)
                kb.release_all()
                return 0
        else:
            fail_frames = 0
        dets = detector(image)
        labels = {}
        for d in dets:
            labels[d["label"]] = labels.get(d["label"], 0) + 1
        # 单帧漏检很常见（敌船被礁石挡一下、飞到画面边缘），
        # 但只要漏一帧就切去"找门"分支，敌人回来也不会再打了。
        # 所以连续 3 帧都没有敌人才算真空房。
        if labels.get("enemy", 0) > 0:
            empty_frames = 0
        else:
            empty_frames = locals().get("empty_frames", 0) + 1
        in_combat = empty_frames < 3
        if in_combat:
            if explore_key is not None:
                # 还在赶路时画面里冒出敌人 —— 说明已经进了新房间，这一段的
                # 「开过去」任务完成了，别等时间到，立刻转战斗。
                explore_key = None
                route_index += 1
                kb.release_all()
                try:
                    _zfight.bump_level()
                except Exception:
                    pass
                log.append(f"[{step:>3}] 撞见敌人，停止赶路，转战斗")
            # ---- 战斗：用本地规则引擎（voyage/hybrid/tactics.py）----
            # 仓库那套已切换掉：它依赖 320x200 坐标和 map_index，与实机对不上，
            # 而且错误只在外面包一层会一直踩同一个坑。
            # ---- 下面这段是旧的规则引擎，已被仓库代码取代，留着不删 ----
            try:
                _r = read_obstacles(image, dets)
                if int(_r.sum()) > 400:
                    # 570 格里 400+ 都是"礁石"，这不是正常房间（失败页/加载页/被遮挡）
                    print(f"[{step:>3}] 画面不像正常房间（礁石 {int(_r.sum())}/570），跳过", flush=True)
                    kb.release_all()
                    time.sleep(tick)
                    continue
                fr = frame_from_detections(dets, image, grid=_r, prev=prev)
            except Exception as exc:
                # 认不到玩家船时不能原地干等 —— 敌方不会等你想。
                # 用原始检测结果挑一个远离最近敌人的方向先动起来。
                m = escape_move(dets, image.shape)
                if m:
                    kb.apply(m, False)
                    if step % 5 == 0:
                        print(f"[{step:>3}] frame 构造失败（{exc}），先朝 {m} 躲开", flush=True)
                else:
                    print(f"[{step}] frame 构造失败：{exc}")
                time.sleep(tick)
                continue
            # 每 5 帧（0.5 秒）才重新规划一次，中间帧只「执行」同一个决策。
            # 之前每帧都调 baseline()（= 每帧 prepare + best_action），两个
            # 候选战术分数一接近就来回抢，船就变成「一会儿退一会儿贴脸」的抖。
            # 规则引擎本来的用法就是「决策周期里 prepare 一次，周期内只 control」。
            if step % 5 == 0 or last_action is None:
                try:
                    last_action = int(rules.baseline(fr))
                except Exception as exc:
                    print(f"[{step:>3}] 规则引擎规划失败（{exc}），先躲一步", flush=True)
                    last_action = None
            # control() 返回的是「位移向量」不是 move 编号（实测 [1 0] = 向右），
            # 之前 int() 硬转成 1，等于每帧都按「右」—— 这就是「一会儿撞上去」
            # 的真凶：不管规则引擎选什么，船都在往右怼。
            try:
                delta = np.asarray(rules.control(last_action, fr), dtype=float).reshape(-1)
            except Exception as exc:
                print(f"[{step:>3}] 规则引擎算不出动作（{exc}），先躲一步", flush=True)
                _m = escape_move(dets, image.shape)
                kb.apply(_m or 0, False)
                prev = fr
                time.sleep(tick)
                continue
            if delta.size >= 2 and (abs(delta[0]) > 1e-6 or abs(delta[1]) > 1e-6):
                dx = int(np.sign(delta[0]))
                dy = int(np.sign(delta[1]))
                move = _DELTA_TO_MOVE.get((dx, dy), 0)
            else:
                move = 0
            fire = (last_action % 2) == 1
            # 规则引擎有时会在没有目标时也输出「开火」动作，实机按下去就是
            # 对着墙放炮。场上没有敌人就一律不按 J。
            if not fr["enemies"]:
                fire = False

            # 规则引擎的开火闸门依赖「敌人冷却估计」，那套估计是拿子弹轨迹
            # 反推的；实机里 YOLO 抓到的子弹太稀疏，window["known"] 一直是
            # False，于是一枪都不开。实机改成按几何条件直接开火：敌人在
            # 射程内、且大致同轴（十字四向的判据），就按住 J。
            px, py = fr["player"]["position"]
            for e in fr["enemies"]:
                ex, ey = e["position"]
                dx, dy = ex - px, ey - py
                gap = (dx * dx + dy * dy) ** 0.5
                if gap > 8.5:
                    continue
                if abs(dy) < 1.2 and abs(dx) > 0.2:      # 同一水平线
                    fire = True
                    break
                if abs(dx) < 1.2 and abs(dy) > 0.2:      # 同一竖直线
                    fire = True
                    break

            # 实机帧里 room.obstacles 是空的（还不知道礁石在哪），规则引擎
            # 会一直往同一个方向怼，撞到石头后位置不动、键还按着。
            # 这里加一层「卡住就换向」的反应式补丁：连续 8 帧位移 < 0.25 格
            # 就当作撞墙，反着开 0.6 秒再说。
            now_pos = np.asarray(fr["player"]["position"], dtype=float)
            if last_pos is not None and float(np.linalg.norm(now_pos - last_pos)) < 0.25:
                stuck_frames += 1
            else:
                stuck_frames = 0
            last_pos = now_pos
            if stuck_frames >= 8:
                # 实测（probe_move.py）：按住一个方向 2 秒能跑 18~21 格，
                # 所以键是通的、船也快；卡住只发生在「正对着礁石顶」的时候。
                # 那就**先横着挪一格再回原方向**（贴着石头蹭过去），
                # 而不是八个方向乱转——乱转就等于原地打摆子。
                stuck_ticks = locals().get("stuck_ticks", 0) + 1
                _pick = choose_unstuck(
                    dets, image, read_obstacles(image, dets),
                    now_pos, move or 0,
                )
                _perp = {1: (3, 4), 2: (3, 4), 3: (1, 2), 4: (1, 2),
                         5: (7, 6), 6: (8, 5), 7: (5, 8), 8: (6, 7)}
                _pair = _perp.get(move or 3, (1, 2))
                forced_move = int(_pick) if _pick else _pair[stuck_ticks % 2]
                forced_until = time.time() + 0.45
                log.append(
                    f"[{step:>3}] 顶到石头（连续 {stuck_frames} 帧位移 <0.25 格），"
                    f"改往空的那边走 -> 移动{forced_move}"
                )
                stuck_frames = 0
            if time.time() < forced_until:
                move = forced_move
            # 规则引擎算完给「不动」时也要动 —— 实机帧里 obstacles 全 0、
            # 敌人血量硬编码，它经常选「移动0」站桩等死。
            if move == 0 and fr["enemies"]:
                m = escape_move(dets, image.shape, fr["player"]["position"])
                if m:
                    move = m

            # 用户：「不需要，会按照现在船头的朝向攻击」——
            # 所以开火不用把方向吸成四轴，直接按规则引擎给的方向走+打。
            # --- 战斗没完不许走（can_next_level 的语义：连续 6 帧空场才算清完）---
            _enemy_n = sum(1 for d in dets if d["label"] == "enemy")
            _bomb_n = sum(1 for d in dets if d["label"] == "bullet")
            if _enemy_n == 0 and _bomb_n == 0:
                _empty_run = locals().get("_empty_run", 0) + 1
            else:
                _empty_run = 0
            if _empty_run < _EMPTY_NEED:
                pass  # 还没清完，继续打

            # --- 贴脸就后退（保持距离）---
            _near_px = nearest_enemy_px(dets, image.shape)
            if _near_px and _near_px[0] < _MIN_DIST:
                move = away_move_from(_near_px[1], _near_px[2])
                fire = False
                if step % 10 == 0:
                    print(f"[{step:>3}] 离敌人 {_near_px[0]:.0f}px 太近 -> 退 移动{move}", flush=True)
            else:
                # --- 近身不许斜向贴脸 ---
                move = _take_no_diag_when_close(move, dets, image.shape)
            # --- 弹道上防抖 ---
            move = antishake(move)
            kb.apply(move, fire)
            prev = fr
            if step % 5 == 0:
                log.append(f"[{step:>3}] 敌={labels.get('enemy',0)} 动作=移动{move}{'开火' if fire else ''}")
        else:
            # 照抄 zmxy4：用它的 Map（320×200）+ maze_utils 的 A*，
            # 每 4 帧算一次「下一个点该往哪走」，然后按那个方向的键。
            _pp = _player_cell(dets, image)
            _cur = scene_sig(image)
            if seg_sig is None:
                seg_sig = _cur
            if float(np.mean(np.abs(_cur - seg_sig))) > 20.0:
                kb.release_all()
                print(f"[{step:>3}] 换房间了", flush=True)
                seg_sig = None
                explore_path = None
                _zn.bump_level()          # 照抄仓库：换房就 level+1，next_exit 跟着走
                if route and route_index < len(route):
                    route_index += 1
                time.sleep(tick)
                continue

            if _pp is None:
                if step % 20 == 0:
                    print(f"[{step:>3}] 没认到自己，先停一下", flush=True)
                time.sleep(tick)
                continue

            # 固定路径：**锁定一个方向一直开**，直到画面变成新房间。
            # 之前每帧问 Map.get_next_direction()，它的目标点每帧都在变
            # （level/next_exit 靠猜），船就在原地左右横跳永远出不去。
            if explore_key is None:
                if route_index < len(route):
                    explore_key = route[route_index][0]
                    print(
                        f"[{step:>3}] 空房，锁定方向 {explore_key}"
                        f"（第 {route_index + 1}/{len(route)} 段）",
                        flush=True,
                    )
                else:
                    explore_key = "s"      # 路径走完还没换房，继续往下探
                    print(f"[{step:>3}] 路径走完，继续往下探", flush=True)
            # 卡住检测：连着 8 帧位移 < 0.25 格就换垂直方向横挪。
            if explore_last is not None:
                _moved = abs(_pp[0] - explore_last[0]) + abs(_pp[1] - explore_last[1])
                explore_stuck = explore_stuck + 1 if _moved < 0.25 else 0
            explore_last = _pp
            if explore_stuck >= 8:
                _pair = {"w": "ad", "s": "ad", "a": "ws", "d": "ws"}.get(explore_key, "ad")
                _side = _pair[explore_flip % 2]
                explore_flip += 1
                explore_stuck = 0
                print(f"[{step:>3}] 卡住了（连续 8 帧没动），横着挪 {_side}", flush=True)
                kb.hold(_side)
            else:
                kb.hold(explore_key)
            if step % 20 == 0:
                print(
                    f"[{step:>3}] 赶路 我=({_pp[0]:.1f},{_pp[1]:.1f}) 键={explore_key} ",
                    flush=True,
                )
        for line in log:
            print(line, flush=True)
        log.clear()
        time.sleep(tick)
    kb.release_all()
    return 0


# 用户给的那张布局图上抄下来的 7 条路线（船所在格 → 白色 Boss 格）。
# 资料图存在 docs/route_map.jpg，逐格放大核对过。
ROUTE_PRESETS = {
    "3x3a": "left,up,right,right",
    "3x3b": "left,up,up,right",
    "3x4a": "down,right,down,right",
    "3x4b": "down,left",
    "3x5": "up,up,left,left",
    "4x3a": "right,right,up,up",
    "4x3b": "up,right,down,down",
}

_DIR_ALIAS = {
    "上": "w", "下": "s", "左": "a", "右": "d",
    "up": "w", "down": "s", "left": "a", "right": "d",
    "w": "w", "s": "s", "a": "a", "d": "d",
}


def parse_route(spec: str) -> list[tuple[str, float]]:
    """把路径解析成 [(按键, 秒数上限), ...]。

    两种写法都收：
      - "left,up,right,right"   ← 不写秒数（推荐）
      - "up:4,right:3"          ← 写了就用它当上限
    秒数只是「保险上限」，走路的真正终止条件是「画面变成新房间」——
    用户 m21336：「你管几秒干什么，识别到新地图就去战斗」。
    """
    raw = (spec or "").strip()
    if raw in ROUTE_PRESETS:
        raw = ROUTE_PRESETS[raw]
    steps: list[tuple[str, float]] = []
    for part in raw.replace("，", ",").replace("；", ",").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, sec = part.replace("：", ":").partition(":")
        key = key.strip().lower()
        key = _DIR_ALIAS.get(key, key)
        if key not in ("w", "s", "a", "d"):
            raise ValueError(f"不认识的方向 {part!r}（可用 up/down/left/right 或 上/下/左/右）")
        if not sec.strip():
            steps.append((key, 12.0))
            continue
        try:
            value = float(sec)
        except ValueError:
            raise ValueError(f"路径片段 {part!r} 的秒数不是数字") from None
        if value <= 0:
            raise ValueError(f"路径片段 {part!r} 的秒数必须大于 0")
        steps.append((key, value))
    return steps


_MOVE_DELTA = {1: (1, 0), 2: (-1, 0), 3: (0, 1), 4: (0, -1),
               5: (1, 1), 6: (1, -1), 7: (-1, 1), 8: (-1, -1)}


def choose_unstuck(dets, image, rock, player_cell, cur_move=0):
    """顶到石头时该往哪挪。

    规则：优先「远离最近敌人」，但**必须走得过**——按礁石图检查前方 2~3 格
    是不是空的，被堵就依次试「垂直两侧 → 反方向」。比随机横挪靠谱，
    也比八个方向乱转稳（乱转等于原地打摆子）。
    """
    if player_cell is None:
        return None
    px, py = float(player_cell[0]), float(player_cell[1])
    h, w = rock.shape[:2]
    sx = w / float(GRID_COLS)
    sy = h / float(GRID_ROWS)

    def free(mv: int) -> bool:
        dx, dy = _MOVE_DELTA.get(int(mv), (0, 0))
        if dx == 0 and dy == 0:
            return False
        for k in (1.5, 2.5, 3.5):
            cx, cy = int(round(px + dx * k)), int(round(py + dy * k))
            if cx < 1 or cy < 1 or cx >= GRID_COLS - 1 or cy >= GRID_ROWS - 1:
                return False
            if rock[cy][cx]:
                return False
        return True

    # 1) 先试「远离最近敌人」那个方向
    want = escape_move(dets, image.shape, [px, py])
    trials: list[int] = []
    if want:
        trials.append(int(want))
    # 2) 再试垂直两侧
    for mv in (1, 2, 3, 4):
        if mv not in trials:
            trials.append(mv)
    # 3) 最后反向
    if cur_move:
        trials.append(int(cur_move))
    for mv in trials:
        if free(mv):
            return mv
    return trials[0] if trials else None


def _side_key(side: str) -> str:
    """方位 → 方向键。"""
    return {"上": "w", "下": "s", "左": "a", "右": "d"}.get(side, "s")


def nav_door_cell_guess(side: str):
    """一侧的默认门口格（静态表里四周封闭，靠这个给 A* 一个目标点）。"""
    # 目标点必须落在上面挖开的出口带里（列 14~17 / 行 7~10）
    return {"上": (15, 1), "下": (15, 17), "左": (1, 9), "右": (28, 9)}.get(side)


# 与 read_obstacles 裁剪可玩区域用的同一组边距 —— 两个坐标系必须一致，
# 否则 A* 的起点和礁石网格差一格，算出来的路是穿墙的，船就走不动。
PLAY_TOP, PLAY_BOTTOM_PAD, PLAY_SIDE = 40, 55, 12


def _player_cell(dets, image):
    """从 YOLO 检测结果算玩家格子坐标（与 read_obstacles 同一坐标系）。"""
    h, w = image.shape[:2]
    pw = max(1, w - 2 * PLAY_SIDE)
    ph = max(1, h - PLAY_TOP - PLAY_BOTTOM_PAD)
    for d in dets:
        if d["label"] == "player":
            x, y, bw, bh = d["box"]
            cx = (x + bw / 2) - PLAY_SIDE
            cy = (y + bh / 2) - PLAY_TOP
            return (cx / pw * GRID_COLS, cy / ph * GRID_ROWS)
    return None


def _axis_only(move: int) -> int:
    """把八方向吸附到四轴（攻击只有四向，船头必须是横或竖）。"""
    return {5: 1, 6: 1, 7: 2, 8: 2}.get(int(move), int(move))


# ================================================================ 战斗侧的四个补丁
# 这些都是在 run_zmxy4.py 里验收过的，切回本地引擎时丢了，这里原样搬回来。
_MIN_DIST = 110.0          # 像素，约 10 格内算「贴脸」
_DANGER_PX = 60.0          # 近身压斜向的阈值
_SWITCH_COOLDOWN = 0.25    # 方向反向冷却
_EMPTY_NEED = 6            # 连续几帧空场才算清完
_OPPOSITE = {1: 2, 2: 1, 3: 4, 4: 3, 5: 8, 8: 5, 6: 7, 7: 6}
_dir_switch = {"t": 0.0, "dir": 0}


def nearest_enemy_px(dets, image_shape):
    """返回 (距离像素, dx, dy) —— dx/dy 是「玩家减去敌人」，正数表示敌人在左/上。"""
    h, w = image_shape[:2]
    players = [d for d in dets if d["label"] == "player"]
    enemies = [d for d in dets if d["label"] == "enemy"]
    if not players or not enemies:
        return None
    px, py = players[0]["box"][0] + players[0]["box"][2] / 2, players[0]["box"][1] + players[0]["box"][3] / 2
    best = None
    for e in enemies:
        ex, ey = e["box"][0] + e["box"][2] / 2, e["box"][1] + e["box"][3] / 2
        d = ((px - ex) ** 2 + (py - ey) ** 2) ** 0.5
        if best is None or d < best[0]:
            best = (d, px - ex, py - ey)
    return best


def away_move_from(dx: float, dy: float) -> int:
    """朝远离敌人的方向给一个 move 编号（1右 2左 3下 4上）。"""
    if abs(dx) >= abs(dy):
        return 1 if dx > 0 else 2
    return 3 if dy > 0 else 4


def antishake(move: int) -> int:
    """0.25 秒内不许反向，避免在弹道上来回抖。"""
    now = time.time()
    prev = _dir_switch["dir"]
    if prev and move and _OPPOSITE.get(move) == prev and now - _dir_switch["t"] < _SWITCH_COOLDOWN:
        return prev
    if move and move != prev:
        _dir_switch["t"] = now
        _dir_switch["dir"] = move
    return move


def _take_no_diag_when_close(move: int, dets, image_shape) -> int:
    """近身时把斜向压成单轴：先查墙，再选远离敌人的那根轴。"""
    if move not in (5, 6, 7, 8):
        return move
    info = nearest_enemy_px(dets, image_shape)
    if not info or info[0] >= _DANGER_PX:
        return move
    _d, dx, dy = info
    cand_h = 1 if dx > 0 else 2
    cand_v = 3 if dy > 0 else 4
    return cand_h if abs(dx) >= abs(dy) else cand_v


def escape_move(dets: list[dict], shape, player_cell=None) -> int | None:
    """从原始检测结果里挑一个「远离最近敌人」的方向。

    规则引擎的 frame 构造需要玩家船被认出来；实机里玩家船常被敌船挡住，
    frame 一构造失败整帧就什么都不做——看起来就是「进新房间站着等死」。
    这个函数不依赖 frame，只要有敌人坐标就能给出一个躲的方向。
    """
    h, w = shape[:2]
    px = py = None
    enemies: list[tuple[float, float]] = []
    for d in dets:
        x, y, bw, bh = d["box"]
        cx = (x + bw / 2) / w * GRID_COLS
        cy = (y + bh / 2) / h * GRID_ROWS
        if d["label"] == "player":
            px, py = cx, cy
        elif d["label"] == "enemy":
            enemies.append((cx, cy))
    if player_cell is not None:
        px, py = float(player_cell[0]), float(player_cell[1])
    if px is None or not enemies:
        return None
    ex, ey = min(enemies, key=lambda e: (e[0] - px) ** 2 + (e[1] - py) ** 2)
    dx, dy = px - ex, py - ey
    if abs(dx) >= abs(dy):
        return 1 if dx > 0 else 2      # 往右 / 往左
    return 3 if dy > 0 else 4          # 往下 / 往上


def scene_sig(image: np.ndarray) -> np.ndarray:
    """把画面压成 64x40 灰度，用来判断「换房间了没有」。

    换房间时整张图会大变（海面颜色、礁石布局、顶部血条全换），
    而同房间内只有船和子弹在动，整图差异很小，阈值能把两者分开。
    """
    small = cv2.resize(image, (64, 40), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)


def in_voyage(image: np.ndarray) -> bool:
    """这帧像不像航海副本的房间。

    主城/活动面板/背包这些画面几乎没有成片的蓝/黄海面，
    用海面占比一刀切。低于阈值就叫它「不在副本」，一律不按键。
    """
    h, w = image.shape[:2]
    play = image[int(0.07 * h):int(0.90 * h), int(0.03 * w):int(0.97 * w)]
    if play.size == 0:
        return False
    sea = water_mask(play)
    return float(sea.mean()) > 0.25


def is_failed(image: np.ndarray) -> bool:
    """判断是不是「挑战失败」结算页。

    失败页整屏是灰白底 + 中间一块深色边框文字，且没有海面那种高饱和黄色。
    用两个稳定特征：① 画面几乎没有黄色海面；② 中央区域大面积灰白。
    """
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hch, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    sea = water_mask(image)
    if float(sea.mean()) > 0.10:
        return False
    # 失败/结算页是灰底：既没有海面、整张图也几乎没有颜色。
    # 正常房间即使礁石再密，海面也总有一块是有颜色的。
    if float(sat.mean()) < 45.0:
        return True
    mid = image[int(h * 0.25):int(h * 0.75), int(w * 0.15):int(w * 0.85)]
    gray = cv2.cvtColor(mid, cv2.COLOR_BGR2GRAY)
    bright = float((gray > 150).mean())
    if bright <= 0.35:
        return False
    # 关键补充：礁石很密的房间海面占比也会掉到 0.10 以下，
    # 光看上面两条会把「石头多的普通房间」误判成失败页（实测把船吓得当场停车）。
    # 失败页是**灰白无彩**的，礁石房的海水仍然高饱和 —— 用整屏平均饱和度分开。
    return float(sat.mean()) < 70.0


def click_at(box: tuple[int, int, int, int], fx: float, fy: float,
             hwnd: int | None = None) -> None:
    """在游戏画布里按相对坐标点一下（fx/fy 是 0~1）。

    走 PostMessage 投递给窗口，不用移动鼠标、也不需要窗口在前台
    （SetCursorPos + mouse_event 是发给「屏幕这个位置上最上层的窗口」，
    游戏一旦被别的窗口盖住就点到别人身上了，实测点了没反应）。
    """
    import ctypes

    # 窗口客户区被 DPI 放大到 1410x885，但游戏只画在左上 940x590 里，
    # 所以相对坐标要按 940x590 折算，不然点到黑边上去。
    x = int(940 * fx)
    y = int(590 * fy)
    if hwnd:
        # 用户明确要求：不许动他的光标。
        # 这里只发 PostMessage，绝不动 SetCursorPos（游戏可能不响应，
        # 但宁可不响应也不能抢光标 —— 需要真点的时候让用户自己点）。
        u = ctypes.windll.user32
        lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        u.PostMessageW(hwnd, 0x200, 0, lp)      # WM_MOUSEMOVE
        u.PostMessageW(hwnd, 0x201, 1, lp)      # WM_LBUTTONDOWN
        u.PostMessageW(hwnd, 0x202, 0, lp)      # WM_LBUTTONUP
        print(f"点击游戏画布 ({x},{y})（虚拟输入，不动光标）")
        return
    # 没有 hwnd 时不再用 SetCursorPos 真点 —— 用户明确要求不许动光标。
    print(f"没有窗口句柄，跳过点击（不动光标）。请在游戏里手动点 ({fx:.2f},{fy:.2f})")


def main() -> int:
    parser = argparse.ArgumentParser(description="实机适配：识别游戏画面（不控）")
    parser.add_argument("--keyword", default="造梦", help="窗口标题关键字")
    parser.add_argument("--conf", type=float, default=0.4)
    parser.add_argument("--frames", type=int, default=5, help="抓几帧做统计")
    parser.add_argument("--backend", default="auto", choices=["auto", "dxgi", "winrt"])
    parser.add_argument(
        "--grab",
        default="pw",
        choices=["pw", "screen"],
        help="截图来源：pw=PrintWindow（默认，窗口被盖住也能抓）；screen=抓屏幕（dxcam）",
    )
    parser.add_argument("--save", action="store_true", help="把带框的画面存到 debug/real/")
    parser.add_argument("--focus", action="store_true",
                        help="启动时把游戏窗口置前（默认不置前，避免反复抢焦点）")
    parser.add_argument("--no-focus", action="store_true", help="（已废弃，默认就不置前）")
    parser.add_argument(
        "--route",
        help='固定行进路径：写方向就行，不用写秒数，如 "left,up,right,right" 或 "左,上,右,右"。'
        " 也可以直接给布局名：3x3a / 3x3b / 3x4a / 3x4b / 3x5 / 4x3a / 4x3b（见 docs/route_map.jpg）。"
        " 规则：朝这一格方向一直开，画面变成新房间（或冒出敌人）就停下转战斗，不看门、不猜路。",
    )
    parser.add_argument("--map-index", type=int, default=6, help="仓库的第几套大地图（0~6），默认6")
    parser.add_argument("--click", default="", help="点窗口内相对坐标，如 0.39,0.78")
    parser.add_argument("--auto", action="store_true", help="自动驾驶：有敌人就战斗，没敌人就找门")
    parser.add_argument("--control", action="store_true", help="真发键（WASD+J）；默认只看不动")
    parser.add_argument("--seconds", type=float, default=20.0, help="--control 下跑多少秒")
    parser.add_argument("--show-frame", action="store_true", help="即使不发键，也把构造成的 frame 打印出来")
    parser.add_argument("--explore", action="store_true", help="空房间：找门并开过去，看小地图白格变不变")
    parser.add_argument("--drive-seconds", type=float, default=0.0, help="探索时按住方向键的秒数（默认 4.5）")
    parser.add_argument(
        "--drive",
        default="",
        help="按住方向键开 N 秒试探，格式 'w:3'（方向 w/a/s/d，秒数）；用来验证「门」在哪",
    )
    args = parser.parse_args()

    wins = list_windows(args.keyword)
    if not wins:
        print(f"没找到标题含「{args.keyword}」的窗口。当前可见窗口里带「造梦」的一个都没有。")
        return 1
    for hwnd, title, box in wins:
        w, h = box[2] - box[0], box[3] - box[1]
        print(f"hwnd={hwnd}  {title}  客户区 {w}x{h}  box={box}")

    hwnd, title, box = max(wins, key=lambda r: (r[2][2] - r[2][0]) * (r[2][3] - r[2][1]))
    print(f"\n选中：{title}")
    global _GRAB_HWND, _USE_PW
    # --grab pw 时 grab_window 需要 hwnd 才能调 PrintWindow
    global _CAMERA  # noqa: F824  (占位，避免误删)
    _USE_PW = getattr(args, "grab", "pw") == "pw"
    _GRAB_HWND = hwnd if _USE_PW else None
    # 默认**不**抢前台。用户明确要求过：不要一次次把游戏窗口拉到最前面。
    # dxcam 抓的是屏幕像素，只要窗口没被别的窗口盖住就能抓，不需要在前台。
    if args.focus:
        focus_window(hwnd)
        wins = [w for w in list_windows(args.keyword) if w[0] == hwnd]
        if wins:
            box = wins[0][2]
        print(f"已置前，box={box}")

    if not WEIGHTS.exists():
        print(f"缺少模型：{WEIGHTS}")
        return 1
    detector = OnnxDetector(WEIGHTS, conf=args.conf)
    print(f"模型输入 {detector.input_shape[3]}x{detector.input_shape[2]}，类别：{detector.class_names}")

    if args.click:
        cx, cy = (float(v) for v in args.click.split(","))
        click_at(box, cx, cy, hwnd)
        return 0

    if args.explore:
        return run_explore(detector, box, args, hwnd)

    if args.drive:
        return run_drive(detector, box, args, hwnd)

    if args.control:
        return run_control(detector, box, args, hwnd)
    if args.auto:
        return run_auto(detector, box, args, hwnd)

    prev: dict | None = None
    for i in range(args.frames):
        t0 = time.perf_counter()
        image = grab_window(box, args.backend)
        t1 = time.perf_counter()
        dets = detector(image)
        t2 = time.perf_counter()
        by_label: dict[str, int] = {}
        for d in dets:
            by_label[d["label"]] = by_label.get(d["label"], 0) + 1
        print(
            f"[{i}] grab {1000*(t1-t0):5.1f}ms  infer {1000*(t2-t1):6.1f}ms  "
            f"{image.shape[1]}x{image.shape[0]}  {by_label}"
        )
        if args.show_frame:
            try:
                fr = frame_from_detections(dets, image, prev=prev)
                prev = fr
                pp = fr["player"]["position"]
                print(
                    "     frame player=(%.2f,%.2f) enemies=%s bullets=%s"
                    % (
                        pp[0], pp[1],
                        [(round(e["position"][0], 2), round(e["position"][1], 2), e["hp"]) for e in fr["enemies"]],
                        [(round(b["position"][0], 2), round(b["position"][1], 2),
                          round(b["velocity"][0], 2), round(b["velocity"][1], 2)) for b in fr["bullets"]],
                    )
                )
            except Exception as exc:
                print("     frame 构造失败：%s" % exc)

        if args.save:
            import cv2

            vis = image.copy()
            for d in dets:
                x, y, w, h = [int(v) for v in d["box"]]
                cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.putText(
                    vis,
                    f'{d["label"]} {d["score"]:.2f}',
                    (x, max(12, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 255, 0),
                    1,
                )
            out = DEBUG / f"det_{i:02d}.png"
            cv2.imwrite(str(out), vis)
            print(f"     -> {out}")
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
