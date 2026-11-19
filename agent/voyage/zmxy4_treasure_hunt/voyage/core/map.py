"""地图构建、关卡路线和移动/攻击位置决策。"""

import numpy as np
from scipy.ndimage import binary_dilation

from . import maze_utils
from .direction import Direction, direction_list, get_direction_vector
from .constants import (
    BULLET_AVOID_HEIGHT,
    BULLET_AVOID_WIDTH,
    COLLISION_DOWN,
    COLLISION_LEFT,
    COLLISION_RIGHT,
    COLLISION_UP,
    EDGE_DOWN,
    EDGE_LEFT,
    EDGE_RIGHT,
    EDGE_UP,
    END_DOWN,
    END_LEFT,
    END_RIGHT,
    END_UP,
    EXIT_DOWN,
    EXIT_LEFT,
    EXIT_RIGHT,
    EXIT_UP,
    FIRE_X_RANGE,
    FIRE_Y_RANGE,
    HIT_HEIGHT,
    HIT_WIDTH,
    MAP_DOWN,
    MAP_LEFT,
    MAP_RIGHT,
    MAP_UP,
    MAZE_HEIGHT,
    MAZE_WIDTH,
)


class Map:
    def __init__(self, render_enabled=True):
        self.render_enabled = render_enabled
        # 三个常量
        self.route_list = [
            (1, 1, 2, 3, 4),
            (0, 0, 3, 3, 4),
            (2, 3, 0, 0, 4),
            (3, 0, 1, 1, 4),
            (2, 3, 3, 0, 4),
            (3, 3, 2, 2, 4),
            (1, 0, 0, 1, 4)
        ]

        self.tasks_list = [
            (0, 1, 2, 3),
            (4, 5, 0, 3),
            (1, 6, 7, 3),
            (2, 8, 4, 3),
            (6, 8, 1, 3),
            (5, 8, 7, 3),
            (6, 7, 2, 3),
        ]

        # 第几套大地图，共有7种
        self.map_index = None

        # 9种任务地图布局，每个地图为7*13的bool矩阵，1表示障碍物，0表示可通行
        self.task_maps = np.zeros((9, 7, 13), dtype=np.bool_)

        # reset时初始化为当前map_index对应的路线，value为0-4，表示右下上左
        self.route = None

        self.level = 0
        self.next_exit = None

        self.collision_left = None
        self.collision_right = None
        self.collision_up = None
        self.collision_down = None

        self.map_left = None
        self.map_up = None
        self.map_width = None
        self.map_height = None

        self.map_template = None

        self.init_task_maps()
        self.map_template, self.map_template_without_exit = self.init_map_template((MAZE_WIDTH, MAZE_HEIGHT))

        self.level_mazes = None
        self.distance_mazes = None

        self.debug_image = None
        self.flee_mazes = None

    def init_task_maps(self):
        self.task_maps[0] = np.array([
            [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
            [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1],
            [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
        ], dtype=np.bool_)

        self.task_maps[1][0][-1] = 1
        self.task_maps[1][2][3] = 1
        self.task_maps[1][3][5] = 1
        self.task_maps[1][3][7] = 1
        self.task_maps[1][4][9] = 1
        self.task_maps[1][6][0] = 1

        self.task_maps[2] = np.array([
            [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
            [0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0],
            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0],
            [0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0],
            [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
        ], dtype=np.bool_)

        self.task_maps[3][1][-2] = 1
        self.task_maps[3][-2][1] = 1

        self.task_maps[4] = np.array([
            [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0],
            [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
        ], dtype=np.bool_)

        self.task_maps[5] = np.array([
            [0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0],
            [1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1],
            [0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0],
            [0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0],
            [0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0],
            [1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1],
            [0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0],
        ], dtype=np.bool_)

        self.task_maps[6] = np.array([
            [1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],
            [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1],
            [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1],
            [0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0],
            [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1],
            [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1],
            [1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],
        ], dtype=np.bool_)

        self.task_maps[7] = np.array([
            [0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0],
            [0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
            [0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0],
        ], dtype=np.bool_)

        self.task_maps[8] = np.array([
            [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
        ], dtype=np.bool_)

    def init_map_template(self, maze_size):
        width, height = maze_size
        map_template = np.zeros((height, width), dtype=np.bool_)
        exit_left = int(round(width * EXIT_LEFT))
        exit_right = int(round(width * EXIT_RIGHT))
        exit_up = int(round(height * EXIT_UP))
        exit_down = int(round(height * EXIT_DOWN))

        edge_left = int(round(width * EDGE_LEFT))
        edge_right = int(round(width * EDGE_RIGHT))
        edge_up = int(round(height * EDGE_UP))
        edge_down = int(round(height * EDGE_DOWN))

        map_template[:edge_up, :] = 1
        map_template[edge_down:, :] = 1
        map_template[:, :edge_left] = 1
        map_template[:, edge_right:] = 1

        map_template_without_exit = map_template.copy()

        map_template[exit_up:exit_down, :] = 0
        map_template[:, exit_left:exit_right] = 0

        # 旧版识别精度不高设置的禁区
        # left_top = (int(round(0.22 * MAZE_WIDTH)), int(round(0.18 * MAZE_HEIGHT)))
        # right_top1 = (int(round(0.68 * MAZE_WIDTH)), int(round(0.19 * MAZE_HEIGHT)))
        # right_top2 = (int(round(0.86 * MAZE_WIDTH)), int(round(0.25 * MAZE_HEIGHT)))
        #
        # map_template_without_exit[:left_top[1], :left_top[0]] = 1
        # map_template_without_exit[:right_top1[1], right_top1[0]:] = 1
        # map_template_without_exit[:right_top2[1], right_top2[0]:] = 1

        return map_template, map_template_without_exit

    def reset(self, map_index):
        self.map_index = map_index
        self.route = self.route_list[map_index]

        self.level = 0
        self.next_exit = self.route[self.level]
        self.level_mazes, self.distance_mazes = self.reset_map_list((MAZE_WIDTH, MAZE_HEIGHT), self.map_template)

    def reset_map_list(self, maze_size, map_template):
        level_mazes = []
        distance_mazes = []
        width, height = maze_size
        tasks = self.tasks_list[self.map_index]

        map_left = int(round(MAP_LEFT * width))
        map_up = int(round(MAP_UP * height))
        map_width = int(round((MAP_RIGHT - MAP_LEFT) * width))
        map_height = int(round((MAP_DOWN - MAP_UP) * height))

        collision_left = int(round(maze_size[0] * COLLISION_LEFT))
        collision_right = int(round(maze_size[0] * COLLISION_RIGHT))
        collision_up = int(round(maze_size[1] * COLLISION_UP))
        collision_down = int(round(maze_size[1] * COLLISION_DOWN))

        for task in tasks:
            maze = np.zeros((height, width), dtype=np.bool_)
            map_area = self.enlarge_bool_array(self.task_maps[task], map_width, map_height)

            maze[map_up:map_up + map_height, map_left:map_left + map_width] = map_area
            maze = self.directional_dilate(maze, collision_right, collision_left, collision_down, collision_up)

            maze = np.logical_or(maze, map_template)
            distance_maze = maze_utils.calculate_distance_from_obstacles(maze)

            level_mazes.append(maze)
            distance_mazes.append(distance_maze)

        return level_mazes, distance_mazes

    def get_maze(self):
        if self.level == 0:
            return self.map_template
        return self.level_mazes[self.level - 1]

    def get_task(self):
        if self.level == 0:
            return -1
        return self.tasks_list[self.map_index][self.level - 1]

    def get_distance_maze(self):
        if self.level == 0:
            return self.map_template_without_exit
        return self.distance_mazes[self.level - 1]

    def enlarge_bool_array(self, task_map, map_width, map_height):
        original_height, original_width = task_map.shape

        # 计算每个元素扩展后的矩阵大小
        factor_x = map_width // original_width + 1
        factor_y = map_height // original_height + 1

        # 扩展每个元素
        enlarged_map = np.block([[np.full((factor_y, factor_x), val) for val in row] for row in task_map])

        # 计算需要裁剪的上下左右的数量
        extra_height = enlarged_map.shape[0] - map_height
        extra_width = enlarged_map.shape[1] - map_width

        # 均匀裁剪上下左右
        crop_top = extra_height // 2
        crop_bottom = extra_height - crop_top
        crop_left = extra_width // 2
        crop_right = extra_width - crop_left

        cropped_map = enlarged_map[crop_top:-crop_bottom, crop_left:-crop_right]

        return cropped_map

    def directional_dilate(self, bool_array, expand_left=0, expand_right=0, expand_top=0, expand_bottom=0):
        # 根据指定的方向构建结构元素（卷积核）
        half_height = max(expand_top, expand_bottom)
        half_width = max(expand_left, expand_right)
        kernel_height = 2 * half_height + 1
        kernel_width = 2 * half_width + 1
        kernel = np.zeros((kernel_height, kernel_width), dtype=np.bool_)

        # 将指定方向的区域填充为 True
        kernel[half_height - expand_top:half_height + expand_bottom + 1,
        half_width - expand_left:half_width + expand_right + 1] = True

        # 使用 binary_dilation 进行膨胀
        expanded_array = binary_dilation(bool_array, structure=kernel, output=True)

        return expanded_array

    def next_level(self):
        self.level += 1
        if self.level == 5:
            self.level = 0

        self.next_exit = self.route[self.level]

    def _fire_axis_mask(self, maze, enemy, index, axis):
        """一条对齐射线的可开火位置；统一整数坐标、中心射程和逐格遮挡。"""
        height, width = maze.shape
        if axis == 0:
            valid = 0 <= index < width and enemy.x1 < index < enemy.x2
            center, limit = enemy.y, FIRE_Y_RANGE
            line = maze[:, index] if valid else None
            length = height
        else:
            valid = 0 <= index < height and enemy.y1 < index < enemy.y2
            center, limit = enemy.x, FIRE_X_RANGE
            line = maze[index, :] if valid else None
            length = width
        if not valid or not 0 <= center < length:
            return np.zeros(length, dtype=bool)
        positions = np.arange(length)
        # 包含玩家格和敌人中心投影格，任意一格被遮挡就不能开火。
        prefix = np.concatenate(([0], np.cumsum(line != 0)))
        blocked = prefix[np.maximum(positions, center) + 1] - prefix[np.minimum(positions, center)]
        return (np.abs(positions - center) <= limit) & (blocked == 0)

    def get_enemy_fire_region(self, enemy, maze=None):
        """可开火区域与 fire_distance 共用射线判定，不依赖敌人射程范围评分。"""
        if maze is None:
            maze = self.get_maze()
        height, width = maze.shape
        region = np.zeros(maze.shape, dtype=bool)
        for x in range(max(0, enemy.x1 + 1), min(width, enemy.x2)):
            region[:, x] = self._fire_axis_mask(maze, enemy, x, 0)
        horizontal_x = ~((enemy.x1 < np.arange(width)) & (np.arange(width) < enemy.x2))
        for y in range(max(0, enemy.y1 + 1), min(height, enemy.y2)):
            region[y, :] |= self._fire_axis_mask(maze, enemy, y, 1) & horizontal_x
        return region

    def fire_distance(self, player, enemy):
        maze = self.get_maze()
        height, width = maze.shape
        if not (0 <= player.x < width and 0 <= player.y < height):
            return None, None
        if enemy.x1 < player.x < enemy.x2:
            if self._fire_axis_mask(maze, enemy, player.x, 0)[player.y]:
                return enemy.y - player.y, 0
        elif enemy.y1 < player.y < enemy.y2:
            if self._fire_axis_mask(maze, enemy, player.y, 1)[player.x]:
                return enemy.x - player.x, 1
        return None, None


    def fire_direction(self, player, enemy_list):
        min_distance = MAZE_HEIGHT + MAZE_WIDTH
        direction = None

        for enemy in enemy_list:
            if enemy is None:
                continue
            distance, row = self.fire_distance(player, enemy)
            if distance is not None:
                if abs(distance) < min_distance:
                    min_distance = abs(distance)
                    if row:
                        if distance > 0:
                            direction = Direction.Right
                        else:
                            direction = Direction.Left
                    else:
                        if distance > 0:
                            direction = Direction.Down
                        else:
                            direction = Direction.Up

        return direction

    def is_collision_row(self, maze, x1, x2, y):
        if x1 > x2:
            return not np.any(maze[y, x2:x1])
        else:
            return not np.any(maze[y, x1:x2])

    def is_collision_col(self, maze, y1, y2, x):
        if y1 > y2:
            return not np.any(maze[y2:y1, x])
        else:
            return not np.any(maze[y1:y2, x])

    def collision_row_range(self, maze, x, left, right, y):
        row = maze[y]
        # 向左搜索
        left_pos = -1
        for i in range(x, left, -1):
            if row[i] != 0:
                left_pos = i
                break

        # 向右搜索
        right_pos = -1
        for i in range(x, right):
            if row[i] != 0:
                right_pos = i
                break

        # 如果没有找到非零元素，则返回原范围
        if left_pos == -1 and right_pos == -1:
            return left, right

        # 返回找到的结果
        return left_pos if left_pos != -1 else left, right_pos if right_pos != -1 else right

    def collision_col_range(self, maze, y, up, down, x):
        col = maze[:, x]
        # 向上搜索
        up_pos = -1
        for i in range(y, up, -1):
            if col[i] != 0:
                up_pos = i
                break

        # 向下搜索
        down_pos = -1
        for i in range(y, down):
            if col[i] != 0:
                down_pos = i
                break

        # 如果没有找到非零元素，则返回原范围
        if up_pos == -1 and down_pos == -1:
            return up, down

        # 返回找到的结果
        return up_pos if up_pos != -1 else up, down_pos if down_pos != -1 else down

    def get_direction_from_next_point(self, player, next_point):
        if next_point[0] > player.x:
            if next_point[1] > player.y:
                return Direction.DownRight
            elif next_point[1] < player.y:
                return Direction.UpRight
            else:
                return Direction.Right
        elif next_point[0] < player.x:
            if next_point[1] > player.y:
                return Direction.DownLeft
            elif next_point[1] < player.y:
                return Direction.UpLeft
            else:
                return Direction.Left
        else:
            if next_point[1] > player.y:
                return Direction.Down
            else:
                return Direction.Up

    def get_next_direction(self, player, falling_items, last_direction=None):
        direction = self.nearly_exit(player)
        if direction is not None:
            return direction, []

        if self.level == 4:
            end = (MAZE_WIDTH * (END_LEFT + END_RIGHT) / 2, MAZE_HEIGHT * (END_UP + END_DOWN) / 2)
        else:
            if falling_items:
                end = (falling_items[0].x, falling_items[0].y)
            elif self.next_exit == 0:
                end = (MAZE_WIDTH - 1, MAZE_HEIGHT * (EXIT_UP + EXIT_DOWN) / 2)
            elif self.next_exit == 1:
                end = (MAZE_WIDTH * (EXIT_LEFT + EXIT_RIGHT) / 2, MAZE_HEIGHT - 1)
            elif self.next_exit == 2:
                end = (0, MAZE_HEIGHT * (EXIT_UP + EXIT_DOWN) / 2)
            else:
                end = (MAZE_WIDTH * (EXIT_LEFT + EXIT_RIGHT) / 2, 0)

        start = (player.x, player.y)
        path = maze_utils.find_path_to_point(self.get_maze(), start, end, distance_maze=self.get_distance_maze(),
                                             hate_factor=8, last_direction=get_direction_vector(last_direction))
        next_point = path[0]
        direction = self.get_direction_from_next_point(player, next_point)
        return direction, path

    def nearly_exit(self, player):
        dx, dy = player.relative_center_position()
        if self.level == 0:
            if self.next_exit in {1, 3}:
                if dx == 1:
                    return Direction.Left
                elif dx == -1:
                    return Direction.Right
                else:
                    return direction_list[self.next_exit]
            elif self.next_exit in {0, 2}:
                if dy == 1:
                    return Direction.Up
                elif dy == -1:
                    return Direction.Down
                else:
                    return direction_list[self.next_exit]

        elif self.level == 4:
            if END_LEFT < player.xn < END_RIGHT and END_UP < player.yn < END_DOWN:
                return Direction.Up
        return None

    def update(self, player, bullets, enemy_list):
        self.flee_mazes = self.get_flee_maze(player, bullets, enemy_list)

    @staticmethod
    def get_bullet_danger_bounds(bullet):
        """前进危险带后方延伸原始 HIT 范围；侧向含余量，返回裁剪后的半开边界。"""
        left, right = bullet.x - BULLET_AVOID_WIDTH, bullet.x + BULLET_AVOID_WIDTH + 1
        up, down = bullet.y - BULLET_AVOID_HEIGHT, bullet.y + BULLET_AVOID_HEIGHT + 1
        if bullet.d == 0:
            left = bullet.x - HIT_WIDTH
            right = bullet.x + FIRE_X_RANGE + 1
        elif bullet.d == 1:
            up = bullet.y - HIT_HEIGHT
            down = bullet.y + FIRE_Y_RANGE + 1
        elif bullet.d == 2:
            left = bullet.x - FIRE_X_RANGE
            right = bullet.x + HIT_WIDTH + 1
        elif bullet.d == 3:
            up = bullet.y - FIRE_Y_RANGE
            down = bullet.y + HIT_HEIGHT + 1
        return max(0, left), max(0, up), min(MAZE_WIDTH, right), min(MAZE_HEIGHT, down)

    def get_flee_maze(self, player, bullets, enemy_list):
        origin_maze = self.get_maze()
        can_fire_region = np.zeros(origin_maze.shape, dtype=bool)
        enemy_fire_regions = []
        for enemy in enemy_list:
            if enemy is not None:
                enemy.prepare_navigation(origin_maze)
                fire_region = self.get_enemy_fire_region(enemy, origin_maze)
                can_fire_region |= fire_region
                enemy_fire_regions.append((enemy, fire_region))
        moveable_maze = np.logical_or(origin_maze, self.map_template_without_exit)
        obstacle_distance_maze = self.get_distance_maze().copy()
        end_region = np.ones((MAZE_HEIGHT, MAZE_WIDTH), dtype=np.float32)

        hit_height = BULLET_AVOID_HEIGHT
        hit_width = BULLET_AVOID_WIDTH

        flee_region_bullet = np.zeros((MAZE_HEIGHT, MAZE_WIDTH), dtype=np.int64)
        if self.level != 4:
            bullet_const = 0
        else:
            bullet_const = 50
        for bullet in bullets:
            left, up, right, down = self.get_bullet_danger_bounds(bullet)
            # 以子弹位置计算梯度，避免地图裁剪改变同一位置的危险分值。
            if bullet.d in (0, 2):
                offset = (np.arange(left, right) - bullet.x) * (1 if bullet.d == 0 else -1)
                weights = np.maximum(1, FIRE_X_RANGE + 1 - offset)
            else:
                offset = (np.arange(up, down) - bullet.y) * (1 if bullet.d == 1 else -1)
                weights = np.maximum(1, FIRE_Y_RANGE + 1 - offset)[:, None]
            flee_region_bullet[up:down, left:right] += weights + bullet_const
            collision_left = max(0, bullet.x - hit_width)
            collision_right = min(MAZE_WIDTH, bullet.x + hit_width + 1)
            collision_up = max(0, bullet.y - hit_height)
            collision_down = min(MAZE_HEIGHT, bullet.y + hit_height + 1)
            # 已覆盖玩家的子弹只保留危险成本，避免起点被动态硬障碍困住。
            # 仅跳过本颗子弹的障碍写入，不清除静态墙或其他子弹的障碍。
            player_inside = (collision_left <= player.x < collision_right
                             and collision_up <= player.y < collision_down)
            if not player_inside:
                moveable_maze[collision_up:collision_down, collision_left:collision_right] = True
            obstacle_distance_maze[max(0, bullet.y - 4 * hit_height):min(MAZE_HEIGHT, bullet.y + 4 * hit_height + 1),
                                   max(0, bullet.x - 4 * hit_width):min(MAZE_WIDTH, bullet.x + 4 * hit_width + 1)] = 0

        flee_region_enemy = np.zeros((MAZE_HEIGHT, MAZE_WIDTH), dtype=np.float32)
        if self.level != 4:
            flee_const = 40
        else:
            flee_const = 56
        for enemy in enemy_list:
            if enemy is not None:
                # 刚开火时保留一半避让范围，随冷却进度线性恢复到最大值。
                cooled_ratio = np.clip(1.0 - enemy.fire_cool_down / enemy.cool_down_time, 0.0, 1.0)
                enemy_distance_r = flee_const * (0.5 + 0.5 * cooled_ratio) - enemy.distance_array
                enemy_distance_r[enemy_distance_r < 0] = 0
                flee_region_enemy += enemy_distance_r

        peek_region_enemy = np.zeros((MAZE_HEIGHT, MAZE_WIDTH), dtype=np.int64)
        peek_cross_enemy = np.zeros((MAZE_HEIGHT, MAZE_WIDTH), dtype=np.int64)
        peek_can_fire_region = np.zeros(origin_maze.shape, dtype=bool)
        for enemy, fire_region in enemy_fire_regions:
            vision = enemy.get_vision_region(origin_maze.shape, boss=self.level == 4)
            peek_region_enemy += vision
            # 严格框内对齐与实际射击规则一致；先按同一敌人求交，再合并。
            aligned = np.zeros(origin_maze.shape, dtype=bool)
            aligned[max(0, enemy.y1 + 1):min(MAZE_HEIGHT, enemy.y2), :] = True
            aligned[:, max(0, enemy.x1 + 1):min(MAZE_WIDTH, enemy.x2)] = True
            cross = vision & aligned
            peek_cross_enemy += cross
            peek_can_fire_region |= cross & fire_region

        for enemy in enemy_list:
            if enemy is not None:
                if enemy.is_safe():
                    end_region[enemy.fire_range < 1] *= 0.92
                else:
                    end_region *= enemy.fire_range
                    if self.level == 4:
                        end_region[:enemy.y2, enemy.x1:enemy.x2] = 0

        end_region[end_region == 1] = 0
        # 射程范围只负责评分，不能把未对齐、超射程或被遮挡的位置作为射击终点。
        end_region[~can_fire_region] = 0
        distance_array = np.abs(player.x - np.arange(MAZE_WIDTH)) + np.abs(
            player.y - np.arange(MAZE_HEIGHT)[:, np.newaxis])
        decay_array = MAZE_HEIGHT / (distance_array + MAZE_HEIGHT)
        end_region *= decay_array
        end_region[flee_region_bullet > 0] = 0
        end_region[flee_region_enemy > 0] = 0
        end_peek_region = np.zeros((MAZE_HEIGHT, MAZE_WIDTH), dtype=np.float32)
        mask = peek_can_fire_region
        end_peek_region[mask] = end_region[mask]
        # 局部终点先从共同评分中提取；全局终点仅保留有效视野之外的位置。
        end_region[peek_region_enemy > 0] = 0

        """
        调试图由 cv2.imshow 按 BGR 显示。
        peek_region_enemy区域为淡灰色，优先级最低
        flee_region_bullet区域为黄色
        flee_region_enemy区域为红色
        end_peek_region区域为黄色
        end_region区域为紫色
        """
        if self.render_enabled:
            self.debug_image = np.zeros((MAZE_HEIGHT, MAZE_WIDTH, 3), dtype=np.uint8)
            peek_region_enemy_image = peek_region_enemy / (np.max(peek_region_enemy) + 1)
            mask = peek_region_enemy > 0
            peek_gray = (30 + 40 * peek_region_enemy_image).astype(np.uint8)
            self.debug_image[mask] = np.stack([peek_gray, peek_gray, peek_gray], axis=-1)[mask]
            flee_region_enemy_image = flee_region_enemy / (np.max(flee_region_enemy) + 1)
            mask = flee_region_enemy > 0
            self.debug_image[mask] = np.stack([flee_region_enemy_image, flee_region_enemy_image, 255 * flee_region_enemy_image], axis=-1).astype(np.uint8)[mask]
            mask = end_region > 0
            self.debug_image[mask] = np.stack([255 * end_region, end_region, 255 * end_region], axis=-1).astype(np.uint8)[mask]
            mask = end_peek_region > 0
            self.debug_image[mask] = np.stack([end_peek_region, 255 * end_peek_region, 255 * end_region], axis=-1).astype(np.uint8)[mask]
            flee_region_bullet_image = flee_region_bullet / (np.max(flee_region_bullet) + 1)
            mask = flee_region_bullet > 0
            self.debug_image[mask] = np.stack([flee_region_bullet_image, 255 * flee_region_bullet_image, 255 * flee_region_bullet_image], axis=-1).astype(np.uint8)[mask]
            # 最后叠加白色原始 HIT 范围；这是控制器估计，并非游戏引擎碰撞框。
            for bullet in bullets:
                left, right = max(0, bullet.x - HIT_WIDTH), min(MAZE_WIDTH - 1, bullet.x + HIT_WIDTH)
                up, down = max(0, bullet.y - HIT_HEIGHT), min(MAZE_HEIGHT - 1, bullet.y + HIT_HEIGHT)
                self.debug_image[up, left:right + 1] = (255, 255, 255)
                self.debug_image[down, left:right + 1] = (255, 255, 255)
                self.debug_image[up:down + 1, left] = (255, 255, 255)
                self.debug_image[up:down + 1, right] = (255, 255, 255)
        else:
            self.debug_image = None

        return moveable_maze, obstacle_distance_maze, flee_region_bullet, flee_region_enemy, peek_region_enemy, peek_cross_enemy, end_peek_region, end_region

    def get_shot_place_direction(self, player, last_direction=None):
        last_direction = get_direction_vector(last_direction)
        moveable_maze, obstacle_distance_maze, flee_region_bullet, flee_region_enemy, peek_region_enemy, _, end_peek_region, end_region = self.flee_mazes
        flee_region = flee_region_bullet + flee_region_enemy

        # 逃离子弹和敌人
        if flee_region[player.y, player.x] > 0:
            flee_path = maze_utils.find_nearest_passable_path(moveable_maze, (player.x, player.y), flee_region,
                                                              distance_maze=obstacle_distance_maze, hate_factor=5,
                                                              last_direction=last_direction)
            if flee_path:
                return self.get_direction_from_next_point(player, flee_path[0]), flee_path, 1

        in_vision = peek_region_enemy[player.y, player.x] > 0
        target_region = end_peek_region if in_vision else end_region
        # 已处于当前搜索范围内的安全射击位置就停留。
        if (not moveable_maze[player.y, player.x]
                and flee_region[player.y, player.x] == 0
                and target_region[player.y, player.x] > 0):
            return None, [], 0

        # 寻路的危险屏蔽不能污染缓存，否则下一次脱险仍会被旧屏蔽困住。
        moveable_maze = moveable_maze.copy()
        # 射击寻路仅屏蔽玩家上下左右各 10 格内的危险区域。
        height, width = moveable_maze.shape
        left, right = max(0, player.x - 10), min(width, player.x + 11)
        up, down = max(0, player.y - 10), min(height, player.y + 11)
        moveable_maze[up:down, left:right] |= flee_region[up:down, left:right] > 0

        # 视野内选择局部终点，但允许在整张可通行地图绕路；无路时原地等待。
        if in_vision:
            path = maze_utils.find_best_path_to_region(
                moveable_maze, (player.x, player.y), end_peek_region,
                distance_maze=obstacle_distance_maze, hate_factor=5,
                last_direction=last_direction,
            )
            if path:
                return self.get_direction_from_next_point(player, path[0]), path, 3
            return None, [], 3

        path = maze_utils.find_best_path_to_region(moveable_maze, (player.x, player.y), end_region,
                                                   distance_maze=obstacle_distance_maze, hate_factor=5,
                                                   last_direction=last_direction,
                                                   cost_map=2.0 * peek_region_enemy)

        if path:
            return self.get_direction_from_next_point(player, path[0]), path, 4
        return None, [], -1
