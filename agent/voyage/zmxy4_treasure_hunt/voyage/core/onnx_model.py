import cv2
import numpy as np
import onnxruntime as ort
import ast


class DetectionResult:
    def __init__(self, conf, xyxyn):
        self.conf = conf  # 置信度
        self.xyxyn = xyxyn  # (x_min, y_min, x_max, y_max) 归一化坐标
        x1, y1, x2, y2 = xyxyn
        self.xywhn = [(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1]


class OnnxModel:
    def __init__(self, onnx_path):
        # 加载 ONNX 模型
        self.session = ort.InferenceSession(onnx_path)

        # 获取模型输入的名称和形状
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape

        metadata = self.session.get_modelmeta()
        names = metadata.custom_metadata_map["names"]
        self.class_names = ast.literal_eval(names)
        if isinstance(self.class_names, list):
            self.class_names = dict(enumerate(self.class_names))
        if metadata.custom_metadata_map.get("end2end", "False").lower() != "true":
            raise ValueError("请使用 nms=False 导出的 YOLO26 免 NMS ONNX 模型")

    def preprocess(self, RGB_image):
        # 复现数据集 Stretch，直接拉伸到模型输入（宽 800、高 512），无需补边。
        input_h, input_w = self.input_shape[2:4]
        image_resized = cv2.resize(RGB_image, (input_w, input_h), interpolation=cv2.INTER_LINEAR)

        # 转换为 NCHW 格式并归一化
        input_image = image_resized.transpose(2, 0, 1).astype(np.float32) / 255.0
        input_image = np.expand_dims(input_image, axis=0)  # 添加批次维度

        return input_image

    def forward(self, RGB_image, conf_threshold=0.4):
        """输入为 RGB；仅过滤置信度和无效框，不执行 NMS。"""
        # 预处理图像
        input_image = self.preprocess(RGB_image)

        # 获取模型的输出
        outputs = self.session.run(None, {self.input_name: input_image})
        detections = outputs[0][0]  # (count, 6): xyxy, confidence, class_id

        if detections.ndim != 2 or detections.shape[1] != 6:
            raise ValueError(f"免 NMS 模型应输出 (N, 6)，实际为 {detections.shape}")
        results = {name: [] for name in self.class_names.values()}
        height, width = self.input_shape[2:4]
        for x1, y1, x2, y2, score, class_id in detections:
            if score <= conf_threshold:
                continue
            x1, x2 = np.clip(np.array([x1, x2]) / width, 0, 1)
            y1, y2 = np.clip(np.array([y1, y2]) / height, 0, 1)
            if x2 <= x1 or y2 <= y1:
                continue
            results[self.class_names[int(class_id)]].append(DetectionResult(
                float(score), [x1, y1, x2, y2],
            ))
        return results

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)
