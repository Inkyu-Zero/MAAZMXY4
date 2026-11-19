"""已知黑色 Alpha 遮罩的反算，以及识别使用的可信像素区域。"""

from dataclasses import dataclass
from importlib.resources import files

import cv2
import numpy as np

@dataclass
class RestoredFrame:
    image: np.ndarray
    valid_pixels: np.ndarray


class MaskRestorer:
    def __init__(self, width, height, *, alpha=None, min_transmission=0.03, recognition_transmission=0.15):
        if not 0 < min_transmission <= recognition_transmission <= 1:
            raise ValueError("透光率阈值必须满足 0 < 显示阈值 <= 识别阈值 <= 1")
        if width <= 0 or height <= 0:
            raise ValueError("视区尺寸必须为正数")
        if alpha is None:
            resource = files("zmxy4_treasure_hunt.maze").joinpath("assets", "edge_mask.png")
            mask = cv2.imdecode(np.frombuffer(resource.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if mask is None or mask.ndim != 3 or mask.shape[2] != 4:
                raise ValueError(f"无法读取 RGBA 遮罩素材：{resource}")
            alpha = mask[..., 3]
        alpha = np.asarray(alpha)
        if alpha.ndim != 2 or alpha.dtype != np.uint8:
            raise ValueError("alpha 必须为二维 uint8 数组")
        # 缓存缩放、透光率和增益，逐帧仅执行乘法与截断。
        alpha = cv2.resize(alpha.astype(np.float32), (width, height), interpolation=cv2.INTER_LINEAR)
        self.transmission = 1 - alpha / 255
        self.usable = self.transmission >= min_transmission
        self.valid_pixels = self.transmission >= recognition_transmission
        self.valid_pixels.flags.writeable = False
        self.gain = np.divide(1, self.transmission, out=np.ones_like(self.transmission), where=self.usable)

    def restore(self, image):
        if image.dtype != np.uint8 or image.shape != (*self.transmission.shape, 3):
            raise ValueError("BGR 截图尺寸或格式与遮罩不一致")
        restored = np.clip(np.rint(image.astype(np.float32) * self.gain[..., None]), 0, 255).astype(np.uint8)
        # 极暗/完全不透光处没有足够信息，保留输入，不能凭空补出地图。
        restored[~self.usable] = image[~self.usable]
        return RestoredFrame(restored, self.valid_pixels)
