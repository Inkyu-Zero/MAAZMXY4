"""通过部分可见的深色圆环定位头像，不依赖头发、肤色或精确纯黑像素。"""

import cv2
import numpy as np


def _samples(image, x, y, radii, angles, interpolation=cv2.INTER_LINEAR):
    xx = (x + np.asarray(radii)[:, None] * np.cos(angles)[None]).astype(np.float32)
    yy = (y + np.asarray(radii)[:, None] * np.sin(angles)[None]).astype(np.float32)
    return cv2.remap(image, xx, yy, interpolation, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def _refine(circle, radii, angles, scale):
    x, y, radius = circle
    points = np.column_stack((radii * np.cos(angles), radii * np.sin(angles)))
    keep = np.ones(len(points), bool)
    for _ in range(3):
        selected = points[keep]
        if len(selected) < 12:
            return None
        design = np.column_stack((2 * selected, np.ones(len(selected))))
        ox, oy, constant = np.linalg.lstsq(design, np.sum(selected ** 2, axis=1), rcond=None)[0]
        squared_radius = constant + ox ** 2 + oy ** 2
        if squared_radius <= 0:
            return None
        fitted = float(np.sqrt(squared_radius))
        residual = abs(np.linalg.norm(points - (ox, oy), axis=1) - fitted)
        keep = residual <= max(0.8, float(np.median(residual)) * 2.5)
    if np.hypot(ox, oy) > max(2, 3 * scale) or abs(fitted - radius) > max(3, 4 * scale):
        return None
    return float(x + ox), float(y + oy), fitted


def _quality(gray, valid, circle, scale, gx, gy, refine=True):
    x, y, radius = (float(value) for value in circle)
    angles = np.linspace(0, 2 * np.pi, 144, endpoint=False)
    thickness = max(1.5, 1.8 * scale)
    radii = radius + np.linspace(-thickness, thickness, 7)
    ring = _samples(gray, x, y, radii, angles)
    dark = ring.min(axis=0)
    outer_radii = radius + np.array([3.5, 5.5, 7.5]) * max(0.6, scale)
    outer = _samples(gray, x, y, outer_radii, angles)
    bright = outer.max(axis=0)
    ring_valid = _samples(valid, x, y, radii, angles, cv2.INTER_NEAREST).min(axis=0) != 0
    outer_valid = _samples(valid, x, y, outer_radii, angles, cv2.INTER_NEAREST).min(axis=0) != 0
    support = (dark < 85) & (bright > 80) & (bright.astype(float) - dark > 35) & ring_valid & outer_valid
    coverage = float(support.mean())
    sectors = support.reshape(8, -1).mean(axis=1)
    # 接受至少约半圈的分散弧段，直线/小黑点不能提供足够圆周支持。
    # 初始候选允许偏心，后续圆拟合与严格圆弧校验再决定是否接受。
    if coverage < 0.3 or np.count_nonzero(sectors >= 0.35) < 2:
        return None
    # 真圆环的外沿梯度沿半径方向，且各角度的外沿半径接近。
    # 墙角和宝箱纹理即使凑出一些黑点，也不应通过这一圆弧一致性检查。
    edge_radii = radius + np.linspace(-4 * scale, 4 * scale, 25)
    vx = _samples(gx, x, y, edge_radii, angles)
    vy = _samples(gy, x, y, edge_radii, angles)
    magnitude = np.hypot(vx, vy)
    alignment = (vx * np.cos(angles) + vy * np.sin(angles)) / (magnitude + 1e-6)
    weights = magnitude * np.maximum(alignment, 0) ** 8
    selected = weights.argmax(axis=0)
    positions = edge_radii[selected]
    strong = support & (weights[selected, np.arange(len(angles))] > 50)
    if not strong.any():
        return None
    fitted_radius = np.median(positions[strong])
    coherent = strong & (abs(positions - fitted_radius) <= max(0.8, 0.8 * scale))
    score = float(coherent.mean())
    result = None
    if score >= 0.45 and np.count_nonzero(coherent.reshape(8, -1).mean(axis=1) >= 0.35) >= 3:
        result = score, x, y, radius
    if refine and 0.2 <= score < 0.7:
        fitted = _refine((x, y, radius), positions[strong], angles[strong], scale)
        if fitted is not None:
            improved = _quality(gray, valid, fitted, scale, gx, gy, refine=False)
            if improved is not None and (result is None or improved[0] > result[0]):
                result = improved
    return result


def find_character_ring(image, valid_pixels=None):
    """返回 (x, y, ring_radius, quality)，没有可信圆环时返回 None。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    valid = np.ones(gray.shape, np.uint8) if valid_pixels is None else valid_pixels.astype(np.uint8)
    scale = min(image.shape[1] / 620, image.shape[0] / 344)
    minimum = max(4, round(17 * scale))
    maximum = max(minimum + 2, round(32 * scale))
    distance = max(5, 7 * scale)
    dark = ((gray < 75) & (valid != 0)).astype(np.uint8) * 255
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1)
    candidates = []
    # 灰度梯度保留被角色覆盖后的圆弧；近黑掩膜补充弱边缘和压缩图像。
    for source in (blurred, cv2.GaussianBlur(dark, (3, 3), 0)):
        circles = cv2.HoughCircles(source, cv2.HOUGH_GRADIENT,
                                   dp=1, minDist=distance, param1=100, param2=max(6, min(12, round(8 * scale))),
                                   minRadius=minimum, maxRadius=maximum)
        if circles is not None:
            candidates.extend(circles[0][:100])
    # 半圈遮挡、小尺寸时霍夫投票可能不足；连通圆弧的包围圆提供补充候选。
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        _, _, width, height = cv2.boundingRect(contour)
        if max(width, height) > 2.8 * maximum or min(width, height) < minimum * 0.5:
            continue
        (x, y), radius = cv2.minEnclosingCircle(contour)
        if minimum <= radius <= maximum:
            candidates.append((x, y, radius))
    ranked = []
    for circle in candidates:
        x, y, radius = circle
        if not (radius < x < gray.shape[1] - radius and radius < y < gray.shape[0] - radius):
            continue
        if not valid[round(float(y)), round(float(x))]:
            continue
        result = _quality(gray, valid, circle, scale, gx, gy)
        if result is not None:
            ranked.append(result)
    if not ranked:
        return None
    score, x, y, radius = max(ranked)
    if any(score - other[0] < 0.06 and np.hypot(x - other[1], y - other[2]) > max(radius, other[3])
           for other in ranked):
        return None
    return x, y, radius, score
