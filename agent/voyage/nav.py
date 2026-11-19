"""静态地图 + A* 寻路。

照 zmxy4 的做法：障碍物用手上的静态布局（data/scenario.json 的 obstacles），
不从画面实时识别——识别出来的礁石图一格错就会把船顶到石头上。
"""
from __future__ import annotations

import heapq
import json
from pathlib import Path

DATA = json.loads(Path("data/scenario.json").read_text(encoding="utf-8"))
SIZE = tuple(DATA["size"])                      # (30, 19)
W, H = SIZE
OBSTACLES: dict[int, list[list[int]]] = {int(k): v for k, v in DATA["obstacles"].items()}
LAYOUTS = DATA["layouts"]

# WASD 四向（游戏里一次只按一个方向最稳）
DIRS = [("d", 1, 0), ("a", -1, 0), ("s", 0, 1), ("w", 0, -1)]
NAMES = {(1, 0): "右", (-1, 0): "左", (0, 1): "下", (0, -1): "上"}


def walkable(room: int, inflate: bool = False) -> list[list[bool]]:
    """可走格。inflate=True 时把礁石四周各扩一格。

    注意：默认 **不膨胀**。实测 5102 膨胀后从 304 格掉到 128 格，
    中间 2 格宽的通道会被整条封死，A* 直接找不到路（这就是「兜圈子」的根因）。
    贴石头角的问题交给运行时的 choose_unstuck 处理。
    """
    g = OBSTACLES[room]
    out = [[g[r][c] == 0 for c in range(W)] for r in range(H)]
    if inflate:
        inf = [[False] * W for _ in range(H)]
        for r in range(H):
            for c in range(W):
                if not out[r][c]:
                    for dr in (-1, 0, 1):
                        for dc in (-1, 0, 1):
                            nr, nc = r + dr, c + dc
                            if 0 <= nr < H and 0 <= nc < W:
                                inf[nr][nc] = True
        out = [[out[r][c] and not inf[r][c] for c in range(W)] for r in range(H)]
    return out


def astar(room: int, start: tuple, goal: tuple, inflate: bool = True, limit: int = 4000):
    """A* 走格子，返回 [(col,row), ...]；走不到返回 None。"""
    grid = walkable(room, inflate)
    sx, sy = int(round(start[0])), int(round(start[1]))
    gx, gy = int(round(goal[0])), int(round(goal[1]))
    if not ok(gx, gy):
        # 目标点在墙里（门口贴着边界时很常见）→ 就近吸到最近的可走格
        best, bd = None, 1e9
        for r in range(h):
            for c in range(w):
                if not grid[r][c]:
                    continue
                d2 = (c - gx) ** 2 + (r - gy) ** 2
                if d2 < bd:
                    bd, best = d2, (c, r)
        if best is None:
            return None
        gx, gy = best
    sx = max(0, min(W - 1, sx)); sy = max(0, min(H - 1, sy))
    gx = max(0, min(W - 1, gx)); gy = max(0, min(H - 1, gy))
    if not grid[gy][gx]:
        # 目标落在石头里（例如门口被膨胀吃掉），就找最近的可走格
        best, bd = None, 1e9
        for r in range(H):
            for c in range(W):
                if grid[r][c]:
                    d = (c - gx) ** 2 + (r - gy) ** 2
                    if d < bd:
                        bd, best = d, (c, r)
        if best is None:
            return None
        gx, gy = best
    if (sx, sy) == (gx, gy):
        return [(sx, sy)]
    openq = [(abs(sx - gx) + abs(sy - gy), 0, (sx, sy))]
    came = {(sx, sy): None}
    best_g = {(sx, sy): 0}
    expanded = 0
    while openq and expanded < limit:
        _, gcur, cur = heapq.heappop(openq)
        expanded += 1
        if cur == (gx, gy):
            path = []
            node = cur
            while node is not None:
                path.append(node)
                node = came[node]
            return path[::-1]
        cx, cy = cur
        for _k, dx, dy in DIRS:
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < W and 0 <= ny < H and grid[ny][nx]:
                ng = gcur + 1
                if ng < best_g.get((nx, ny), 1 << 30):
                    best_g[(nx, ny)] = ng
                    came[(nx, ny)] = cur
                    heapq.heappush(openq, (ng + abs(nx - gx) + abs(ny - gy), ng, (nx, ny)))
    return None


def door_cell(room: int, side: str, inflate: bool = False) -> tuple | None:
    """这一侧的门口（格子坐标）。

    静态图里四周是封闭的礁石墙、出口不在障碍表里，所以不能直接读边界格；
    改成从边界往里扫，在中轴附近找第一个可走格当作「门口」。
    """
    g = walkable(room, inflate)
    if side in ("上", "下"):
        order = range(2, H - 2) if side == "上" else range(H - 3, 1, -1)
        for d, y in enumerate(order):
            for dx in (0, -1, 1, -2, 2, -3, 3):
                x = W // 2 + dx
                if 0 <= x < W and g[y][x]:
                    return (x, y)
            if d > 8:
                break
        return None
    order = range(2, W - 2) if side == "左" else range(W - 3, 1, -1)
    for d, x in enumerate(order):
        for dy in (0, -1, 1, -2, 2, -3, 3):
            y = H // 2 + dy
            if 0 <= y < H and g[y][x]:
                return (x, y)
        if d > 8:
            break
    return None


def path_to_steps(path: list[tuple]) -> list[tuple[str, int]]:
    """把一条格子路径压成「方向 + 连续步数」。"""
    steps: list[tuple[str, int]] = []
    for i in range(1, len(path)):
        dx = path[i][0] - path[i - 1][0]
        dy = path[i][1] - path[i - 1][1]
        key = {
            (1, 0): "d", (-1, 0): "a", (0, 1): "s", (0, -1): "w",
            (1, 1): "ds", (1, -1): "dw", (-1, 1): "as", (-1, -1): "aw",
        }[(dx, dy)]
        if steps and steps[-1][0] == key:
            steps[-1] = (key, steps[-1][1] + 1)
        else:
            steps.append((key, 1))
    return steps


if __name__ == "__main__":
    import sys
    room = int(sys.argv[1]) if len(sys.argv) > 1 else 5102
    print(f"房间 {room} 的静态障碍：")
    for row in OBSTACLES[room]:
        print("   " + "".join("#" if v else "." for v in row))
    print("四边出口：", {s: door_cell(room, s) for s in ("上", "下", "左", "右")})


_DIR8 = (
    (0, -1, 1.0), (1, 0, 1.0), (0, 1, 1.0), (-1, 0, 1.0),
    (1, -1, 1.41421356), (1, 1, 1.41421356), (-1, 1, 1.41421356), (-1, -1, 1.41421356),
)


def distance_from_obstacles(grid):
    """每格到最近障碍的曼哈顿距离（照抄 zmxy4 的 calculate_distance_from_obstacles）。"""
    import numpy as np
    from scipy.ndimage import distance_transform_cdt

    m = np.asarray(grid, dtype=bool)              # True = 可走
    pad = np.pad(~m, 1, constant_values=True)     # 障碍=True，顺手把四周当墙
    return distance_transform_cdt(pad, metric="taxicab")[1:-1, 1:-1]


def astar_grid(grid, start: tuple, goal: tuple, limit: int = 4000,
               hate: float = 1.0, turn_cost: float = 2.0, last_dir=None):
    """八方向 A*（grid[r][c] True=可走）。

    照抄 zmxy4 的两条关键代价，这才是「不贴石头、不来回摇摆」的来源：
    - 贴墙惩罚：单步代价 = 1 + hate/(到最近障碍距离+1)，离礁石越近代价越高
    - 首步转向成本：和上一帧移动方向的夹角越大越贵，完全反向 = turn_cost
    另外斜向不允许穿墙角（两侧必须同时可走）。
    """
    import heapq as _hq
    import math as _math

    h, w = len(grid), len(grid[0])

    def ok(cx, cy):
        return 0 <= cx < w and 0 <= cy < h and grid[cy][cx]

    try:
        dist = distance_from_obstacles(grid)
    except Exception:
        dist = None

    sx, sy = int(round(start[0])), int(round(start[1]))
    gx, gy = int(round(goal[0])), int(round(goal[1]))

    def nearest_free(cx, cy):
        best, bd = None, 1e9
        for r in range(h):
            for c in range(w):
                if grid[r][c]:
                    d = (c - cx) ** 2 + (r - cy) ** 2
                    if d < bd:
                        bd, best = d, (c, r)
        return best

    if not ok(sx, sy):
        nf = nearest_free(sx, sy)
        if nf is None:
            return None
        sx, sy = nf
    if not ok(gx, gy):
        nf = nearest_free(gx, gy)
        if nf is None:
            return None
        gx, gy = nf
    if (sx, sy) == (gx, gy):
        return [(sx, sy)]

    def heur(cx, cy):
        return _math.hypot(cx - gx, cy - gy)

    openq = [(heur(sx, sy), 0.0, (sx, sy))]
    came = {(sx, sy): None}
    best_g = {(sx, sy): 0.0}
    n = 0
    while openq and n < limit:
        _, gcur, cur = _hq.heappop(openq)
        n += 1
        if cur == (gx, gy):
            path = []
            node = cur
            while node is not None:
                path.append(node)
                node = came[node]
            return path[::-1]
        cx, cy = cur
        for dx, dy, step in _DIR8:
            nx, ny = cx + dx, cy + dy
            if not ok(nx, ny):
                continue
            if dx and dy and not (ok(cx + dx, cy) and ok(cx, cy + dy)):
                continue                      # 不许穿墙角
            cost = step
            if dist is not None:
                cost += hate / (float(dist[ny][nx]) + 1.0)
            if cur == (sx, sy) and last_dir is not None:
                lx, ly = last_dir
                ln = _math.hypot(lx, ly) or 1.0
                cos = (dx * lx + dy * ly) / (step * ln)
                cost += turn_cost * (1.0 - cos) / 2.0
            ng = gcur + cost
            if ng < best_g.get((nx, ny), 1e18):
                best_g[(nx, ny)] = ng
                came[(nx, ny)] = cur
                _hq.heappush(openq, (ng + heur(nx, ny), ng, (nx, ny)))
    return None


def identify_room(rock_grid, players=None) -> tuple[int, float]:
    """把画面里读到的礁石格子图，跟 10 张静态表比，返回最像的房间号和得分。

    只比「内部区域」（去掉四周一圈边界），因为边界在每张表里都是墙，
    比了反而不能区分。
    """
    import numpy as _np
    best, best_score = None, -1.0
    for rid, grid in OBSTACLES.items():
        g = _np.asarray(grid, dtype=bool)
        p = _np.asarray(rock_grid, dtype=bool)
        inner_g = g[2:H - 2, 4:W - 4]
        inner_p = p[2:H - 2, 4:W - 4]
        inter = _np.logical_and(inner_g, inner_p).sum()
        union = _np.logical_or(inner_g, inner_p).sum()
        score = float(inter) / float(union) if union else 0.0
        if score > best_score:
            best, best_score = rid, score
    return best, best_score


# 四个出口大致在画布的中轴上（仓库里的 EXIT_LEFT=0.46 / RIGHT=0.57 / UP=0.40 / DOWN=0.54
# 都落在中段），所以直接取「可走区域」中轴上的那一格当门口。
DOOR_CELL = {"上": (15, 2), "下": (15, 16), "左": (2, 9), "右": (27, 9)}


def path_step(path, pos, thresh: float = 0.9):
    """沿路径走：返回下一步该按的方向键，(w/a/s/d) 或 None（说明已经到终点）。

    pos 是 (x, y) 浮点格坐标；path 是 [(x, y), ...] 的整数格序列。
    """
    if not path:
        return None
    i = 0
    while i < len(path):
        px, py = float(path[i][0]), float(path[i][1])
        if ((px - pos[0]) ** 2 + (py - pos[1]) ** 2) ** 0.5 >= thresh:
            break
        i += 1
    if i >= len(path):
        return None
    tx, ty = float(path[i][0]), float(path[i][1])
    dx, dy = tx - pos[0], ty - pos[1]
    # 八方向：两个轴都明显偏了就同时按两个键（键盘支持斜向）
    keys = ""
    if abs(dx) > 0.35:
        keys += "d" if dx > 0 else "a"
    if abs(dy) > 0.35:
        keys += "s" if dy > 0 else "w"
    if not keys:
        keys = "d" if dx > 0 else ("a" if dx < 0 else ("s" if dy > 0 else "w"))
    return keys
