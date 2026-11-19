"""通过矩形四角、暗色直边和金色外框联合定位迷宫视区。"""

from dataclasses import dataclass

import cv2
import numpy as np

ASPECT_RATIO = 620 / 344


@dataclass(frozen=True)
class ViewportMatch:
    box: tuple[int, int, int, int]
    confidence: float
    gold_coverage: tuple[float, ...]
    corner_support: tuple[float, ...]


def _outside_strips(mask, box, width):
    x, y, right, bottom = box
    mx, my = round((right - x) * 0.1), round((bottom - y) * 0.1)
    return (
        mask[y - width - 1:y - 1, x + mx:right - mx],
        mask[bottom + 1:bottom + width + 1, x + mx:right - mx],
        mask[y + my:bottom - my, x - width - 1:x - 1],
        mask[y + my:bottom - my, right + 1:right + width + 1],
    )


def _corner_support(gray, corners, scale):
    radius = max(4, round(6 * scale))
    support = []
    for x, y in corners:
        left, top = max(0, x - radius), max(0, y - radius)
        patch = gray[top:y + radius + 1, left:x + radius + 1]
        points = cv2.goodFeaturesToTrack(patch, maxCorners=12, qualityLevel=0.03,
                                        minDistance=2, blockSize=3)
        if points is None:
            support.append(0.0)
            continue
        points = points.reshape(-1, 2) + (left, top)
        distance = np.linalg.norm(points - (x, y), axis=1).min()
        support.append(max(0.0, 1 - float(distance) / radius))
    return tuple(support)


def _iou(a, b):
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    return intersection / ((a[2] - a[0]) * (a[3] - a[1]) +
                           (b[2] - b[0]) * (b[3] - b[1]) - intersection)


def locate_viewport(image):
    """返回右下边界不包含在内的视区；没有可靠或唯一候选时抛出 ValueError。

    不依赖 WASD 素材或精确像素值，要求完整的四角与外框可见。
    """
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise ValueError("窗口定位需要 uint8 BGR 图片")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, (8, 45, 100), (35, 220, 255)) != 0
    candidates = []
    # 多档亮度阈值兼容深灰边线和抗锯齿，不要求边框为某个精确 RGB。
    for level in (20, 35, 50, 65):
        dark = (gray < level).astype(np.uint8) * 255
        contours, _ = cv2.findContours(dark, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w < 180 or h < 100:
                continue
            aspect_error = abs(w / h / ASPECT_RATIO - 1)
            if aspect_error > 0.02:
                continue
            rectangle_fill = cv2.contourArea(contour) / (w * h)
            if rectangle_fill < 0.94:
                continue
            polygon = cv2.approxPolyDP(contour, 0.012 * cv2.arcLength(contour, True), True)
            if len(polygon) != 4 or not cv2.isContourConvex(polygon):
                continue
            corners = np.array(((x, y), (x + w - 1, y),
                                (x + w - 1, y + h - 1), (x, y + h - 1)))
            distances = np.linalg.norm(polygon.reshape(4, 1, 2) - corners[None], axis=2)
            if np.max(np.min(distances, axis=1)) > max(3, min(w, h) * 0.015):
                continue
            scale = (w / 620 + h / 344) / 2
            band = max(2, round(5 * scale))
            box = (x, y, x + w, y + h)
            if (x < band + 1 or y < band + 1 or
                    x + w + band + 1 > image.shape[1] or y + h + band + 1 > image.shape[0]):
                continue
            coverage = tuple(float(patch.mean()) for patch in _outside_strips(gold, box, band))
            if min(coverage) < 0.6 or np.mean(coverage) < 0.75:
                continue
            # 四条直边需要连续暗色支持，不能仅凭矩形轮廓面积接受候选。
            depth = max(2, round(2 * scale))
            edges = (np.any(dark[y:y + depth, x:x + w] != 0, axis=0),
                     np.any(dark[y + h - depth:y + h, x:x + w] != 0, axis=0),
                     np.any(dark[y:y + h, x:x + depth] != 0, axis=1),
                     np.any(dark[y:y + h, x + w - depth:x + w] != 0, axis=1))
            dark_support = tuple(float(np.mean(edge)) for edge in edges)
            if min(dark_support) < 0.8:
                continue
            corners_support = _corner_support(gray, corners, scale)
            if min(corners_support) < 0.35:
                continue
            confidence = (0.35 * (1 - aspect_error / 0.02) + 0.25 * np.mean(coverage)
                          + 0.15 * rectangle_fill + 0.15 * np.mean(dark_support)
                          + 0.1 * np.mean(corners_support))
            if confidence < 0.8:
                continue
            candidates.append(ViewportMatch(box, float(confidence), coverage, corners_support))
    if not candidates:
        raise ValueError("未找到同时满足四角、暗色直边和金色外框的迷宫视区")
    candidates.sort(key=lambda match: match.confidence, reverse=True)
    best = candidates[0]
    # 同一矩形在多档阈值下的结果算一个候选；不同区域相近得分则拒绝猜测。
    if any(_iou(best.box, other.box) < 0.8 and best.confidence - other.confidence < 0.04
           for other in candidates[1:]):
        raise ValueError("检测到多个相似的迷宫边框，无法唯一定位")
    return best
