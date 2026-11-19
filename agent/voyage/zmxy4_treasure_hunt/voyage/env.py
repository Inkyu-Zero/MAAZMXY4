"""游戏环境流程编排：感知更新、自动战斗、动作调度和调试渲染。"""

import time

import cv2
import numpy as np

from zmxy4_treasure_hunt.voyage.game_window import GameWindow
from zmxy4_treasure_hunt.voyage.core.action_manager import ActionManager, key_down
from zmxy4_treasure_hunt.voyage.core.constants import MAZE_HEIGHT, MAZE_WIDTH
from zmxy4_treasure_hunt.voyage.core.direction import Direction
from zmxy4_treasure_hunt.voyage.core.entities import Bullets, EnemyList, FallingItems, Player
from zmxy4_treasure_hunt.voyage.core.map import Map


class FrameRateCalculator:
    def __init__(self):
        self.reset()

    def reset(self):
        self.last_time = None
        self.fps = 0.0
        self.report_start = None
        self.report_count = 0

    def trigger(self, log_to_terminal=False):
        """每次战斗更新计一帧，终端每十秒报告一次战斗平均更新频率。"""
        current_time = time.perf_counter()
        if self.last_time is not None:
            time_difference = current_time - self.last_time
            if time_difference > 0:
                self.fps = 1 / time_difference
            self.report_count += 1
        if self.report_start is None:
            self.report_start = current_time
        elapsed = current_time - self.report_start
        if elapsed >= 10.0:
            if log_to_terminal:
                average_fps = self.report_count / elapsed
                print(f"[FPS] {average_fps:.1f}，平均更新间隔 {elapsed * 1000 / self.report_count:.1f} ms")
            self.report_start = current_time
            self.report_count = 0
        self.last_time = current_time
        return self.fps


class Voyager:
    def __init__(self, timeout=5, stay_timeout=60, render_enabled=False, combat_timeout=60):
        if not np.isfinite(combat_timeout) or combat_timeout <= 0:
            raise ValueError("combat_timeout must be positive and finite")
        self.render_enabled = render_enabled
        self.timeout = timeout
        self.game_window = GameWindow()
        self.window_size = None
        self.action_manager = ActionManager()

        self.player: Player | None = None
        self.enemy_list = EnemyList()
        self.bullets = Bullets()
        self.falling_items = FallingItems()
        self.new_level = False
        self.level_started_at = None


        self.fps_calculator = FrameRateCalculator()
        self.map = Map(render_enabled=render_enabled)
        self.stay_timeout = stay_timeout
        self.combat_timeout = combat_timeout
        self._reset_combat_timer()

    def _reset_combat_timer(self):
        self._combat_level = None
        self._combat_elapsed = 0.0
        self._combat_started_at = None

    @property
    def combat_elapsed_seconds(self):
        elapsed = self._combat_elapsed
        if self._combat_started_at is not None:
            elapsed += max(0.0, time.monotonic() - self._combat_started_at)
        return elapsed

    def _pause_combat_timer(self):
        self._combat_elapsed = self.combat_elapsed_seconds
        self._combat_started_at = None

    def _update_combat_timer(self):
        """同关累计战斗时间；空场暂停，切关重置，加载时间不计入。"""
        if self._combat_level != self.map.level:
            self._reset_combat_timer()
            self._combat_level = self.map.level
        if not 1 <= self.map.level <= 4 or self.can_next_level():
            self._pause_combat_timer()
            return 0
        if self._combat_started_at is None:
            self._combat_started_at = time.monotonic()
        if self.combat_elapsed_seconds >= self.combat_timeout:
            self._pause_combat_timer()
            return -3
        return 0

    def init_window(self, outer_window_box=None, *, focus_game=True):
        self.game_window.init_window(outer_window_box, focus_game=focus_game)
        self.window_size = self.game_window.window_size

        if self.render_enabled:
            image = np.zeros((MAZE_HEIGHT, MAZE_WIDTH, 3), dtype=np.uint8)
            cv2.imshow("Monitor", image)
            cv2.moveWindow("Monitor", 0, 0)
            cv2.setWindowProperty("Monitor", cv2.WND_PROP_TOPMOST, 1)

    def _update_combat_fps(self):
        """只统计连续战斗；过关、入场和空场等待均丢弃当前统计窗口。"""
        if 1 <= self.map.level <= 4 and not self.new_level and not self.can_next_level():
            self.fps_calculator.trigger(log_to_terminal=not self.render_enabled)
        else:
            self.fps_calculator.reset()

    def reset(self):
        self._reset_combat_timer()
        self.player = None
        self.action_manager.reset()
        self.fps_calculator.reset()
        self.enemy_list = EnemyList()
        self.bullets = Bullets()
        self.falling_items = FallingItems()

        self.new_level = False
        self.level_started_at = None

        status_code = self.update()
        if status_code:
            return status_code
        self.render()

        map_index = self.game_window.get_map_index_at_start()
        self.map.reset(map_index)
        self._reset_combat_timer()

        status_code = self.try_next_level()
        print("游戏开始！大地图：", map_index)

        return status_code

    def update(self) -> int:
        """更新感知、实体跟踪及战斗状态，供自动战斗和调试共用。"""
        status_code = self.game_window.update()
        start_time = time.monotonic()
        if status_code == -1:
            self._pause_combat_timer()
            self.fps_calculator.reset()
        while status_code == -1:
            time.sleep(0.01)
            status_code = self.game_window.update()

            if status_code == -1 and time.monotonic() - start_time >= self.timeout:
                return -4

        # 结算画面没有新的实体检测结果，保留最后一次有效的场景状态。
        if status_code:
            self._pause_combat_timer()
            self.fps_calculator.reset()
            return status_code

        maze = self.map.get_maze()

        player = Player(self.game_window.player_box, maze)
        if self.player and self.player.is_next(player):
            self.map.next_level()
            self.new_level = True
            self.level_started_at = time.monotonic()

            print(f"进入第 {self.map.level} 关：关卡类型 {self.map.get_task()}")

            # 下一关可能使用不同的地图
            maze = self.map.get_maze()
            player = Player(self.game_window.player_box, maze)

            # 清除上一关的追踪记录，避免跨关误匹配
            self.enemy_list.clear()
            self.bullets.clear()
            self.falling_items.clear()
            self.action_manager.stop_moving()

        self.player = player

        self.bullets.update(self.game_window.bullet_list_normal, self.player)
        # 子弹列表已经在 update 内部按距离排序

        current_time = time.time()
        self.enemy_list.update(self.game_window.enemy_boxes, maze, current_time=current_time)
        self.enemy_list.sort_by(self.player)

        self.action_manager.update_pao(self.game_window.items["pao"])

        self.falling_items.update(self.game_window.falling_items_normal)
        self.falling_items.sort_by(self.player)

        for bullet in self.bullets.new_bullets:
            min_distance = np.inf
            fired_enemy = None
            for enemy in self.enemy_list:
                if bullet.d == 0 and enemy.y1 < bullet.y < enemy.y2:
                    distance = bullet.x - enemy.x
                elif bullet.d == 1 and enemy.x1 < bullet.x < enemy.x2:
                    distance = bullet.y - enemy.y
                elif bullet.d == 2 and enemy.y1 < bullet.y < enemy.y2:
                    distance = enemy.x - bullet.x
                elif bullet.d == 3 and enemy.x1 < bullet.x < enemy.x2:
                    distance = enemy.y - bullet.y
                else:
                    continue
                if distance < 0:
                    continue

                if distance < min_distance:
                    min_distance = distance
                    fired_enemy = enemy
            if fired_enemy is not None:
                fired_enemy.set_fired(current_time)

        # 使动作管理器同步时间
        self.action_manager.set_action_start_time()

        # 准备fire动作
        fire_direction = self.map.fire_direction(self.player, self.enemy_list)
        self.action_manager.set_fire_direction(fire_direction)

        # 观测更新后旧危险图失效；仅自动战斗寻路会重建它。
        self.map.flee_mazes = None
        self.map.debug_image = None

        self._update_combat_fps()
        return self._update_combat_timer()

    def update_navigation(self):
        """按当前观测生成自动战斗寻路使用的危险图。"""
        self.map.update(self.player, self.bullets, self.enemy_list)

    def auto_step(self):
        status_code = self.move_to_enemy_and_update()  # 含有update

        if status_code:
            return status_code

        status_code = self.try_start_level()  # 含有update
        if status_code:
            return status_code
        status_code = self.try_next_level()  # 含有update

        return status_code

    def auto_run(self):
        """从重置开始运行一局自动战斗，返回结束状态码。"""
        status_code = self.reset()
        while status_code == 0:
            status_code = self.auto_step()
        return status_code

    def debug_step(self):
        """只更新感知、自动战斗建议路径和 Monitor；不执行移动、开火或过关。"""
        status_code = self.update()
        if status_code not in (0, -3):
            return status_code
        self.update_navigation()
        _, path, _ = self.map.get_shot_place_direction(
            self.player, last_direction=self.action_manager.last_direction
        )
        self.render(path)
        return status_code

    def try_start_level(self):
        def loop_act(direction):
            self.action_manager.take_action_direction(direction)
            self.render()

            time.sleep(0)
            return self.update()

        while self.new_level and self.player:
            if self.map.get_task() == 5:
                status_code = loop_act(None)
                if status_code:
                    return status_code
                if not self.enemy_list:
                    continue
                if self.map.map_index == 1:
                    if self.enemy_list[0].xn < 0.6:
                        continue
                elif self.map.map_index == 5:
                    if 0.4 < self.enemy_list[0].xn < 0.6:
                        continue
            elif self.map.map_index == 3:
                if self.map.level == 1:
                    direction = Direction.Down
                    self.action_manager.take_action_direction(direction)
                    time.sleep(1)
                    status_code = self.update()
                    if status_code:
                        return status_code
            elif self.map.map_index == 4:
                if self.map.level == 1:
                    status_code = loop_act(Direction.DownLeft)
                    if status_code:
                        return status_code
                    if self.player.xn > 0.75:
                        continue
                elif self.map.level == 2:
                    direction = Direction.Left
                    status_code = loop_act(direction)
                    if status_code:
                        return status_code
                    if self.player.xn > 0.3:
                        continue
            elif self.map.map_index == 5:
                if self.map.level == 2:
                    direction = Direction.Left
                    status_code = loop_act(direction)
                    if status_code:
                        return status_code
                    if self.player.xn > 0.3:
                        continue

            self.new_level = False
        return 0

    def can_next_level(self):
        return len(self.enemy_list) == 0 and len(self.bullets) == 0

    def try_next_level(self):
        """进入新关卡5秒后，使用静态地图拾取掉落物、前往出口。"""
        loop_start_time = time.monotonic()
        while self.can_next_level() and not self.new_level:
            if (
                self.level_started_at is None
                or time.monotonic() - self.level_started_at >= 5
            ):
                direction, path = self.map.get_next_direction(
                    self.player, self.falling_items, last_direction=self.action_manager.last_direction)
                self.render(path)
                self.action_manager.take_action_direction(direction)
            else:
                self.render()
            # 原来是 0.1（每帧固定睡 100ms），实测计算只要 40ms，
            # 等于 70% 的时间在睡觉，FPS 被压到 7，躲子弹反应不过来。
            # 用户要求直接关掉休眠（sleep(0) 只是让出时间片，不阻塞）。
            time.sleep(0)
            status_code = self.update()

            if status_code:
                return status_code
            if time.monotonic() - loop_start_time >= self.stay_timeout and self.can_next_level() and not self.new_level:
                return -4

        return 0

    def move_to_enemy_and_update(self):
        if self.can_next_level():
            return 0
        if self.new_level:
            # 原来这里直接 return 0，等于「新关卡还没开完场，什么都不做」。
            # 而 new_level 只在 try_start_level 的循环末尾才被清掉，那个循环的
            # 条件是 `while self.new_level and self.player` —— 切场景那一帧没认到
            # 玩家，循环直接退出、new_level 永远保持 True，船就站着不动直到被打死
            # （用户 m26985：第四关见到 boss 后没动被打死）。
            status_code = self.try_start_level()
            if status_code:
                return status_code
            if self.new_level:
                print("[开局] 开场走位没能结束（没认出玩家？），强制进入战斗", flush=True)
                self.new_level = False

        self.update_navigation()
        direction, path, _ = self.map.get_shot_place_direction(
            self.player, last_direction=self.action_manager.last_direction
        )
        if direction is None:
            self.action_manager.take_action_direction(None)
            self.render()
            status_code = self.update()
            return status_code

        self.action_manager.take_action_direction(direction)
        self.render(path)

        status_code = self.update()

        return status_code


    def render(self, path=None):
        if not self.render_enabled:
            return

        image = np.zeros((MAZE_HEIGHT, MAZE_WIDTH, 3), dtype=np.uint8)
        if self.map.debug_image is not None:
            image += self.map.debug_image

        if self.player:
            cv2.circle(image, (self.player.x, self.player.y), 4, (0, 255, 0), -1)
            cv2.circle(image, (self.player.x, self.player.y), 1, (0, 0, 255), -1)
        for enemy in self.enemy_list:
            cv2.circle(image, (enemy.x, enemy.y), 4, (0, 0, 255), -1)
            cv2.rectangle(image, (enemy.x1, enemy.y1), (enemy.x2, enemy.y2), (0, 0, 255), 2)
        for bullet in self.bullets:
            cv2.circle(image, (bullet.x, bullet.y), 2, (0, 255, 255), -1)
            direction_delta = ((5, 0), (0, 5), (-5, 0), (0, -5))
            dx, dy = direction_delta[bullet.d]
            cv2.line(image, (bullet.x - dx, bullet.y - dy), (bullet.x, bullet.y), (0, 255, 255), 1)
        for bullet in self.bullets.safe_bullets:
            cv2.circle(image, (bullet.x, bullet.y), 2, (0, 255, 0), -1)
            direction_delta = ((5, 0), (0, 5), (-5, 0), (0, -5))
            dx, dy = direction_delta[bullet.d]
            cv2.line(image, (bullet.x - dx, bullet.y - dy), (bullet.x, bullet.y), (0, 255, 0), 1)
        for item in self.falling_items:
            cv2.circle(image, (item.x, item.y), 4, (255, 255, 0), -1)

        # 绘制迷宫
        maze = self.map.get_maze()
        image[maze] = 128

        if path:
            for i in range(len(path) - 1):
                cv2.line(image, path[i], path[i + 1], (255, 0, 0), 1)
            cv2.circle(image, path[-1], 2, (255, 0, 0), -1)

        fps = self.fps_calculator.fps

        # 在左上角显示 HP
        cv2.putText(image, f"HP: {self.game_window.hit_point}", (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255),
                    1)
        # 在右上角显示地图序列d
        cv2.putText(image, f"Map: {self.game_window.map_index}", (250, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1)
        # 在右上角显示关卡
        cv2.putText(image, f"Level: {self.map.level}", (250, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        # 在关卡下方显示当前布局模板编号
        cv2.putText(image, f"Task: {self.map.get_task()}", (250, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1)
        # 在右下角显示帧率
        cv2.putText(image, f"FPS: {fps:.1f}", (250, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        # 左下角显示拥有物品
        for i, value in enumerate(self.game_window.items.values()):
            if value:
                thickness = -1
            else:
                thickness = 1
            cv2.rectangle(image, (5 + i * 15, 180), (15 + i * 15, 190), (255, 255, 255), thickness)

        cv2.imshow("Monitor", image)
        cv2.waitKey(1)


def auto_loop(voyager: Voyager, target=3):
    # 传入已经初始化好窗口的 Voyager 实例。
    success_count = 0
    if target <= 0:
        return
    print("Start Loop")
    while success_count < target:
        print(f"Success {success_count}/{target}")
        status_code = voyager.auto_run()
        if status_code == 1:
            success_count += 1
        time.sleep(1)
        voyager.game_window.recover(status_code)
