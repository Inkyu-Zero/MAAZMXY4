"""从小地图读「第几套大地图」。

仓库原来的做法（game_window.get_map_index_at_start）只看小白块的归一化坐标，
7 套地图各写一个点，位置一撞就判错。这里按用户的要求换成两步：
  1. 先数小地图是几行 × 几列
  2. 再看小白块落在第几行第几列
再和 scenario.json 里 7 套 layouts 的 rooms 矩阵比对，形状唯一就能定下来。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

_DATA = json.loads((Path(__file__).resolve().parent / "data" / "scenario.json").read_text("utf-8"))
LAYOUTS = _DATA["layouts"]

# scenario.json 里 layouts 的顺序，和仓库 Map 的 route_list / tasks_list 的索引**不是同一个**。
# 把 7 条 route 按两种方向编码在 7 套 layouts 的 rooms 上各走一遍，只有 B 编码能一一对上，
# 得到下面这张映射表（route 索引 -> layouts 索引）：
#   route0 -> layouts5、route1 -> layouts2、route2 -> layouts6、route3 -> layouts0
#   route4 -> layouts3、route5 -> layouts4、route6 -> layouts1
# 反过来就是 layouts 索引 -> route/task 索引。
LAYOUT_TO_MAP = {5: 0, 2: 1, 6: 2, 0: 3, 3: 4, 4: 5, 1: 6}

W = H = 0


def _grid_bbox(crop):
    """在小地图 ROI 里找「灰格子阵列」的外接框（海面是高饱和蓝，容器是更暗的圆）。"""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    m = ((sat < 95) & (val > 35) & (val < 235)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    cand = sorted(((stats[i, 4], i) for i in range(1, n) if stats[i, 4] > 800), reverse=True)
    if not cand:
        return None
    i = cand[0][1]
    return int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]), int(stats[i, 3])


def _seams(profile, margin=5):
    """在一维灰度曲线上找暗缝（网格线）。距两端 margin 内的不算，避免把 bbox 边缘当线。"""
    out = []
    for i in range(margin, len(profile) - margin):
        if profile[i] < profile[i - 1] and profile[i] <= profile[i + 1] and profile[i] < profile.mean() * 0.8:
            out.append(i)
    # 合并挨得太近的（同一根粗线）
    merged = []
    for v in out:
        if not merged or v - merged[-1] > 4:
            merged.append(v)
    return merged


def read_map_id(image, roi=(0.86, 0.0, 1.0, 0.22)):
    """返回 (map_index, info)。info 里有 rows/cols/白块位置/候选。"""
    h, w = image.shape[:2]
    x0, x1 = int(roi[0] * w), int(roi[2] * w)
    y0, y1 = int(roi[1] * h), int(roi[3] * h)
    crop = image[y0:y1, x0:x1]
    bb = _grid_bbox(crop)
    if bb is None:
        return None, {"error": "找不到小地图格子"}
    bx, by, bw, bh = bb
    cell_img = crop[by:by + bh, bx:bx + bw]
    gray = cv2.cvtColor(cell_img, cv2.COLOR_BGR2GRAY)

    col_seams = _seams(gray.mean(axis=0))
    row_seams = _seams(gray.mean(axis=1))
    cols = len(col_seams) + 1
    rows = len(row_seams) + 1

    # 每格中心亮度 → 最亮的那格当作小白块（当前房间）
    xs = [0] + col_seams + [bw]
    ys = [0] + row_seams + [bh]
    best = (-1.0, 0, 0)
    means = np.zeros((rows, cols))
    for r in range(rows):
        for c in range(cols):
            cy0, cy1 = ys[r], ys[r + 1]
            cx0, cx1 = xs[c], xs[c + 1]
            patch = gray[cy0 + 3:cy1 - 3, cx0 + 3:cx1 - 3]
            if patch.size == 0:
                continue
            v = float(patch.mean())
            means[r, c] = v
            if v > best[0]:
                best = (v, r, c)

    white_r, white_c = best[1], best[2]

    # 和 7 套 layouts 比：先按 (cols, rows) 筛，再按「整张灰格的 rooms 形状」区分同尺寸的兄弟。
    #
    # 判据说明：小地图里「有房间」的格子填了灰块，「没房间」的格子更暗（接近背景）。
    # 所以对每套候选算「有房间格的平均亮度 - 没房间格的平均亮度」，差值最大的那套最像。
    # 同尺寸的兄弟（3x3 有 index 3/6、3x4 有 0/2、4x3 有 1/5）靠这个分开。
    scored = []
    for idx, lay in enumerate(LAYOUTS):
        if int(lay["cols"]) != cols or int(lay["rows"]) != rows:
            continue
        rooms = lay["rooms"]
        if white_r * cols + white_c >= len(rooms):
            continue
        if int(rooms[white_r * cols + white_c]) < 0:
            continue  # 白块（当前房间）必须在有房间的格子上
        on, off = [], []
        for r in range(rows):
            for c in range(cols):
                rv = int(rooms[r * cols + c]) if r * cols + c < len(rooms) else -1
                (on if rv >= 0 else off).append(float(means[r, c]))
        if not on or not off:
            # 全是有房间 / 全是没房间的套数，没有对比意义，给个中性分
            gap = 0.0
        else:
            gap = sum(on) / len(on) - sum(off) / len(off)
        # 再比「整张灰格的形状」：把每格按亮度二分成有房间/没房间，
        # 和这套的 rooms 逐格对，错一格扣 20 分。
        # 这一步专门用来分开 (cols, rows) 相同、gap 又打平的兄弟套。
        _lo, _hi = float(means.min()), float(means.max())
        _thr = (_lo + _hi) / 2.0
        wrong = 0
        for r in range(rows):
            for c in range(cols):
                rv = int(rooms[r * cols + c]) if r * cols + c < len(rooms) else -1
                has = means[r, c] > _thr
                if has != (rv >= 0):
                    wrong += 1
        score = gap - wrong * 20.0
        scored.append((score, idx))

    scored.sort(reverse=True)
    cands = [i for _, i in scored]
    info = {
        "rows": rows, "cols": cols, "white": (white_r, white_c),
        "cell_means": means.round(1).tolist(), "cands": cands,
        "scores": [(round(g, 2), i) for g, i in scored],
    }
    if not cands:
        return None, info
    lay_idx = cands[0]
    info["layout_index"] = lay_idx
    # 换成仓库 Map 用的索引
    return LAYOUT_TO_MAP.get(lay_idx), info


# ---------------------------------------------------------------- 用白块位置反推「现在第几关」
# 用户要求：「地图上亮度最高的白色格子代表现在所在的房间，可以加个重新导航
# 并且回到原来的房间，然后再去本该去的下一个房间」
# 所以每帧读一次白块位置，算出它落在 route 的第几步；和仓库自己记的 level
# 不一致就以白块为准（白块是「我现在到底在哪」的直接证据）。
ROUTE_LIST = [
    (1, 1, 2, 3, 4), (0, 0, 3, 3, 4), (2, 3, 0, 0, 4), (3, 0, 1, 1, 4),
    (2, 3, 3, 0, 4), (3, 3, 2, 2, 4), (1, 0, 0, 1, 4),
]
# route_list 的编码：0=右 1=下 2=左 3=上 4=不走
_STEP = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}


def route_rooms(layout_idx: int, map_idx: int) -> list[int]:
    """按 route_list[map_idx] 在 layouts[layout_idx] 上走一遍，返回房间号序列。"""
    lay = LAYOUTS[layout_idx]
    cols, rows = int(lay["cols"]), int(lay["rows"])
    rooms = [int(v) for v in lay["rooms"]]
    start = lay.get("start")
    si = rooms.index(start) if start in rooms else 0
    r, c = divmod(si, cols)
    seq = [rooms[si]]
    for code in ROUTE_LIST[map_idx]:
        if code == 4:
            break
        dx, dy = _STEP[code]
        nr, nc = r + dy, c + dx
        if not (0 <= nr < rows and 0 <= nc < cols):
            break
        idx = nr * cols + nc
        if rooms[idx] < 0:
            break
        seq.append(rooms[idx])
        r, c = nr, nc
    return seq


def level_from_white(white_rc, layout_idx: int, map_idx: int):
    """白块 (row, col) → 这一局走到第几关（0-based）；不在路线上返回 None。"""
    if not white_rc:
        return None
    wr, wc = white_rc
    lay = LAYOUTS[layout_idx]
    cols, rows = int(lay["cols"]), int(lay["rows"])
    rooms = [int(v) for v in lay["rooms"]]
    if not (0 <= wr < rows and 0 <= wc < cols):
        return None
    room = rooms[wr * cols + wc]
    if room < 0:
        return None
    seq = route_rooms(layout_idx, map_idx)
    return seq.index(room) if room in seq else None

# ---------------------------------------------------------------- 小地图：几列 x 几行 + 白块在哪一格
# 用户 m26803：「怎么只识别白块位置，应该同时识别房间是几乘几的，然后看白块在坐标的那一格」。
# 做法：在小地图 ROI 里按「每格是一块灰色小方块」逐个连通域找出来（尺寸 14~30 x 11~22），
# 再把它们的中心点按间距聚类 → 得到列数、行数；最亮的那格就是当前房间。
# 比之前数暗缝稳：圆形容器边缘、光晕都不满足「方块尺寸」这一条。
def _cell_boxes(crop):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    m = ((sat < 95) & (val > 35) & (val < 235)).astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        bw, bh, area = stats[i, 2], stats[i, 3], stats[i, 4]
        if not (14 <= bw <= 30 and 11 <= bh <= 22):
            continue
        if not (0.9 <= bw / max(bh, 1) <= 1.8):
            continue
        if area < 150:
            continue
        out.append((float(cent[i][0]), float(cent[i][1]), float(gray[lab == i].mean())))
    return out


def _cluster1d(vals, tol=6.0):
    vals = sorted(vals)
    groups = []
    for v in vals:
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _near_count(cells, value, axis, tol=4.0):
    """某个中心线上落了多少个方块（axis=0 按 x、1 按 y）。"""
    return sum(1 for c in cells if abs(c[axis] - value) <= tol)


def _expand_centers(centers, pitch_hint=None):
    """把「整列/整行没被识别到」的窟窿补回来。

    小地图里偶尔会有某一格的方块太暗、连通域漏检，导致那一整列（或整行）的
    中心簇消失 → 数出来少一列/多一列，格阵尺寸就跟着错。这里用相邻簇的间距
    推断真实步距，凡是间距接近 k 倍步距的，就在中间补上缺的中心。
    """
    if len(centers) < 2:
        return list(centers)
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    pitch = pitch_hint or min(g for g in gaps if g > 0) if any(g > 0 for g in gaps) else 0.0
    if pitch <= 0:
        return list(centers)
    out = [centers[0]]
    for i, gap in enumerate(gaps):
        k = int(round(gap / pitch))
        if k >= 2:
            for m in range(1, k):
                out.append(centers[i] + m * pitch)
        out.append(centers[i + 1])
    return out


def _snap_centers(centers, counts, min_count):
    """按「每列/每行至少要有 min_count 个方块」剔掉圆框边缘、光斑这类假簇。"""
    if len(centers) <= 2:
        return centers
    keep = [c for c, n in zip(centers, counts) if n >= min_count]
    if len(keep) < 2:
        return centers
    return keep


def read_grid(image, roi=(0.85, 0.0, 1.0, 0.24)):
    """返回 dict(rows, cols, white=(row, col), means)，失败返回 None。

    rows/cols 由方块中心聚类得出；white 是亮度最高的那一格的 (row, col)。
    """
    h, w = image.shape[:2]
    crop = image[int(roi[1] * h):int(roi[3] * h), int(roi[0] * w):int(roi[2] * w)]
    cells = _cell_boxes(crop)
    if len(cells) < 6:
        return None
    xs = _cluster1d([c[0] for c in cells])
    ys = _cluster1d([c[1] for c in cells])
    # 圆框边缘的亮弧、格子高光有时会凑出一个「假列/假行」。真正的格阵里每一列
    # 至少有两个方块，只有 1 个方块又跟邻居间距不整的，基本是噪声。
    xs = _snap_centers(xs, [_near_count(cells, x, 0) for x in xs], 2)
    ys = _snap_centers(ys, [_near_count(cells, y, 1) for y in ys], 2)
    xs = _expand_centers(xs)
    ys = _expand_centers(ys)
    if len(xs) < 2 or len(ys) < 2:
        return None
    # 白块那格是纯白、会跟相邻格糊成一整块，用连通域反而找不到它；
    # 直接按格阵坐标量每格的平均亮度，最亮的那格就是白块。
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    px = (xs[-1] - xs[0]) / (len(xs) - 1) if len(xs) > 1 else 24.0
    py = (ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else 18.0
    half_x, half_y = max(4, int(px * 0.34)), max(4, int(py * 0.34))
    means = [[-1.0] * len(xs) for _ in range(len(ys))]
    for ri, cy in enumerate(ys):
        for ci, cx in enumerate(xs):
            x1, x2 = max(0, int(cx) - half_x), min(gray.shape[1], int(cx) + half_x + 1)
            y1, y2 = max(0, int(cy) - half_y), min(gray.shape[0], int(cy) + half_y + 1)
            if x2 - x1 < 3 or y2 - y1 < 3:
                continue
            means[ri][ci] = float(gray[y1:y2, x1:x2].mean())
    best = max(((means[r][c], r, c) for r in range(len(ys)) for c in range(len(xs))
                if means[r][c] >= 0), default=None)
    if best is None:
        return None
    return {"rows": len(ys), "cols": len(xs), "white": (best[1], best[2]), "means": means}

def cell_at_level(layout_idx, map_idx, level):
    """沿 route_list[map_idx] 从起点格走 level 步，返回应该在的 (row, col)。

    白块代表「我现在在哪一格」，不是「起点在哪」—— 用户 m26864 指出 3x4 那两套
    的起点格不同、白块也会随进度移动，所以要按当前 level 校验，而不是只比起点。
    """
    lay = LAYOUTS[layout_idx]
    cols, rows = int(lay["cols"]), int(lay["rows"])
    rooms = [int(v) for v in lay["rooms"]]
    start = lay.get("start")
    si = rooms.index(start) if start in rooms else 0
    r, c = divmod(si, cols)
    for step in range(int(level)):
        code = ROUTE_LIST[map_idx][step] if step < len(ROUTE_LIST[map_idx]) else 4
        if code == 4:
            return None
        dx, dy = _STEP[code]
        r, c = r + dy, c + dx
        if not (0 <= r < rows and 0 <= c < cols):
            return None
        if rooms[r * cols + c] < 0:
            return None
    return (r, c)

def start_cell_center(layout_idx, map_idx, level=0):
    """该套在 level 那一步应该落在的格子的「归一化中心」(x, y)。

    用户 m26876：3x3 有两套、4x3 也有两套，它们的**起点格不一样**，
    刚进对局时白块就是起点格 —— 所以直接用白块的归一化位置去比这个中心，
    不需要先正确数出「几列几行」（数错一列就会把整件事带偏）。
    """
    lay = LAYOUTS[layout_idx]
    cols, rows = int(lay["cols"]), int(lay["rows"])
    cell = cell_at_level(layout_idx, map_idx, level)
    if cell is None:
        return None
    r, c = cell
    return ((c + 0.5) / cols, (r + 0.5) / rows)
