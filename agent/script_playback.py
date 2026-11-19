"""Input-only jobs for the existing studio worker and host task queue.

One job plays one immutable recording. Repetition belongs to MFA's task queue.
No tasker/pipeline, capture, recognition, navigation or restart is involved.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import threading
import time

from window_geometry import WindowLease
from background_mouse_guard import hold_background_mouse


class CompletedInput:
    succeeded = True

    def wait(self):
        return self


class InputOnlyController:
    """Keep native keyboard/text; send mouse and wheel messages in verified pixels.

    Maa 5.12 calls screencap to initialize scaling on its first absolute mouse
    input, even with UseRawSize. These four mouse operations use the selected
    background message transport directly, without touching the system cursor.
    """
    def __init__(self, native, lease, mouse_method='SendMessage', should_stop=lambda: False):
        if mouse_method not in ('SendMessage', 'PostMessage'):
            raise ValueError('纯脚本播放请使用 SendMessage 或 PostMessage 后台鼠标输入。')
        self.native, self.lease, self.mouse_method = native, lease, mouse_method
        self.position, self.buttons = (lease.base[0] // 2, lease.base[1] // 2), set()
        self.should_stop = should_stop
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
            wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.api.SendMessageTimeoutW.restype = wintypes.LPARAM
        self.api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.PostMessageW.restype = wintypes.BOOL

    @property
    def info(self):
        return {'hwnd': self.lease.hwnd}

    def __getattr__(self, name):
        if name in ('post_click_key', 'post_key_down', 'post_key_up'):
            def send(*args, **kwargs):
                self.lease.require_identity()
                return getattr(self.native, name)(*args, **kwargs)
            return send
        raise AttributeError(name)

    def _send(self, message):
        # Cleanup may run after a size failure, but must still target only the
        # original live HWND. It never redirects input to another process.
        self.lease.require_identity()
        x, y = self.lease.map_point(*self.position)
        flags = sum({0: 1, 1: 2, 2: 16}[button] for button in self.buttons)
        point = ((y & 0xffff) << 16) | (x & 0xffff)
        return self._message(message, flags, point)

    def _message(self, message, flags, point):
        self.lease.require_identity()
        # Geometry and packed coordinates are physical pixels. Windows converts
        # mouse messages according to the sending thread's DPI context; leaving
        # this thread unaware enlarged every point at 125% display scaling.
        # Reuse the geometry scope and restore it even when dispatch fails.
        with self.lease.api.windows._physical_pixels():
            if self.mouse_method == 'PostMessage':
                ok = self.api.PostMessageW(self.lease.hwnd, message, flags, point)
            else:
                result = ctypes.c_size_t()
                ok = self.api.SendMessageTimeoutW(self.lease.hwnd, message, flags, point, 2, 500, ctypes.byref(result))
        if not ok:
            raise RuntimeError('后台鼠标输入失败或游戏窗口无响应。')
        return CompletedInput()

    def post_touch_down(self, x, y, contact=0):
        self._message(0x6, 1, 0)  # Same background WM_ACTIVATE as Maa, no focus API.
        time.sleep(.01)
        self.position = (x, y)
        self.buttons.add(contact)
        return self._send({0: 0x201, 1: 0x204, 2: 0x207}[contact])

    def post_touch_move(self, x, y, contact=0):
        self.position = (x, y)
        return self._send(0x200)

    def post_touch_up(self, contact=0):
        self.buttons.discard(contact)
        return self._send({0: 0x202, 1: 0x205, 2: 0x208}[contact])

    def post_click(self, x, y):
        self.post_touch_down(x, y)
        time.sleep(.05)  # Match Maa's atomic click duration.
        return self.post_touch_up()

    def clear_hover(self):
        self.lease.require_identity()
        self.position = tuple(round(20*base/size) for base,size in zip(self.lease.base,self.lease.client_size))
        self._message(0x200, 0, (20 << 16) | 20)

    def post_input_text(self, text):
        from recording_playback import _wait_job
        # Native text input waits 50 ms per character. Split long text so stop
        # is observed between characters rather than after an entire paragraph.
        for char in text:
            if self.should_stop():
                break
            self.lease.ensure()
            _wait_job(self.native.post_input_text(char))
        return CompletedInput()

    def post_scroll(self, dx, dy):
        # Wheel messages carry screen coordinates; preserve the preceding
        # script mouse position without consulting or moving the real cursor.
        current = self.lease.ensure()
        x, y = self.lease.map_point(*self.position)
        x, y = x + current['x'], y + current['y']
        point = ((y & 0xffff) << 16) | (x & 0xffff)
        for message, delta in ((0x20a, dy), (0x20e, dx)):
            if delta:
                self._message(message, (delta & 0xffff) << 16, point)
        return CompletedInput()


def initialize_input_runtime(root):
    native_dir = Path(root) / 'runtimes/win-x64/native'
    if native_dir.is_dir():
        os.environ.setdefault('MAAFW_BINARY_PATH', str(native_dir))
    # Initialize native/NumPy imports on the JSONL owner thread. On Windows,
    # first loading them while another thread blocks on stdin can stall CRT
    # initialization. No window/controller is created here.
    from maa.controller import Win32Controller
    from maa.define import LoggingLevelEnum
    from maa.tasker import Tasker
    Tasker.set_stdout_level(LoggingLevelEnum.Off)  # stdout belongs to JSONL IPC
    Tasker.set_log_dir(Path(root) / 'debug' / 'script-playback')


def native_controller(hwnd, keyboard_method, mouse_method):
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum
    if keyboard_method not in ('SendMessage', 'PostMessage') or mouse_method not in ('SendMessage', 'PostMessage'):
        raise ValueError('纯脚本播放请使用 SendMessage 或 PostMessage 后台键盘输入。')
    # None disables the native connection warm-up/speed test too.
    controller = Win32Controller(hwnd, screencap_method=0,
                                 mouse_method=getattr(MaaWin32InputMethodEnum, mouse_method),
                                 keyboard_method=getattr(MaaWin32InputMethodEnum, keyboard_method))
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError('无法建立纯输入窗口连接。')
    return controller


class PlaybackJob:
    def __init__(self, root, params, *, controller_factory=native_controller, lease_factory=WindowLease,
                 input_adapter_factory=InputOnlyController):
        self.root, self.params = Path(root), dict(params)
        if controller_factory is native_controller:
            initialize_input_runtime(self.root)
        self.controller_factory, self.lease_factory = controller_factory, lease_factory
        self.input_adapter_factory = input_adapter_factory
        self.cancel = threading.Event()
        self.state, self.error = 'running', ''
        self.phase = 'validating'
        self._thread = threading.Thread(target=self._run, name='script-playback', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            native_dir = self.root / 'runtimes/win-x64/native'
            if native_dir.is_dir():
                os.environ.setdefault('MAAFW_BINARY_PATH', str(native_dir))
            from recording_playback import read_recording, play_actions
            from script_library import resolve_recording_reference
            path = resolve_recording_reference(self.root / 'recordings', self.params['file'])
            rec = read_recording(path, expected_sha256=self.params['sha256'], strict=True)
            from box_speed import validate_speed, validate_recording
            speed=validate_speed(self.params.get('speed',1))
            validate_recording(rec,speed)
            if speed==2:
                from box_speed_native import verify_playback_proof
                verify_playback_proof(self.params.get('box_speed_proof'),self.params['hwnd'],self.params['sha256'])
            if self.cancel.is_set():
                self.state = 'stopped'
                return
            self.phase = 'window'
            with self.lease_factory(self.params['hwnd'], rec['base']) as lease:
                self.phase = 'connecting'
                native = self.controller_factory(lease.hwnd, self.params.get('keyboard', 'SendMessage'),
                                                 self.params.get('mouse', 'SendMessage'))
                try:
                    controller = self.input_adapter_factory(native, lease, self.params.get('mouse', 'SendMessage'), self.cancel.is_set)
                    self.phase = 'playing'
                    with hold_background_mouse(controller):
                        succeeded = play_actions(rec, controller, self.cancel.is_set, ensure=lease.ensure,
                            speed=speed,
                            clock=lease.clock, cleanup_guard=lease.require_identity,
                            after_playback=getattr(controller, 'clear_hover', None),
                            log=lambda message: print(message, file=sys.stderr, flush=True))
                    self.state = 'stopped' if self.cancel.is_set() else 'succeeded' if succeeded else 'failed'
                finally:
                    # Controller owns its native keys; player already releases each
                    # held input even on exceptions before disposal.
                    del native
        except Exception as error:
            self.error, self.state = str(error), 'failed'

    def snapshot(self):
        # Completion is published only after the lease/inputs have been released.
        state = 'running' if self._thread.is_alive() else self.state
        return dict(state=state, error=self.error, phase=self.phase)

    def stop(self):
        self.cancel.set()
        self._thread.join(3)
        if self._thread.is_alive():
            raise RuntimeError('播放输入尚未清理完成，请等待后重试。')
        return self.snapshot()
