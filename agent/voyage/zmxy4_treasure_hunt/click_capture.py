import win32api
import win32con
import win32gui
import tkinter as tk
import ctypes
from PIL import Image, ImageGrab, ImageTk


class ClickCapture:
    def __init__(self, message="请点击选中游戏窗口"):
        self.root = None
        self.click_position = None
        self.message = message
        self.region = None
        self.preview_region = None
        self.manual_mode = False
        self.drag_start = None

    def capture_click(self, event):
        if self.manual_mode:
            self.drag_start = self.selection_point(event)
            self.show_preview(None)
            return
        # 确认当前显示的范围，避免销毁遮罩后命中另一个窗口。
        if self.preview_region is not None:
            self.click_position = (event.x_root, event.y_root)
            self.region = self.preview_region
            self.root.quit()

    def toggle_selection_mode(self, event=None):
        self.manual_mode = not self.manual_mode
        self.drag_start = None
        self.show_preview(None)
        self.canvas.itemconfigure(
            self.hint, text="拖动鼠标框选游戏区域" if self.manual_mode else self.message)
        self.canvas.itemconfigure(
            self.help_text,
            text=("框选模式 · 拖动后松开确认 · F2 自动选窗 · Esc 取消"
                  if self.manual_mode else
                  "移动鼠标预览窗口 · 单击确认 · F2 框选 · Esc 取消"))

    def selection_point(self, event):
        return (
            max(self.origin[0], min(event.x_root, self.origin[0] + self.screenshot.width)),
            max(self.origin[1], min(event.y_root, self.origin[1] + self.screenshot.height)),
        )

    def drag_selection(self, event):
        if not self.manual_mode or self.drag_start is None:
            return
        x0, y0 = self.drag_start
        x1, y1 = self.selection_point(event)
        region = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.show_preview(region if x0 != x1 and y0 != y1 else None)

    def finish_selection(self, event):
        if not self.manual_mode or self.drag_start is None:
            return
        self.drag_selection(event)
        self.drag_start = None
        if self.preview_region is not None:
            self.click_position = (event.x_root, event.y_root)
            self.region = self.preview_region
            self.root.quit()

    def exit_capture(self, event):
        self.root.quit()

    def window_at(self, point):
        """按 Z 顺序寻找遮罩下面的顶层窗口。"""
        windows = []

        def collect(hwnd, _):
            if (hwnd != self.overlay_hwnd and win32gui.IsWindowVisible(hwnd)
                    and not win32gui.IsIconic(hwnd)):
                try:
                    rect = win32gui.GetWindowRect(hwnd)
                except win32gui.error:
                    return True  # 窗口可能刚好关闭。
                left, top, right, bottom = rect
                if left <= point[0] < right and top <= point[1] < bottom:
                    windows.append(rect)
            return True

        win32gui.EnumWindows(collect, None)
        return windows[0] if windows else None

    def update_preview(self):
        if not self.manual_mode:
            self.show_preview(self.window_at(win32gui.GetCursorPos()))
        self.preview_job = self.root.after(30, self.update_preview)

    def show_preview(self, region):
        if region != self.preview_region:
            self.preview_region = region
            self.canvas.delete("preview")
            if region is not None:
                left, top, right, bottom = region
                x1, y1 = left - self.origin[0], top - self.origin[1]
                x2, y2 = right - self.origin[0], bottom - self.origin[1]
                # 裁到虚拟桌面内；返回值仍保留窗口的完整屏幕坐标。
                crop = (max(0, x1), max(0, y1),
                        min(self.screenshot.width, x2), min(self.screenshot.height, y2))
                self.preview_image = ImageTk.PhotoImage(self.screenshot.crop(crop), master=self.root)
                self.canvas.create_image(crop[0], crop[1], anchor="nw",
                                         image=self.preview_image, tags="preview")
                self.canvas.create_rectangle(x1 + 2, y1 + 2, x2 - 2, y2 - 2,
                                             outline="#00c8ff", width=4, tags="preview")
            self.canvas.tag_raise("hint")

    def start_capture(self):
        self.click_position = None
        self.region = None
        self.preview_region = None
        self.preview_job = None
        self.manual_mode = False
        self.drag_start = None
        # Tk 和 Win32 使用同一套物理像素坐标，兼容多屏及不同缩放比例。
        set_dpi_context = ctypes.windll.user32.SetThreadDpiAwarenessContext
        set_dpi_context.argtypes = [ctypes.c_void_p]
        set_dpi_context.restype = ctypes.c_void_p
        previous_dpi_context = set_dpi_context(ctypes.c_void_p(-4))
        try:
            self.origin = (win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN),
                           win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN))
            # 先截图再创建遮罩，预览不会把自身识别为待选窗口。
            self.screenshot = ImageGrab.grab(all_screens=True)
            self.root = tk.Tk()
            self.root.overrideredirect(True)
            self.root.attributes('-topmost', True)
            self.root.geometry(f"{self.screenshot.width}x{self.screenshot.height}"
                               f"{self.origin[0]:+d}{self.origin[1]:+d}")
            self.canvas = tk.Canvas(self.root, highlightthickness=0, cursor="crosshair")
            self.canvas.pack(fill=tk.BOTH, expand=True)
            dimmed = Image.blend(self.screenshot.convert("RGB"),
                                Image.new("RGB", self.screenshot.size, "black"), 0.45)
            self.background_image = ImageTk.PhotoImage(dimmed, master=self.root)
            self.canvas.create_image(0, 0, anchor="nw", image=self.background_image)
            # 提示放在主屏幕上，即使副屏在左侧也能看到。
            screen_width = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            hint_x = screen_width // 2 - self.origin[0]
            hint_y = 40 - self.origin[1]
            hint_width = max(160, min(640, screen_width - 100))
            heading = self.canvas.create_text(
                hint_x, hint_y, anchor="n", text="选择游戏窗口",
                font=('Microsoft YaHei', 12, 'bold'), fill="#7dd3fc", tags="hint")
            heading_bottom = self.canvas.bbox(heading)[3]
            self.hint = self.canvas.create_text(
                hint_x, heading_bottom + 12, anchor="n", text=self.message,
                width=hint_width, justify="center", font=('Microsoft YaHei', 22, 'bold'),
                fill="#fde047", tags="hint")
            message_bottom = self.canvas.bbox(self.hint)[3]
            self.help_text = self.canvas.create_text(
                hint_x, message_bottom + 14, anchor="n",
                text="移动鼠标预览窗口 · 单击确认 · F2 框选 · Esc 取消",
                width=hint_width, justify="center", font=('Microsoft YaHei', 12),
                fill="white", tags="hint")
            left, top, right, bottom = self.canvas.bbox("hint")
            panel = self.canvas.create_rectangle(
                left - 24, top - 18, right + 24, bottom + 18,
                fill="#111827", outline="#38bdf8", width=3, tags="hint")
            self.canvas.tag_lower(panel, heading)
            self.root.bind("<Button-1>", self.capture_click)
            self.root.bind("<B1-Motion>", self.drag_selection)
            self.root.bind("<ButtonRelease-1>", self.finish_selection)
            self.root.bind("<F2>", self.toggle_selection_mode)
            self.root.bind("<Escape>", self.exit_capture)
            self.root.update_idletasks()
            self.overlay_hwnd = win32gui.GetAncestor(self.root.winfo_id(), win32con.GA_ROOT)
            # Tk 对负坐标的 geometry 解释为距右/下边缘，使用 Win32 明确定位。
            win32gui.SetWindowPos(self.overlay_hwnd, win32con.HWND_TOPMOST,
                                  *self.origin, self.screenshot.width, self.screenshot.height,
                                  win32con.SWP_SHOWWINDOW)
            self.root.focus_force()
            self.update_preview()
            self.root.mainloop()
        finally:
            if self.root is not None:
                if self.preview_job is not None:
                    self.root.after_cancel(self.preview_job)
                self.root.destroy()
                self.root = None
            if previous_dpi_context:
                set_dpi_context(previous_dpi_context)

        return self.region
