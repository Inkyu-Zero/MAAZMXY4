"""Explicit, observation-only input capture for the script studio.

Hooks only decode and enqueue input. A single owner thread checks the target,
updates the timeline and releases the listeners. Synthetic releases below are
JSON events: this module never sends input or activates a window.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import math
from queue import Empty, SimpleQueue
import threading
import time


CONTROL_KEYS = {0x77, 0x78, 0x79, 0x7B}  # F8, F9, F10, F12
MAX_ACTIONS = 100_000
MAX_DURATION_MS = 7 * 24 * 3600 * 1000


def key_vk(key):
    """pynput KeyCode and special Key enums expose VK in different places."""
    for value in (getattr(key, "vk", None),
                  getattr(getattr(key, "value", None), "vk", None)):
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 255:
            return value
    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1:
        if 1 <= ord(char) <= 26:  # Ctrl+A ... Ctrl+Z
            return ord(char) + 64
        if char.isascii() and char.isalpha():
            return ord(char.upper())
        if char.isascii() and char.isdigit():
            return ord(char)
        return {" ": 32, ";": 186, "=": 187, ",": 188, "-": 189,
                ".": 190, "/": 191, "`": 192, "[": 219, "\\": 220,
                "]": 221, "'": 222}.get(char)
    return None


def _base_size(width, height):
    if width > height:
        return [round(720 * width / height), 720]
    return [720, round(720 * height / width)]


class _Win32WindowAPI:
    """Loaded only by an explicit CaptureSession.start()."""

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetAncestor": ([wintypes.HWND, ctypes.c_uint], wintypes.HWND),
            "GetForegroundWindow": ([], wintypes.HWND),
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.user32, name)
            method.argtypes, method.restype = args, result

    @contextmanager
    def _physical_pixels(self):
        # pynput reports physical screen pixels. Match that coordinate space
        # locally without changing the hosting process's global DPI policy.
        previous = self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise RuntimeError("无法读取目标窗口的物理像素坐标，录制未开始。")
        try:
            yield
        finally:
            self.user32.SetThreadDpiAwarenessContext(previous)

    def inspect(self, hwnd):
        with self._physical_pixels():
            return self._inspect_physical(hwnd)

    def _inspect_physical(self, hwnd):
        api = self.user32
        root = api.GetAncestor(hwnd, 2) or hwnd
        if not api.IsWindow(hwnd) or api.IsIconic(root):
            raise RuntimeError("目标窗口已关闭或最小化，录制已停止。")
        rect, point, pid = wintypes.RECT(), wintypes.POINT(), wintypes.DWORD()
        name = ctypes.create_unicode_buffer(256)
        if not api.GetClientRect(hwnd, ctypes.byref(rect)) or not api.ClientToScreen(hwnd, ctypes.byref(point)):
            raise RuntimeError("无法读取目标客户区，录制已停止。")
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or not api.GetClassNameW(hwnd, name, len(name)):
            raise RuntimeError("无法确认目标窗口身份，录制已停止。")
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise RuntimeError("目标客户区尺寸无效，录制已停止。")
        return dict(hwnd=hwnd, pid=int(pid.value), class_name=name.value,
                    width=width, height=height, x=point.x, y=point.y)

    def is_foreground(self, hwnd):
        api = self.user32
        foreground = api.GetForegroundWindow()
        return bool(foreground) and (api.GetAncestor(foreground, 2) or foreground) == (api.GetAncestor(hwnd, 2) or hwnd)


def _default_listeners(callbacks):
    from pynput import keyboard, mouse

    # Returning False here only filters pynput delivery; suppress is disabled.
    keys = keyboard.Listener(on_press=callbacks["on_press"], on_release=callbacks["on_release"],
                             suppress=False,
                             win32_event_filter=lambda _msg, data: not bool(data.flags & 0x12))
    pointer = mouse.Listener(on_click=callbacks["on_click"], on_move=callbacks["on_move"],
                             on_scroll=callbacks["on_scroll"], suppress=False,
                             win32_event_filter=lambda _msg, data: not bool(data.flags & 3))
    return [keys, pointer]


class CaptureSession:
    """One capture at a time; on_change receives an inert snapshot dictionary.

    Optional adapters support isolated tests. Constructing an instance installs
    no hooks and loads no native modules. All timeline mutations are serialized
    on the worker, including UI commands and hotkeys.
    """

    def __init__(self, on_change=None, *, window_api=None, listener_factory=None, clock=None, lease_factory=None):
        self._on_change = on_change
        self._api = window_api
        self._lease_factory = lease_factory
        self._lease = None
        self._listener_factory = listener_factory or _default_listeners
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._gate = threading.RLock()
        self._worker = None
        self._listeners = []
        self._queue = SimpleQueue()
        self._ready = threading.Event()
        self._closed = False
        self._ending = False
        self._state = "idle"
        self._hwnd = 0
        self._error = ""
        self._message = ""
        self._save_requested = False
        self._exit_requested = False
        self._actions = []
        self._markers = []
        self._name = time.strftime("录制-%m%d-%H%M%S")
        self._recorded_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._base = [1147, 720]
        self._identity = None
        self._started = None
        self._deadline = 0.0
        self._paused_at = 0.0
        self._paused_total = 0.0
        self._duration = 0
        self._last_time = 0
        self._keys = set()
        self._buttons = set()
        self._physical_keys = set()
        self._physical_buttons = set()
        self._control_down = set()
        self._position = (0, 0)
        self._last_move = None
        self._last_notify = 0.0

    def _elapsed(self, now=None):
        if self._started is None:
            return 0
        if self._state not in {"recording", "paused", "resume_countdown"}:
            return self._duration
        current = self._paused_at if self._state in {"paused", "resume_countdown"} else (self._clock() if now is None else now)
        return min(MAX_DURATION_MS, max(self._last_time, 0, round((current - self._started - self._paused_total) * 1000)))

    def snapshot(self):
        with self._lock:
            return dict(state=self._state, elapsed_ms=self._elapsed(), event_count=len(self._actions),
                        hwnd=self._hwnd, error=self._error, message=self._message,
                        save_requested=self._save_requested, exit_requested=self._exit_requested,
                        marker_count=len(self._markers), closed=self._closed,
                        countdown_seconds=max(0, math.ceil(self._deadline - self._clock())) if self._state in {"countdown", "resume_countdown"} else 0)

    def _notify(self, force=False):
        now = self._clock()
        if not force and now - self._last_notify < .1:
            return
        self._last_notify = now
        if self._on_change:
            try:
                self._on_change(self.snapshot())
            except Exception:
                pass  # A presentation callback cannot interrupt listener cleanup.

    def _callbacks(self, queue):
        def offer(kind, data):
            queue.put((kind, self._clock(), data, None))

        def key(value, pressed):
            vk = key_vk(value)
            if vk is not None:
                offer("key", (vk, pressed))

        def click(x, y, button, pressed, *_):
            name = getattr(button, "name", None) or str(button).split(".")[-1]
            if name in {"left", "right", "middle"}:
                offer("click", (int(x), int(y), name, bool(pressed)))

        return dict(on_press=lambda value, *_: key(value, True),
                    on_release=lambda value, *_: key(value, False),
                    on_click=click,
                    on_move=lambda x, y, *_: offer("move", (int(x), int(y))),
                    on_scroll=lambda x, y, dx, dy, *_: offer("scroll", (int(x), int(y), int(dx), int(dy))))

    def _listeners_alive(self):
        return any(listener.is_alive() for listener in self._listeners)

    def start(self, hwnd: int, countdown_seconds=3):
        if type(hwnd) is not int or hwnd <= 0:
            raise ValueError("请选择有效的游戏窗口。")
        if type(countdown_seconds) is not int or not 0 <= countdown_seconds <= 60:
            raise ValueError("录制倒计时必须是0～60秒的整数。")
        with self._gate:
            if self._closed:
                raise RuntimeError("录制会话已关闭。")
            if (self._worker and self._worker.is_alive()) or self._listeners_alive():
                raise RuntimeError("请先停止录制并等待监听器清理完成。")
            if self._api is None:
                self._api = _Win32WindowAPI()
                from window_geometry import WindowLease
                self._lease_factory = self._lease_factory or (lambda handle: WindowLease(handle,adapt=False))
            if self._lease_factory:
                self._lease = self._lease_factory(hwnd)
            try:
                identity = self._api.inspect(hwnd)
                if any(type(identity.get(field)) is not int or identity[field] <= 0 for field in ("pid", "width", "height")):
                    raise ValueError("目标窗口身份或客户区尺寸无效。")
            except BaseException:
                if self._lease:
                    self._lease.close()
                    self._lease = None
                raise
            with self._lock:
                self._hwnd, self._identity = hwnd, identity
                self._base = _base_size(identity["width"], identity["height"])
                self._actions, self._markers = [], []
                self._name = time.strftime("录制-%m%d-%H%M%S")
                self._recorded_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._keys, self._buttons = set(), set()
                self._physical_keys, self._physical_buttons, self._control_down = set(), set(), set()
                self._started, self._duration, self._last_time = None, 0, 0
                self._paused_total, self._paused_at = 0.0, 0.0
                self._last_move, self._position = None, (0, 0)
                self._error, self._message = "", ""
                self._save_requested = self._exit_requested = self._ending = False
                self._state = "countdown"
                self._queue = SimpleQueue()
                self._ready = threading.Event()
            self._worker = threading.Thread(target=self._run, args=(countdown_seconds,), daemon=True,
                                            name="script-studio-capture")
            self._worker.start()
            if not self._ready.wait(3):
                self.stop()
                raise RuntimeError("输入监听器启动超时。")
            if self._error:
                self._worker.join(3)
                raise RuntimeError(self._error)
            return self.snapshot()

    def _command(self, kind, data=None):
        worker = self._worker
        if not worker or not worker.is_alive():
            return
        if worker is threading.current_thread():
            raise RuntimeError("不能从录制状态回调同步调用控制命令。")
        reply = {"done": threading.Event()}
        self._queue.put((kind, self._clock(), data, reply))
        deadline = time.monotonic() + 3
        while not reply["done"].wait(.025):
            if not worker.is_alive():
                return  # A terminal hotkey may have won the enqueue/teardown race.
            if time.monotonic() >= deadline:
                raise RuntimeError("录制器正在清理，请稍后重试。")
        if "error" in reply:
            raise reply["error"]

    def pause(self):
        self._command("pause")
        return self.snapshot()

    def resume(self, countdown_seconds=0):
        if type(countdown_seconds) is not int or not 0 <= countdown_seconds <= 60:
            raise ValueError('继续录制倒计时必须为0～60秒的整数。')
        self._command("resume", countdown_seconds)
        return self.snapshot()

    def mark(self, label):
        if not isinstance(label, str) or not label.strip() or len(label) > 256:
            raise ValueError("标记文字必须为1～256个字符。")
        self._command("mark", label.strip())
        return self.snapshot()

    def stop(self):
        with self._gate:
            if self._worker and self._worker.is_alive():
                self._command("stop")
                self._worker.join(3)
                if self._worker.is_alive():
                    raise RuntimeError("录制监听器尚未清理完成，请重试停止。")
            elif self._listeners_alive():
                self._cleanup_listeners()
            if self._listeners_alive():
                raise RuntimeError("录制监听器尚未退出，不能开始下一段录制。")
            with self._lock:
                if self._state == "stopping":
                    self._state = "failed" if self._error else "stopped"
                return self._recording()

    def close(self):
        self.stop()
        with self._lock:
            self._closed = True
            self._state = "closed"
        self._notify(True)

    def _recording(self):
        return dict(version=1, name=self._name, base=list(self._base),
                    recorded_at=self._recorded_at,
                    duration_ms=self._duration, actions=deepcopy(self._actions),
                    editor={"markers": deepcopy(self._markers)})

    def _append(self, timestamp, op, **data):
        timestamp = max(self._last_time, timestamp)
        self._actions.append(dict(t=timestamp - self._last_time, op=op, **data))
        self._last_time = timestamp

    def _marker(self, timestamp, label):
        self._markers.append(dict(at_ms=timestamp, label=label))

    def _release(self, timestamp):
        for vk in sorted(self._keys):
            self._append(timestamp, "keyup", key=vk)
        self._keys.clear()
        for button in sorted(self._buttons):
            self._append(timestamp, "up", x=self._position[0], y=self._position[1], button=button)
        self._buttons.clear()

    def _begin(self, now):
        self._started = now
        self._state = "recording"
        self._message = "正在录制；F8暂停，F9停止，F10停止并保存，F12退出。"
        if not self._api.is_foreground(self._hwnd):
            self._pause(now, "游戏不在前台，请切回游戏后手动继续。")
        self._notify(True)

    def _pause(self, now, message="录制已暂停，请切回游戏后手动继续。"):
        if self._state == 'resume_countdown':
            self._state, self._message = 'paused', '已取消继续倒计时，录制保持暂停。'
            self._notify(True)
            return
        if self._state != "recording":
            return
        timestamp = self._elapsed(now)
        self._release(timestamp)
        self._marker(timestamp, "暂停录制")
        self._paused_at, self._state, self._message = now, "paused", message
        self._notify(True)

    def _resume(self, now):
        if self._state not in {"paused", "resume_countdown"}:
            return
        self._check_target(now)
        if self._ending:
            return
        if not self._api.is_foreground(self._hwnd):
            raise RuntimeError("请先切回目标游戏窗口，再按F8继续。")
        timestamp = self._elapsed(now)
        self._paused_total += max(0, now - self._paused_at)
        self._state = "recording"
        self._last_move = None
        self._marker(timestamp, "继续录制")
        self._message = "正在录制。"
        self._notify(True)

    def _finish(self, now, error=""):
        if self._ending:
            return
        timestamp = self._elapsed(now)
        self._release(timestamp)
        self._duration = timestamp
        if self._started is not None:
            self._marker(timestamp, "录制停止")
        self._ending = True
        self._error = error
        self._state = "stopping"
        self._message = error or "录制已停止。"

    def _check_target(self, now):
        try:
            current = self._lease.ensure() if self._lease else self._api.inspect(self._hwnd)
            if any(current[key] != self._identity[key] for key in ("pid", "class_name", "width", "height")):
                raise RuntimeError("目标窗口身份或客户区尺寸已改变，录制已停止。")
            if self._state == "recording" and not self._api.is_foreground(self._hwnd):
                self._pause(now, "游戏失去焦点，录制已暂停；切回游戏后手动继续。")
            return current
        except Exception as exc:
            self._finish(now, str(exc))
            return None

    def _tick(self, now):
        if self._ending:
            return
        self._check_target(now)
        if not self._ending and self._state == "countdown" and now >= self._deadline:
            self._begin(self._deadline)
        elif not self._ending and self._state == 'resume_countdown' and now >= self._deadline:
            if self._api.is_foreground(self._hwnd):
                self._resume(now)
            else:
                self._state = 'paused'
                self._message = '目标窗口尚未回到前台，录制保持暂停；点击继续后有3秒切回游戏。'
                self._notify(True)
        if self._state == "recording" and self._elapsed(now) >= MAX_DURATION_MS:
            self._finish(now, "录制已达到7天时长上限。")
        for listener in self._listeners:
            if not listener.is_alive() and not self._ending:
                self._finish(now, "输入监听器意外退出，录制已停止。")
        self._notify()

    def _input(self, kind, at, data):
        if kind == "key":
            vk, pressed = data
            if vk in CONTROL_KEYS:
                if not pressed:
                    self._control_down.discard(vk)
                    return
                if vk in self._control_down:
                    return
                self._control_down.add(vk)
                if vk == 0x77:
                    if self._state in {"paused", "resume_countdown"}:
                        try:
                            self._resume(at)
                        except RuntimeError as exc:
                            self._message = str(exc)
                            self._notify(True)
                    else:
                        self._pause(at)
                elif vk == 0x78 and self._state == "countdown":
                    self._begin(at)
                else:
                    self._save_requested = vk == 0x79
                    self._exit_requested = vk == 0x7B
                    self._finish(at)
                return
            repeated = vk in self._physical_keys
            self._physical_keys.add(vk) if pressed else self._physical_keys.discard(vk)
            if self._state != "recording" or self._started is None or at < self._started:
                return
            if pressed and not repeated:
                self._keys.add(vk)
                self._append(self._elapsed(at), "keydown", key=vk)
            elif not pressed and vk in self._keys:
                self._keys.remove(vk)
                self._append(self._elapsed(at), "keyup", key=vk)
            return
        if kind == "click":
            x, y, button, pressed = data
            repeated = button in self._physical_buttons
            self._physical_buttons.add(button) if pressed else self._physical_buttons.discard(button)
        else:
            x, y = data[:2]
        if self._state != "recording" or self._started is None or at < self._started:
            return
        current = self._api.inspect(self._hwnd)
        cx, cy = x - current["x"], y - current["y"]
        if not (0 <= cx < current["width"] and 0 <= cy < current["height"]):
            if self._buttons:
                self._pause(at, "鼠标拖出客户区，录制已暂停；切回游戏后手动继续。")
            return
        gx = min(self._base[0] - 1, round(cx * self._base[0] / current["width"]))
        gy = min(self._base[1] - 1, round(cy * self._base[1] / current["height"]))
        self._position = gx, gy
        timestamp = self._elapsed(at)
        if kind == "click":
            if pressed and not repeated:
                self._buttons.add(button)
                self._append(timestamp, "down", x=gx, y=gy, button=button)
            elif not pressed and button in self._buttons:
                self._buttons.remove(button)
                self._append(timestamp, "up", x=gx, y=gy, button=button)
        elif kind == "move" and self._buttons:
            if self._last_move is None or timestamp - self._last_move >= 20:
                self._append(timestamp, "move", x=gx, y=gy)
                self._last_move = timestamp
        elif kind == "scroll" and any(data[2:]):
            self._append(timestamp, "scroll", dx=data[2], dy=data[3])

    def _cleanup_listeners(self):
        for listener in self._listeners:
            try:
                listener.stop()
            except Exception as exc:
                self._error = self._error or f"停止输入监听器失败：{exc}"
        for listener in self._listeners:
            try:
                listener.join(timeout=1)
            except Exception as exc:
                self._error = self._error or f"输入监听器清理失败：{exc}"

    def _run(self, countdown):
        replies = []
        try:
            self._listeners = list(self._listener_factory(self._callbacks(self._queue)))
            for listener in self._listeners:
                listener.start()
            with self._lock:
                self._deadline = self._clock() + countdown
                self._tick(self._clock())
            self._ready.set()
            self._notify(True)
            while not self._ending:
                try:
                    kind, at, data, reply = self._queue.get(timeout=.025)
                except Empty:
                    with self._lock:
                        self._tick(self._clock())
                    continue
                try:
                    with self._lock:
                        if kind == "stop":
                            self._finish(at)
                        elif kind == "pause":
                            self._pause(at)
                        elif kind == "resume":
                            if data and self._state == 'paused':
                                self._deadline = self._clock() + data
                                self._state = 'resume_countdown'
                                self._message = f'将在{data}秒后继续，请切回目标游戏窗口。'
                                self._notify(True)
                            else:
                                self._resume(at)
                        elif kind == "mark":
                            if self._state not in {"recording", "paused"}:
                                raise RuntimeError("开始录制后才能添加标记。")
                            self._marker(self._elapsed(at), data)
                        elif kind == "barrier":
                            self._tick(self._clock())
                        elif kind != "barrier":
                            self._tick(self._clock())
                            if not self._ending:
                                self._input(kind, at, data)
                        if len(self._actions) + len(self._markers) + len(self._keys) + len(self._buttons) >= MAX_ACTIONS - 4:
                            self._finish(at, "录制已达到10万条事件上限。")
                        self._notify()
                except Exception as exc:
                    if reply is None:
                        with self._lock:
                            self._finish(self._clock(), str(exc))
                    else:
                        reply["error"] = exc
                finally:
                    if reply:
                        if self._ending:
                            replies.append(reply)
                        else:
                            reply["done"].set()
        except Exception as exc:
            with self._lock:
                self._finish(self._clock(), f"输入监听器启动失败：{exc}")
        finally:
            self._ready.set()
            self._cleanup_listeners()
            if self._lease:
                try:
                    self._lease.close()
                except Exception as error:
                    self._error = '窗口会话清理失败：' + str(error)
                finally:
                    self._lease = None
            with self._lock:
                self._state = "stopping" if self._listeners_alive() else "failed" if self._error else "stopped"
            # Wake commands queued concurrently with a terminal hotkey.
            while True:
                try:
                    _, _, _, reply = self._queue.get_nowait()
                except Empty:
                    break
                if reply:
                    replies.append(reply)
            for reply in replies:
                reply["done"].set()
            self._notify(True)
