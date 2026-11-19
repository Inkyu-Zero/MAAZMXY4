"""Windows 输入封装，只对发生变化的方向键发送事件。"""


def _keys_of(char):
    """把 "SD" 这种斜向字符串拆成 ["S", "D"]。

    原来的 key_down 是 ord(char.upper())，传入两字符的斜向会直接报错，
    所以仓库的斜向移动从来没真的按下过两个键 —— 只能四向走。
    """
    return [c for c in str(char).upper() if c in "WASDJ"]


def key_down(char):
    import win32api

    for c in _keys_of(char):
        win32api.keybd_event(ord(c), 0, 0, 0)


def key_up(char):
    import win32api
    import win32con

    for c in _keys_of(char):
        win32api.keybd_event(ord(c), 0, win32con.KEYEVENTF_KEYUP, 0)


def click(x, y):
    import win32api
    import win32con

    win32api.SetCursorPos((x, y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)


class MovementController:
    def __init__(self):
        self.pressed = set()

    def apply(self, movements):
        desired = set(movements)
        if not desired <= set("WASD"):
            raise ValueError("移动按键只能包含 WASD")
        for char in sorted(self.pressed - desired):
            key_up(char)
            self.pressed.remove(char)
        for char in sorted(desired - self.pressed):
            # 先记录状态，以便输入系统抛错时仍能在 finally 中尝试释放。
            self.pressed.add(char)
            key_down(char)

    def stop(self):
        # 按键状态可能来自上次中断，统一释放四个方向。
        error = None
        for char in "WASD":
            try:
                key_up(char)
            except Exception as exc:
                error = error or exc
        self.pressed.clear()
        if error is not None:
            raise error

    @staticmethod
    def click(x, y):
        click(x, y)
