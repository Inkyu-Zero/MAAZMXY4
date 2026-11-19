import numpy as np
from collections import deque
import heapq
import math
from scipy.ndimage import distance_transform_cdt

try:
    from ..._native import region_path as _native_region_path
except ModuleNotFoundError as exc:
    if exc.name != "zmxy4_treasure_hunt._native":
        raise
    _native_region_path = None


def calculate_distance_from_obstacles(maze):
    """
    计算迷宫中每个通路点到最近障碍物的曼哈顿距离，数组外侧也视为墙。

    参数:
    - maze: 二维布尔数组（bool numpy array），0 表示通路，1 表示障碍。

    返回:
    - distance_from_obstacle: 二维数组，每个通路点到最近障碍物的距离，障碍物位置为 0。
    """
    # 补一圈墙，使边缘通路到数组外侧的距离为 1。
    obstacles = np.pad(maze == 1, 1, constant_values=True)
    # 四方向 BFS 对应曼哈顿距离；保留 float64，供调用方写入和参与代价运算。
    return distance_transform_cdt(~obstacles, metric="taxicab")[1:-1, 1:-1].astype(np.float64)


_DIRECTIONS = ((0, 1), (1, 0), (0, -1), (-1, 0),
               (1, 1), (1, -1), (-1, 1), (-1, -1))


def calculate_turn_costs(last_direction, turn_cost):
    """八方向的首步转向成本；turn_cost 是完全反向的成本，不随步长变化。"""
    if not math.isfinite(turn_cost) or turn_cost < 0:
        raise ValueError("turn_cost must be nonnegative and finite")
    if last_direction is None:
        return (0.0,) * 8
    if tuple(last_direction) not in _DIRECTIONS:
        raise ValueError("last_direction must be an eight-direction unit vector")
    previous = math.atan2(last_direction[1], last_direction[0])
    costs = []
    for dx, dy in _DIRECTIONS:
        angle = abs((math.atan2(dy, dx) - previous + math.pi) % (2 * math.pi) - math.pi)
        costs.append(turn_cost * round(angle / (math.pi / 4)) / 4)
    return tuple(costs)


def find_nearest_passable_point(maze, start):
    rows, cols = maze.shape
    x = min(cols - 1, max(0, int(round(start[0]))))
    y = min(rows - 1, max(0, int(round(start[1]))))
    start = (x, y)
    if maze[y, x] == 0:
        return start  # 起点已经是通路

    queue = deque([start])
    visited = {start}

    while queue:
        cx, cy = queue.popleft()

        for dc, dr in [(0, -1), (0, 1), (-1, 0), (1, 0)]:  # 四个方向
            nx, ny = cx + dc, cy + dr
            if 0 <= ny < rows and 0 <= nx < cols and (nx, ny) not in visited:
                if maze[ny, nx] == 0:
                    return (nx, ny)  # 找到最近的通路点
                visited.add((nx, ny))
                queue.append((nx, ny))

    return start  # 如果没有找到，返回原点（极端情况下）


def _region_path(maze, start, targets, distance_maze, hate_factor,
                 danger=None, last_direction=None, turn_cost=2.0, stride=1,
                 allow_partial=True, cost_map=None):
    """指定步长的 A*；跨步路径逐格检查，不额外检查斜向两侧。

    无时间限制；找到评分大于 0.8 的目标即返回，否则遍历可达节点选取最高分目标。
    脱险时找到安全目标即返回，无法完全脱险时返回风险更低的位置。
    cost_map 是非负逐格额外成本，仅影响路径成本，不启用 danger 的脱险语义。
    last_direction 是上次实际移动的单位向量；仅第一步按转向夹角加入 turn_cost。
    """
    rows, cols = maze.shape
    targets = np.asarray(targets)
    blocked_targets = (maze != 0) & (targets != 0)
    if np.any(blocked_targets):
        targets = targets.copy()
        targets[blocked_targets] = 0
    if not np.any(targets):
        return []
    goal_distance = distance_transform_cdt(targets == 0, metric="chessboard")
    if distance_maze is None:
        distance_maze = calculate_distance_from_obstacles(maze)
    min_step_cost = 1 + hate_factor / (np.max(distance_maze) + 1)
    turn_costs = calculate_turn_costs(last_direction, turn_cost)
    if _native_region_path is not None:
        # 仅在 dtype 或布局不匹配时转换；Rust 持有 GIL，零拷贝借用这些数组。
        return _native_region_path(
            np.ascontiguousarray(maze, dtype=np.bool_), start,
            np.ascontiguousarray(targets, dtype=np.float64),
            np.ascontiguousarray(goal_distance, dtype=np.int32),
            np.ascontiguousarray(distance_maze, dtype=np.float64),
            hate_factor, float(min_step_cost),
            None if danger is None else np.ascontiguousarray(danger, dtype=np.float64),
            turn_costs, stride, allow_partial,
            None if cost_map is None else np.ascontiguousarray(cost_map, dtype=np.float64),
        )
    costs = {start: 0.0}
    parents = {}
    remaining = float(goal_distance[start[1], start[0]])
    queue = [(remaining * min_step_cost, remaining, 0.0, start)]
    best = None
    best_value = -np.inf
    safer = start

    def path_to(point):
        path = []
        while point in parents:
            path.append(point)
            point = parents[point]
        return path[::-1]

    while queue:
        _, _, cost, current = heapq.heappop(queue)
        if cost != costs[current]:
            continue
        cx, cy = current
        if danger is not None and danger[cy, cx] < danger[safer[1], safer[0]]:
            safer = current
        value = targets[cy, cx]
        if value > 0:
            if danger is not None:
                return path_to(current)
            if value > best_value:
                best, best_value = current, value
            if value > 0.8:
                return path_to(best)
            # 允许穿过低分候选区域，继续寻找更合适的射击位置。
        for direction_index, (dx, dy) in enumerate(_DIRECTIONS):
            nx, ny = cx + dx * stride, cy + dy * stride
            if not (0 <= nx < cols and 0 <= ny < rows) or maze[ny, nx]:
                continue
            step_cost = turn_costs[direction_index] if current == start else 0.0
            blocked = False
            for offset in range(1, stride + 1):
                x, y = cx + dx * offset, cy + dy * offset
                if maze[y, x]:
                    blocked = True
                    break
                step_cost += 1 + hate_factor / (distance_maze[y, x] + 1)
                if danger is not None:
                    step_cost += danger[y, x]
                if cost_map is not None:
                    step_cost += cost_map[y, x]
            if blocked:
                continue
            new_cost = cost + step_cost
            neighbor = (nx, ny)
            if new_cost < costs.get(neighbor, np.inf):
                costs[neighbor] = new_cost
                parents[neighbor] = current
                remaining = float(goal_distance[ny, nx])
                priority = new_cost + remaining * min_step_cost
                # 浮点累加误差不应打破等价路径的距离择优，导致开阔地大面积扩展。
                heapq.heappush(queue, (round(priority, 9), remaining, new_cost, neighbor))
    if best is not None:
        return path_to(best)
    return path_to(safer) if danger is not None and allow_partial else []


def _coarse_to_fine_region_path(maze, start, targets, distance_maze, hate_factor, **kwargs):
    """先搜索 5 格网格，未找到目标时再以 1 格完整搜索。"""
    passable_targets = (np.asarray(targets) > 0) & (maze == 0)
    if not np.any(passable_targets):
        return []
    if distance_maze is None:
        distance_maze = calculate_distance_from_obstacles(maze)
    for stride in (5, 1):
        # 跨步只能落在与起点坐标模 stride 同余的格子上；没有候选时直接细搜。
        if stride > 1 and not np.any(passable_targets[start[1] % stride::stride,
                                                      start[0] % stride::stride]):
            continue
        path = _region_path(
            maze, start, targets, distance_maze, hate_factor,
            stride=stride, allow_partial=stride == 1, **kwargs,
        )
        if path or (not maze[start[1], start[0]] and targets[start[1], start[0]] > 0):
            return path
    return []


def find_nearest_passable_path(maze, start, flee_region, distance_maze=None, hate_factor=1,
                               last_direction=None, turn_cost=4.0):
    """先以 5 格寻找脱险路径，失败后以 1 格重试。"""
    return _coarse_to_fine_region_path(maze, start, (flee_region == 0).astype(np.float32),
                              distance_maze, hate_factor, danger=flee_region,
                              last_direction=last_direction, turn_cost=turn_cost)


def find_path_to_point(maze, start, end, stride: int = 10, distance_maze=None, hate_factor=1,
                       last_direction=None, turn_cost=2.0):
    # 迷宫的尺寸
    rows, cols = maze.shape
    end = (min(cols - 1, max(0, int(round(end[0])))), min(rows - 1, max(0, int(round(end[1])))))

    # 八方向移动
    directions = [
        (0, stride), (stride, 0), (0, -stride), (-stride, 0),  # 上下左右
        (stride, stride), (stride, -stride), (-stride, stride), (-stride, -stride)  # 四个对角线
    ]
    turn_costs = dict(zip(_DIRECTIONS, calculate_turn_costs(last_direction, turn_cost)))

    # 计算每个点到障碍物的距离图
    if distance_maze is None:
        distance_from_obstacle = calculate_distance_from_obstacles(maze)
    else:
        distance_from_obstacle = distance_maze

    # 启发函数
    def heuristic(x, y):
        # 使用步数下界，避免像素距离压过真实路径成本和首步转向成本。
        return max(0, (max(abs(x - end[0]), abs(y - end[1])) - 1) // stride)

    # 优先队列初始化
    open_set = []
    heapq.heappush(open_set, (0, start))

    # 路径跟踪字典
    came_from = {}
    cost_so_far = {start: 0}

    while open_set:
        _, current = heapq.heappop(open_set)
        cx, cy = current

        # 判断是否进入终点附近的步幅半宽度范围
        if abs(cx - end[0]) <= stride and abs(cy - end[1]) <= stride:
            path = []
            while current in came_from:
                path.append(current)
                current = came_from[current]
            path.reverse()
            path.append(end)
            return path

        # 八向扩展
        for dc, dr in directions:
            nx, ny = cx + dc, cy + dr

            if 0 <= ny < rows and 0 <= nx < cols and maze[ny, nx] == 0:
                new_cost = cost_so_far[current] + 1 + 1 / (distance_from_obstacle[ny, nx] + 1) * hate_factor
                if current == start:
                    new_cost += turn_costs[(dc // stride, dr // stride)]

                if (nx, ny) not in cost_so_far or new_cost < cost_so_far[(nx, ny)]:
                    cost_so_far[(nx, ny)] = new_cost
                    priority = new_cost + heuristic(nx, ny)
                    heapq.heappush(open_set, (priority, (nx, ny)))
                    came_from[(nx, ny)] = current

    return [end]


def find_best_path_to_region(maze, start, end_region, distance_maze=None, hate_factor=1,
                             cost_map=None, last_direction=None, turn_cost=2.0):
    """按目标评分择优；先粗后细搜索，可传入与地图同形状的非负逐格额外成本。"""
    return _coarse_to_fine_region_path(maze, start, end_region, distance_maze,
                              hate_factor, cost_map=cost_map,
                              last_direction=last_direction, turn_cost=turn_cost)
