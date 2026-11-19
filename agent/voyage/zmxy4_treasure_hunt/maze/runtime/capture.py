"""DXcam BGR 截图及资源生命周期。导入模块不会初始化显示设备。"""

import math
import time


class ScreenCapture:
    def __init__(self, camera=None, timeout=5):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须为正数且有限")
        if camera is None:
            import dxcam

            try:
                camera = dxcam.create(output_color="BGR")
            except Exception as exc:
                print(f"DXGI 截图初始化失败：{exc}，尝试 WinRT。")
                camera = dxcam.create(output_color="BGR", backend="winrt")
        self.camera = camera
        self.timeout = timeout
        self.closed = False

    def grab(self, region=None):
        """region 使用显示器内的 (left, top, right, bottom) 坐标。"""
        if self.closed:
            raise RuntimeError("截图设备已关闭")
        deadline = time.monotonic() + self.timeout
        while True:
            frame = self.camera.grab(region=region, new_frame_only=False)
            if frame is not None:
                return frame
            if time.monotonic() >= deadline:
                raise TimeoutError(f"DXcam 在 {self.timeout:g} 秒内未能获取屏幕画面")
            time.sleep(0.01)

    def close(self):
        if not self.closed:
            self.closed = True
            self.camera.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
