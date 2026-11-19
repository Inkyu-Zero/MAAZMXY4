"""方向按键、开火冷却和临时转向管理。"""

import time

import win32api
import win32con

from .direction import (is_reverse_direction, is_single_direction,
                        is_horizontal_direction, get_horizontal_and_vertical_direction)


def key_down(string):
    """模拟按下键"""
    for char in string:
        vk_code = ord(char.upper())
        win32api.keybd_event(vk_code, 0, 0, 0)


def key_up(string):
    """模拟释放键"""
    for char in string:
        vk_code = ord(char.upper())
        win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)


def move_to(direction):
    if direction is None:
        return
    # direction.value 可能是 "SD" 这种两字符斜向，key_down 已经支持拆开按
    key_down(direction.value)


def stop_to(direction):
    if direction is None:
        return
    key_up(direction.value)


def key_up_all():
    for char in "WASDJ":
        key_up(char)


def click(x, y):
    win32api.SetCursorPos((x, y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)


class ActionManager:
    def __init__(self, action_interval=0.03):
        self.action_interval = action_interval

        self.fire_interval = 0.6
        self.pao = False
        self.last_fire_time = time.time()
        self.last_direction = None

        self.fire_reward = 0
        self.fire_direction = None

        self.action_start_time = time.time()

    def reset(self):
        self.stop_moving()
        self.fire_interval = 0.6
        self.pao = False
        self.fire_reward = 0

    def stop_moving(self):
        """释放移动按键，同时清空方向记录，不执行开火。"""
        stop_to(self.last_direction)
        self.last_direction = None

    def _can_fire(self):
        return time.time() - self.last_fire_time > self.fire_interval

    def get_fire_cool_down(self):
        return max(0.0, self.last_fire_time + self.fire_interval - time.time())

    def update_pao(self, pao):
        if self.pao == pao:
            return

        self.pao = pao
        if pao:
            self.fire_interval = 0.4
        else:
            self.fire_interval = 0.6

    def get_fire_direction_adjustment(self, fire_direction):
        if len(fire_direction.value) != 1:
            raise ValueError("Invalid fire direction.")
        if self.last_direction is None:
            return None, fire_direction

        # 处理单方向移动时
        if is_single_direction(self.last_direction):
            if fire_direction == self.last_direction:
                return None, None
            elif is_reverse_direction(self.last_direction, fire_direction):
                return None, fire_direction
            elif is_horizontal_direction(fire_direction):
                return None, fire_direction
            else:
                return self.last_direction, fire_direction

        # 处理双方向移动时
        if is_horizontal_direction(fire_direction):
            _, move_vertical = get_horizontal_and_vertical_direction(self.last_direction)
            if fire_direction == move_vertical:
                return None, None
            return None, fire_direction
        else:
            move_horizontal, move_vertical = get_horizontal_and_vertical_direction(self.last_direction)
            if fire_direction == move_vertical:
                return move_horizontal, None
            return move_horizontal, fire_direction

    def modify_direction(self, direction):
        if direction is None:
            return self.last_direction, None
        if self.last_direction is None:
            return None, direction
        if direction == self.last_direction:
            return None, None

        old_horizontal, old_vertical = get_horizontal_and_vertical_direction(self.last_direction)
        new_horizontal, new_vertical = get_horizontal_and_vertical_direction(direction)

        # 保留相同轴上的按键，只切换发生变化的方向。
        if old_horizontal == new_horizontal:
            return old_vertical, new_vertical
        if old_vertical == new_vertical:
            return old_horizontal, new_horizontal
        return self.last_direction, direction

    def set_action_start_time(self):
        self.action_start_time = time.time()

    def set_fire_direction(self, direction):
        self.fire_direction = direction

    def fire(self):
        if self._can_fire():
            if self.fire_direction is None:
                # 寻找射击位置或撤离射击线不扣分。
                self.fire_reward = 0.0
                return 0

            # 能开火，有开火机会，奖励1分
            self.fire_reward = 1

            fire_suspend_direction, fire_temporary_direction = self.get_fire_direction_adjustment(self.fire_direction)
            stop_to(fire_suspend_direction)
            move_to(fire_temporary_direction)
            time.sleep(0.038)
            key_down("J")
            self.last_fire_time = time.time()
            time.sleep(0.002)

            stop_to(fire_temporary_direction)
            move_to(fire_suspend_direction)
            key_up("J")

            return 1

        # 冷却由引擎控制，无论是否对齐敌人均不计奖惩。
        self.fire_reward = 0.0
        return 0

    def take_action_direction(self, direction):
        self.fire()

        stop_direction, move_direction = self.modify_direction(direction)
        stop_to(stop_direction)
        move_to(move_direction)
        time.sleep(0.003)
        move_to(direction)
        self.last_direction = direction

        remaining_time = self.action_start_time + self.action_interval - time.time()
        if remaining_time > 0:
            time.sleep(remaining_time)
