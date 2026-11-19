"""从墙壁线条重建格网，并把图像观测转换成局部迷宫。"""

import logging

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class GridDetectionError(ValueError):
    """当前画面不足以可靠重建格网。"""


class Grid:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.vertical_lines = None
        self.horizontal_lines = None
        self.vertical_start_small = True
        self.horizontal_start_small = True
        self.interval0x = self.interval0y = 0
        self.interval1x = self.interval1y = 0

    def plot(self, img):
        h, w = img.shape
        for x in self.vertical_lines:
            cv2.line(img, (x, 0), (x, h), 100, 2)
        for y in self.horizontal_lines:
            cv2.line(img, (0, y), (w, y), 100, 2)

        return img

    def analyze(self, image, char_coord, treasure_coords, valid_pixels=None):
        if self.horizontal_lines is None or self.vertical_lines is None:
            raise GridDetectionError("尚未检测到格网")
        rows = len(self.horizontal_lines) - 1
        cols = len(self.vertical_lines) - 1
        view = np.zeros((rows, cols), dtype=np.uint8)

        view[self.horizontal_start_small::2, self.vertical_start_small::2] = 1
        view[1-self.horizontal_start_small::2, 1-self.vertical_start_small::2] = 2

        for r in range(rows):
            for c in range(cols):
                if view[r, c] == 0:
                    cell = image[self.horizontal_lines[r]:self.horizontal_lines[r + 1],
                                 self.vertical_lines[c]:self.vertical_lines[c + 1]]
                    if cell.size == 0:
                        raise GridDetectionError("格网包含空单元格")
                    if valid_pixels is not None:
                        visible = valid_pixels[self.horizontal_lines[r]:self.horizontal_lines[r + 1],
                                               self.vertical_lines[c]:self.vertical_lines[c + 1]]
                        if np.mean(visible) < 0.6:
                            # 0 只表示本帧无法观测；完整地图不会因此被修改。
                            continue
                        cell = cell[visible]
                    avg_val = np.mean(cell)
                    view[r, c] = 2 if avg_val > 100 else 3

        # 标注宝箱位置
        for treasure_coord in treasure_coords:
            treasure_row = np.digitize([treasure_coord[1]], self.horizontal_lines)[0] - 1
            treasure_col = np.digitize([treasure_coord[0]], self.vertical_lines)[0] - 1
            # 判断坐标是否越界
            if 0 <= treasure_row < view.shape[0] and 0 <= treasure_col < view.shape[1]:
                view[treasure_row, treasure_col] = 4

        # 标注人物位置
        view_char, offset = self.calculate_character_position(char_coord)
        return view, view_char, offset

    def calculate_character_position(self, char_coord):
        char_row = np.digitize([char_coord[1]], self.horizontal_lines)[0] - 1
        char_col = np.digitize([char_coord[0]], self.vertical_lines)[0] - 1
        if not (0 <= char_row < len(self.horizontal_lines) - 1 and
                0 <= char_col < len(self.vertical_lines) - 1):
            raise GridDetectionError("人物位置超出可见格网")
        if char_row % 2 == self.horizontal_start_small and char_col % 2 == self.vertical_start_small:
            # 计算格子的中心点坐标
            cell_center_x = self.vertical_lines[char_col] + self.interval1x / 2
            cell_center_y = self.horizontal_lines[char_row] + self.interval1y / 2
            # 计算相对于格子中心的偏移
            offset_x = char_coord[0] - cell_center_x
            offset_y = char_coord[1] - cell_center_y
            # 计算相对的偏移度
            offset_degree_x = offset_x / self.interval1x * 2
            offset_degree_y = offset_y / self.interval1y * 2

            return (char_row, char_col), (offset_degree_x, offset_degree_y)
        else:
            return (char_row, char_col), (0, 0)

    def update(self, vertical_lines, horizontal_lines):
        def interval(lines):
            merged = [lines[0]]
            for line in lines[1:]:
                if line - merged[-1] > 4:
                    merged.append(line)
            return np.asarray(merged), np.diff(merged)

        vertical_lines = np.sort(np.asarray(vertical_lines, dtype=float))
        horizontal_lines = np.sort(np.asarray(horizontal_lines, dtype=float))
        if (len(vertical_lines) < 3 or len(horizontal_lines) < 3 or
                not np.all(np.isfinite(vertical_lines)) or not np.all(np.isfinite(horizontal_lines))):
            raise GridDetectionError("检测到的水平线或垂直线不足")

        vertical_lines, intervalsX = interval(vertical_lines)
        horizontal_lines, intervalsY = interval(horizontal_lines)

        if len(intervalsX) < 2 or len(intervalsY) < 2:
            raise GridDetectionError("合并重复线条后格网线不足")

        if self.interval0x == 0:
            xmin = min(intervalsX)
            ymin = min(intervalsY)

            small_intervalsX = [i for i in intervalsX if xmin <= i <= xmin + 4]
            small_intervalsY = [i for i in intervalsY if ymin <= i <= ymin + 4]
            small_x = float(np.mean(small_intervalsX))
            small_y = float(np.mean(small_intervalsY))

            large_intervalsX = [i for i in intervalsX if small_x * 4 <= i <= small_x * 8]
            large_intervalsY = [i for i in intervalsY if small_y * 4 <= i <= small_y * 8]
            if not large_intervalsX or not large_intervalsY:
                raise GridDetectionError("未识别到交替排列的大小格间距")
            self.interval0x, self.interval0y = small_x, small_y
            self.interval1x = np.median(large_intervalsX)
            self.interval1y = np.median(large_intervalsY)

            logger.debug("格距：小格 (%s, %s)，大格 (%s, %s)",
                         self.interval0x, self.interval0y, self.interval1x, self.interval1y)

        self.vertical_lines, self.vertical_start_small = self.supplement_lines(vertical_lines, self.width,
                                                                               self.interval0x, self.interval1x)
        self.horizontal_lines, self.horizontal_start_small = self.supplement_lines(horizontal_lines, self.height,
                                                                                   self.interval0y, self.interval1y)

        self.vertical_lines = [int(i) for i in self.vertical_lines if 0 <= i < self.width]
        self.horizontal_lines = [int(i) for i in self.horizontal_lines if 0 <= i < self.height]
        if len(self.vertical_lines) < 2 or len(self.horizontal_lines) < 2:
            raise GridDetectionError("可见区域中没有完整格网")

    def supplement_lines(self, original_lines, img_dim, interval0, interval1):
        start_point = original_lines[0]
        expected_start0, start0 = self.generate_expected_lines(start_point, img_dim, 0, interval0, interval1)
        expected_start1, start1 = self.generate_expected_lines(start_point, img_dim, 1, interval0, interval1)

        mse_small_start = self.calculate_mse(original_lines, expected_start0)
        mse_large_start = self.calculate_mse(original_lines, expected_start1)

        if mse_small_start < mse_large_start:
            return expected_start0, start0
        else:
            return expected_start1, start1

    def generate_expected_lines(self, start_point, img_dim, start_index, small_interval, large_interval):
        if not all(np.isfinite(value) and value > 0 for value in (small_interval, large_interval)):
            raise GridDetectionError("格网间距必须为正数且有限")
        intervals = [small_interval, large_interval]
        lines = [start_point]

        # 往后生成线条，直到超出图像尺寸
        flag = 1 - start_index
        while lines[-1] < img_dim + small_interval:
            next_line = lines[-1] + intervals[flag]
            if next_line < img_dim + small_interval:
                lines.append(next_line)
            else:
                break
            flag = 1 - flag

        # 在开始位置向前生成线条，直到达到0
        curr_start = start_point
        while curr_start >= - small_interval:
            curr_start -= intervals[start_index]
            if curr_start >= - small_interval:
                lines.insert(0, curr_start)
            else:
                break
            start_index = 1 - start_index

        negative_count = len([i for i in lines if i < 0])
        if negative_count % 2 == 1:
            start_index = 1 - start_index

        return lines, start_index

    def calculate_mse(self, original, expected):
        mse_values = []
        for value in original:
            closest_val = min(expected, key=lambda x: abs(x - value))
            mse_values.append((value - closest_val) ** 2)
        return np.mean(mse_values)

