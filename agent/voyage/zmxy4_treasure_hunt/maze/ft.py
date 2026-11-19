"""截图、识别、规划与执行的主循环。"""

import math
import time

import cv2
import numpy as np

from zmxy4_treasure_hunt.window_permissions import ensure_window_permission

from .runtime.capture import ScreenCapture
from .runtime.controls import MovementController
from .debug.diagnostics import export_alignment_debug
from .vision.mask import MaskRestorer
from .navigation import Maze, load_weekday_map
from .vision import detect_walls, locate_character, locate_game

WINDOW_NAME = "Monitoring Window"
# BGR：未知区域、房间、墙壁、通道、宝箱、人物。
MAP_COLORS = np.array([
    (0, 0, 0),
    (235, 235, 235),
    (0, 0, 0),
    (235, 235, 235),
    (0, 190, 255),
    (60, 210, 60),
], dtype=np.uint8)


class FindTreasure:
    def __init__(self, *, capture=None, controller=None, outer_window_box=None, interval=0.05, map_template=None, debug=False):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval 必须为正数且有限")
        self.interval = interval
        self.debug = debug
        self.template = map_template if map_template is not None else load_weekday_map()
        self.capture = capture if capture is not None else ScreenCapture()
        self.controller = controller if controller is not None else MovementController()
        self.closed = False
        try:
            first, last = locate_game(self.capture.grab(outer_window_box))
            left, top = outer_window_box[:2] if outer_window_box is not None else (0, 0)
            self.region = (first[0] + left, first[1] + top,
                           last[0] + left, last[1] + top)
            if (len(self.region) != 4 or not all(isinstance(x, int) for x in self.region)
                    or self.region[0] < 0 or self.region[1] < 0
                    or self.region[2] <= self.region[0] or self.region[3] <= self.region[1]):
                raise ValueError("region 必须为显示器内的整数坐标 (left, top, right, bottom)")
            self.width = self.region[2] - self.region[0]
            self.height = self.region[3] - self.region[1]
            self.mask_restorer = MaskRestorer(self.width, self.height)
            self.maze = Maze(self.width, self.height, self.template.cells)
        except BaseException:
            self.close()
            raise
        print("定位成功，窗口坐标为：", self.region)
        print(f"已加载完整地图：{self.template.name}（{self.template.weekday}.json）")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.controller.stop()
        finally:
            self.capture.close()

    def main(self):
        try:
            left, top, right, bottom = self.region
            ensure_window_permission(((left + right) // 2, (top + bottom) // 2))
            if self.debug:
                cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
                cv2.moveWindow(WINDOW_NAME, 0, 0)
                cv2.resizeWindow(WINDOW_NAME, 290, 322)
                cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_TOPMOST, 1)
            self.controller.click((left + right) // 2, (top + bottom) // 2)
            time.sleep(0.1)
            self.monitor()
        finally:
            try:
                self.close()
            finally:
                if self.debug:
                    cv2.destroyAllWindows()

    def step(self):
        """执行一帧；识别或规划失败时立即松开方向键。"""
        raw_image = frame = character = observation = None
        self.maze.last_view = self.maze.last_view_char = self.maze.last_alignment = None
        try:
            raw_image = self.capture.grab(self.region)
            frame = self.mask_restorer.restore(raw_image)
            character = locate_character(frame.image, frame.valid_pixels)
            observation = detect_walls(frame.image, character, frame.valid_pixels)
            result = self.maze.analyze(observation, character)
            self.controller.apply(result.movements)
            self.maze.record_movements(result.movements)
            return result
        except BaseException as exc:
            self.controller.stop()
            if self.debug and isinstance(exc, ValueError):
                try:
                    output, _ = export_alignment_debug(
                        self.maze, self.template, exc, raw=raw_image, frame=frame,
                        character=character, observation=observation, region=self.region)
                    exc.add_note(f"诊断现场已保存：{output}")
                    print(f"诊断现场已保存：{output}")
                except Exception as save_error:
                    exc.add_note(f"诊断现场保存失败：{save_error}")
            raise

    def monitor(self):
        elapsed = None
        while True:
            started = time.monotonic()
            result = self.step()
            if self.debug:
                self.render(result.image, elapsed)
                if cv2.waitKey(1) & 0xFF == 27:
                    return
            if result.complete:
                print("已检查所有宝箱")
                return
            if result.approaching_treasure:
                if not self.next():
                    print("已获得宝箱")
                    return
                # 假宝箱消失，但人物仍在原处，保留地图、格距和移动历史。
                self.maze.remove_treasure(result.path[-1])
            remaining = self.interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
            elapsed = time.monotonic() - started

    @staticmethod
    def render(maze, elapsed):
        image = np.repeat(np.repeat(MAP_COLORS[maze], 10, axis=1), 10, axis=0)
        # 独立黑色状态栏，避免 FPS 文字覆盖地图中的墙壁。
        image = cv2.copyMakeBorder(image, 32, 0, 0, 0, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        if elapsed is not None and elapsed > 0:
            cv2.putText(image, f"FPS: {1 / elapsed:.1f}", (8, 23),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.imshow(WINDOW_NAME, image)

    def next(self):
        """顶部暗场表示假宝箱：松键等待 5 秒后原地继续，最多检查八次。"""
        left, top, right, _ = self.region
        crop = (left, top, right, top + max(1, int(0.3 * self.height)))
        try:
            for _ in range(8):
                time.sleep(0.5)
                if np.mean(self.capture.grab(crop)[..., 1]) < 30:
                    self.controller.stop()
                    time.sleep(5)
                    return True
            return False
        finally:
            self.controller.stop()
