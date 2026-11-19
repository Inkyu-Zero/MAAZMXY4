import cv2
import os
import sys
import time
import numpy as np
import dxcam
import win32api
import win32con

from zmxy4_treasure_hunt.voyage.core.onnx_model import OnnxModel, DetectionResult
from zmxy4_treasure_hunt.voyage.core.action_manager import key_up_all
from zmxy4_treasure_hunt.window_permissions import ensure_window_permission


def set_cursor_pos(x, y):
    win32api.SetCursorPos((x, y))


def click(x, y):
    win32api.SetCursorPos((x, y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)


class GameWindow:
    # 按 1762 × 1106 参考窗口换算的相对坐标。
    REFRESH_CLICK_POSITIONS = (
        (859 / 1762, 969 / 1106),
        (1135 / 1762, 314 / 1106)
    )

    def __init__(self, yolo_weight_path="weights/best.onnx", *, capture_enabled=True):
        if getattr(sys, "frozen", False) and not os.path.isabs(yolo_weight_path):
            yolo_weight_path = os.path.join(os.path.dirname(sys.executable), yolo_weight_path)
        self.refresh_on_engine_timeout = False
        self.camera = None
        if capture_enabled:
            try:
                self.camera = dxcam.create()
            except Exception as exc:
                print(f"DXGI screen capture initialization failed: {exc}. Trying WinRT.")
                self.camera = dxcam.create(backend="winrt")

        self.model = OnnxModel(yolo_weight_path)

        self.window_box: list[int] | tuple[int, int, int, int] | None = None
        self.window_size: tuple[int, int] | None = None
        self.left_top_mask_rate = (0.078, 0.14)
        self.left_bottom_mask_rate = (0.326, 0.872)

        self.hit_point = None
        self.map_index = None

        self.player_box = None
        self.detections = {}
        self.enemy_boxes = None
        self.bullet_list_normal = None
        self.falling_items_normal = None

        self.items = {"pao": False, "dan": False, "fan": False}

    def init_window(self, outer_window_box=None, *, focus_game=True):
        for attempt in range(2):
            screen_image = self.capture_screen(outer_window_box)
            screen_image_hls = cv2.cvtColor(screen_image, cv2.COLOR_RGB2HLS)
            self.window_box = self.locate_window(screen_image_hls)

            if outer_window_box is not None:
                self.window_box[0] += outer_window_box[0]
                self.window_box[1] += outer_window_box[1]
                self.window_box[2] += outer_window_box[0]
                self.window_box[3] += outer_window_box[1]
            width = self.window_box[2] - self.window_box[0]
            height = self.window_box[3] - self.window_box[1]
            self.window_size = (width, height)

            print("Window Box:", self.window_box)
            print(f"Window Size: {width} x {height} = {width * height}")

            status = self.update()
            if status != -1:
                break
            if attempt == 1:
                raise GameWindowError("初始化失败：尝试启航后仍然失败")
            self.click_normal(0.245, 0.783)
            time.sleep(5)

        if focus_game:
            left, top, right, bottom = self.window_box
            ensure_window_permission(((left + right) // 2, (top + bottom) // 2))
            click((left + right) // 2, (top + bottom) // 2)

    def get_map_index_at_start(self):
        frame = self.capture_screen(box=self.window_box)
        self.map_index = self.get_map_index(frame)

        return self.map_index

    def update(self):
        return self.update_from_image(self.capture_screen(box=self.window_box))

    def update_from_image(self, image, *, conf_threshold=0.4, check_status=True):
        """原地处理 RGB 游戏截图；需要保留原图的调用方应先复制。"""
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise ValueError("Expected an RGB uint8 image with shape (H, W, 3)")
        frame = image
        self.detections = {}

        self.detect_item(frame)

        mask_width = int(round(frame.shape[1] * self.left_top_mask_rate[0]))
        mask_height = int(round(frame.shape[0] * self.left_top_mask_rate[1]))
        frame[0:mask_height, 0:mask_width] = 255
        mask_width = int(round(frame.shape[1] * self.left_bottom_mask_rate[0]))
        mask_height = int(round(frame.shape[0] * self.left_bottom_mask_rate[1]))
        frame[mask_height:, 0:mask_width] = 255

        map_contours = self.locate_map_contours(frame)
        self.hit_point = self.get_hit_point(frame)

        if check_status and (len(map_contours) != 1 or self.hit_point == 0):
            if self.is_failed(frame):
                return -2
            if self.is_successful(frame):
                return 1
            return -1

        det = self.model(frame, conf_threshold=conf_threshold)
        self.detections = det

        self.player_box = self.filter_player(det)
        self.enemy_boxes = det['enemy']
        self.falling_items_normal = self.filter_falling_items(det)
        self.bullet_list_normal = self.filter_bullet(det, frame)

        # 首次尚无玩家位置时，交给环境重试，避免构造 Player(None, maze)。
        # 离线强制检测仍可查看没有玩家的图片；后续漏检沿用上一帧玩家框。
        if check_status and self.player_box is None:
            return -1

        return 0

    def _screen_point(self, xn: float, yn: float) -> tuple[int, int]:
        box, size = self.window_box, self.window_size
        if box is None or size is None:
            raise GameWindowError("游戏窗口尚未初始化，请先调用 init_window()")
        return (
            int(round(box[0] + size[0] * xn)),
            int(round(box[1] + size[1] * yn)),
        )

    def set_cursor_pos_normal(self, xn: float, yn: float) -> None:
        set_cursor_pos(*self._screen_point(xn, yn))

    def click_normal(self, xn: float, yn: float) -> None:
        click(*self._screen_point(xn, yn))

    def recover(self, status, end=False):
        time.sleep(3)
        key_up_all()

        image = self.capture_screen(box=self.window_box)
        if status == -2 or self.is_failed(image):
            print("Failed.")
            self.click_normal(0.4, 0.712)
            time.sleep(3)
        elif status == -4 and self.refresh_on_engine_timeout:
            print("Engine Time Out. Refreshing game.")
            self.refresh_game()
            self.start_hanghai(end=end)
        elif status in (-3, -4):
            print("Combat Time Out." if status == -3 else "Engine Time Out.")
            self.click_normal(0.8, 0.08)
            time.sleep(3)
            self.start_hanghai()

        elif status == 1:
            print("Successful.")
            self.click_normal(0.784, 0.765)
            time.sleep(3)
            self.start_hanghai(end=end)

    def refresh_game(self):
        """聚焦浏览器并刷新，按窗口相对坐标重新进入游戏和航海。"""
        box = self.window_box
        if box is None:
            raise GameWindowError("游戏窗口尚未初始化，请先调用 init_window()")
        left, top, right, _ = box
        click(int(round((left + right) / 2)), int(top - 10))
        win32api.keybd_event(win32con.VK_F5, 0, 0, 0)
        win32api.keybd_event(win32con.VK_F5, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(60)
        for xn, yn in self.REFRESH_CLICK_POSITIONS:
            self.click_normal(xn, yn)
            time.sleep(3)

    def start_hanghai(self, end=False):
        max_confidence = 0.6
        icon_box: DetectionResult | None = None
        for i in range(10):
            self.set_cursor_pos_normal(0.75, 0.072)
            image = self.capture_screen(self.window_box)
            det = self.model(image)
            icon_boxes = det['player']

            for icon in icon_boxes:
                if icon.conf > max_confidence:
                    max_confidence = icon.conf
                    icon_box = icon

            if icon_box is not None:
                break

            time.sleep(1)

        if icon_box is None:
            raise TimeoutError("预期在主界面找到航海图标，未找到")

        self.click_normal(icon_box.xywhn[0], icon_box.xywhn[1])

        time.sleep(1)
        self.click_normal(0.352, 0.706)
        if not end:
            time.sleep(0.2)
            self.click_normal(0.352, 0.706)

        time.sleep(3)

    def is_successful(self, image):
        success_box_rate = (0.567, 0.772, 0.434, 0.684)

        success_image = self.crop_by_rate(image, success_box_rate)
        success_mask = cv2.inRange(success_image, np.array([220, 220, 220]), np.array([255, 255, 255]))
        char_num = self.get_filtered_bounding_box_count(success_mask, min_area=10)

        if char_num > 30:
            return True
        return False

    def is_failed(self, image):
        fail_box_rate = (0.06, 0.18, 0.18, 0.72)

        fail_image = self.crop_by_rate(image, fail_box_rate)
        fail_mask = cv2.inRange(fail_image, np.array([50, 50, 50]), np.array([100, 100, 100]))
        if np.mean(fail_mask) > 200:
            return True
        return False

    def filter_player(self, det):
        player_list = det['player']
        if len(player_list) == 0:
            return self.player_box

        if len(player_list) == 1:
            return player_list[0]

        max_confidence = 0
        player = None
        for player_box in player_list:
            if player_box.conf > max_confidence:
                max_confidence = player_box.conf
                player = player_box

        return player

    def filter_falling_items(self, det):
        falling_items = []
        for key in ['dan', 'fan', 'jia', 'pao']:
            if key == 'jia':
                if self.hit_point == 4:
                    continue
            elif self.items[key]:
                continue
            for box in det[key]:
                xywhn = box.xywhn
                falling_items.append((xywhn[0], xywhn[1]))

        return falling_items

    def filter_bullet(self, det, image):
        if not det['bullet']:
            return []
        hls = cv2.cvtColor(image, cv2.COLOR_RGB2HLS)
        return self.detect_bullet_normal(hls, det['bullet'])

    def detect_bullet_normal(self, hls_image, bullet_boxes):
        """仅提取 YOLO 框内的主要弹头，以其相对框中心的偏移判定方向。"""
        height, width, _ = hls_image.shape
        head_lower = np.array([12, 70, 5])
        head_upper = np.array([32, 120, 30])
        bullet_list = []
        for box in bullet_boxes:
            x1n, y1n, x2n, y2n = box.xyxyn
            x1, x2 = np.clip([int(round(x1n * width)), int(round(x2n * width))], 0, width)
            y1, y2 = np.clip([int(round(y1n * height)), int(round(y2n * height))], 0, height)
            if x2 <= x1 or y2 <= y1:
                continue
            head_mask = cv2.inRange(hls_image[y1:y2, x1:x2], head_lower, head_upper)
            contours, _ = cv2.findContours(head_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            # 用最大有效弹头抑制框边缘的孤立噪点，每个检测框最多生成一颗子弹。
            head = max(contours, key=cv2.contourArea)
            if cv2.contourArea(head) <= 0:
                continue
            x, y, w, h = cv2.boundingRect(head)
            xc, yc = x1 + x + w / 2, y1 + y + h / 2
            dx, dy = xc - (x1 + x2) / 2, yc - (y1 + y2) / 2
            if dx == 0 and dy == 0:
                continue  # 正好位于框中心时无法根据当前帧确定方向。
            if abs(dx) >= abs(dy):
                direction = 0 if dx > 0 else 2
            else:
                direction = 1 if dy > 0 else 3
            bullet_list.append((xc / width, yc / height, direction))
        return bullet_list

    def detect_item(self, window_image):
        pao_rate = (0.0437, 0.0618, 0.906, 0.938)
        dan_rate = (0.121, 0.139, 0.904, 0.939)
        fan_rate = (0.198, 0.216, 0.906, 0.941)

        pao_image = self.crop_by_rate(window_image, pao_rate)
        dan_image = self.crop_by_rate(window_image, dan_rate)
        fan_image = self.crop_by_rate(window_image, fan_rate)

        pao_hls = cv2.cvtColor(pao_image, cv2.COLOR_RGB2HLS)
        dan_hls = cv2.cvtColor(dan_image, cv2.COLOR_RGB2HLS)
        fan_hls = cv2.cvtColor(fan_image, cv2.COLOR_RGB2HLS)

        lower = np.array([65, 15, 20])
        upper = np.array([100, 35, 150])
        pao_mask = cv2.inRange(pao_hls, lower, upper)
        dan_mask = cv2.inRange(dan_hls, lower, upper)
        fan_mask = cv2.inRange(fan_hls, lower, upper)

        self.items["pao"] = np.mean(pao_mask) < 100
        self.items["dan"] = np.mean(dan_mask) < 100
        self.items["fan"] = np.mean(fan_mask) < 100

    def capture_screen(self, box=None):
        """按需获取 RGB 截图；区域坐标相对于采集显示器，由 DXcam 校验。"""
        if self.camera is None:
            raise RuntimeError("Screen capture is disabled; use update_from_image()")
        region = tuple(box) if box is not None else None
        deadline = time.monotonic() + 3.0
        while True:
            image = self.camera.grab(region=region)

            if image is not None:
                return image

            if time.monotonic() >= deadline:
                raise TimeoutError("No screen frame received within 5 seconds")

            time.sleep(0.001)

    def locate_window(self, hls_image):
        # 二值化
        wall_lower = np.array([0, 20, 0])
        wall_upper = np.array([105, 75, 255])
        wall_mask = cv2.inRange(hls_image, wall_lower, wall_upper)

        water_lower = np.array([90, 86, 100])
        water_upper = np.array([105, 200, 255])
        water_mask = cv2.inRange(hls_image, water_lower, water_upper)

        mask = cv2.bitwise_or(wall_mask, water_mask)

        # 膨胀
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        box = self.get_window_box(mask)
        return box

    def get_hit_point(self, window_image):
        rate = (0.09, 0.21, 0.038, 0.065)
        health_image = self.crop_by_rate(window_image, rate)

        # 二值化
        health_lower = np.array([150, 0, 0])
        health_upper = np.array([255, 200, 80])
        health_mask = cv2.inRange(health_image, health_lower, health_upper)

        min_area = health_image.shape[0] * health_image.shape[1] * 0.0005
        hit_point = self.get_filtered_bounding_box_count(health_mask, min_area=min_area)
        return hit_point

    def locate_map_contours(self, window_image):
        xr0 = 0.90
        yr0 = 0.055
        rate = (xr0, 0.975, yr0, 0.155)
        map_image = self.crop_by_rate(window_image, rate)

        # 转换为灰度图像
        gray = cv2.cvtColor(map_image, cv2.COLOR_RGB2GRAY)

        # 使用阈值处理，提取白色区域
        _, thresh = cv2.threshold(gray, 254, 255, cv2.THRESH_BINARY)

        # 查找轮廓
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        return contours

    def get_map_index(self, window_image):
        xr0 = 0.90
        yr0 = 0.055
        contours = self.locate_map_contours(window_image)

        # 判断白色区域数量
        if len(contours) != 1:
            raise GameWindowError(f"Expected 1 white block in map area, but found {len(contours)}.")

        # 获取唯一白色区域的坐标
        x, y, _, _ = cv2.boundingRect(contours[0])
        height, width, _ = window_image.shape

        xr = (xr0 * width + x) / width
        yr = (yr0 * height + y) / height

        map_list = [
            (0.940, 0.0776),
            (0.914, 0.108),
            (0.931, 0.098),
            (0.931, 0.108),
            (0.948, 0.118),
            (0.931, 0.118),
            (0.922, 0.0776)
        ]
        map_index = self.find_nearest_point((xr, yr), map_list)

        return map_index

    def crop_image_by_box(self, image, box):
        x0, y0, x1, y1 = box
        return image[y0:y1, x0:x1]

    def crop_by_rate(self, image, rate):
        h, w, _ = image.shape
        return image[int(round(h * rate[2])):int(round(h * rate[3])),
                     int(round(w * rate[0])):int(round(w * rate[1]))]

    def get_centroid_and_direction(self, outer, inners):
        outer_x, outer_y, outer_w, outer_h = cv2.boundingRect(outer)
        outer_x2 = outer_x + outer_w
        outer_y2 = outer_y + outer_h
        for contour in inners:
            x, y, w, h = cv2.boundingRect(contour)
            xc = x + w / 2
            yc = y + h / 2

            if cv2.pointPolygonTest(outer, (xc, yc), False) < 0:
                continue

            if not self.is_point_in_box((xc, yc), (outer_x, outer_y, outer_x2, outer_y2)):
                continue

            angle = np.arctan2(yc - outer_y - outer_h / 2, xc - outer_x - outer_w / 2)
            angle = angle * 180 / np.pi
            if -45 <= angle <= 45:
                return xc, yc, 0
            elif 45 < angle <= 135:
                return xc, yc, 1
            elif -135 <= angle < -45:
                return xc, yc, 3
            else:
                return xc, yc, 2

        return None

    def get_filtered_bounding_box_list(self, mask, min_area=500):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        bounding_boxes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h >= min_area:
                bounding_boxes.append((x, y, w, h))

        return bounding_boxes

    def get_filtered_bounding_box_count(self, mask, min_area=100):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        count = 0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h >= min_area:
                count += 1

        return count

    def get_window_box(self, mask, min_size=(200, 200), aspect_ratio_range=(1.55, 1.65)):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        largest_box = None
        max_area = 0

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)

            if w >= min_size[0] and h >= min_size[1]:
                aspect_ratio = w / h if h != 0 else 0
                if aspect_ratio_range[0] <= aspect_ratio <= aspect_ratio_range[1]:
                    area = w * h
                    if area > max_area:
                        max_area = area
                        largest_box = (x, y, w, h)
        if largest_box is None:
            raise GameWindowError("找不到游戏窗口，请确保游戏已进入航海界面，且没有遮挡")
        return [largest_box[0], largest_box[1], largest_box[0] + largest_box[2], largest_box[1] + largest_box[3]]

    def find_nearest_point(self, point, point_list, tolerance=0.1):
        min_distance = float('inf')
        nearest_index = -1

        for i, (x, y) in enumerate(point_list):
            if abs(point[0] - x) > tolerance or abs(point[1] - y) > tolerance:
                raise GameWindowError(f"Point ({x}, {y}) at index {i} is too far from target point {point}.")

            distance = np.sqrt((point[0] - x) ** 2 + (point[1] - y) ** 2)

            if distance < min_distance:
                min_distance = distance
                nearest_index = i

        return nearest_index

    def is_point_in_box(self, point, box):
        x, y = point
        x1, y1, x2, y2 = box

        # 检查点是否在 box 的范围内
        return x1 <= x <= x2 and y1 <= y <= y2


class GameWindowError(Exception):
    pass
