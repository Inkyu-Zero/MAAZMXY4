"""Isolated recorder tests. No Windows API, hooks or game input are used."""

from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from script_studio.capture import CaptureSession, key_vk


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeWindows:
    def __init__(self):
        self.foreground = True
        self.closed = False
        self.info = dict(hwnd=42, pid=17, class_name="Game", width=1200, height=750, x=100, y=50)

    def inspect(self, hwnd):
        if self.closed:
            raise RuntimeError("目标窗口已关闭。")
        if hwnd != 42:
            raise RuntimeError("错误窗口")
        return dict(self.info)

    def is_foreground(self, hwnd):
        return self.foreground


class FakeListener:
    def __init__(self, *, fail=False, sticky=False):
        self.alive = False
        self.fail = fail
        self.sticky = sticky
        self.stopped = False
        self.joined = False

    def start(self):
        if self.fail:
            raise RuntimeError("fake hook failed")
        self.alive = True

    def stop(self):
        self.stopped = True
        if not self.sticky:
            self.alive = False

    def join(self, timeout=None):
        self.joined = True

    def is_alive(self):
        return self.alive


class FakeHooks:
    def __init__(self):
        self.calls = 0
        self.callbacks = {}
        self.listeners = []
        self.thread_ids = []

    def __call__(self, callbacks):
        self.calls += 1
        self.callbacks = callbacks
        self.listeners = [FakeListener(), FakeListener()]
        return self.listeners

    def key(self, vk, pressed=True):
        self.callbacks["on_press" if pressed else "on_release"](SimpleNamespace(vk=vk))

    def mouse(self, x, y, button="left", pressed=True):
        self.callbacks["on_click"](x, y, SimpleNamespace(name=button), pressed)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.windows = FakeWindows()
        self.hooks = FakeHooks()
        self.updates = []
        self.session = CaptureSession(self.updates.append, window_api=self.windows,
                                      listener_factory=self.hooks, clock=self.clock)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for listener in self.hooks.listeners:
            listener.sticky = False
        self.session.close()

    def flush(self):
        self.session._command("barrier")

    def begin(self):
        self.session.start(42, 0)
        self.assertEqual(self.session.snapshot()["state"], "recording")

    def test_ui_resume_has_three_seconds_to_return_focus_and_freezes_elapsed(self):
        self.begin()
        self.clock.advance(1)
        self.windows.foreground = False
        self.flush()
        before = self.session.snapshot()['elapsed_ms']
        self.session.resume(countdown_seconds=3)
        self.assertEqual(self.session.snapshot()['state'], 'resume_countdown')
        self.assertEqual(self.session.snapshot()['countdown_seconds'], 3)
        self.clock.advance(2)
        self.flush()
        self.assertEqual(self.session.snapshot()['countdown_seconds'], 1)
        self.assertEqual(self.session.snapshot()['elapsed_ms'], before)
        self.windows.foreground = True
        self.clock.advance(1)
        self.flush()
        self.assertEqual(self.session.snapshot()['state'], 'recording')
        self.assertEqual(self.session.snapshot()['elapsed_ms'], before)
        self.clock.advance(.4)
        self.flush()
        self.assertAlmostEqual(self.session.snapshot()['elapsed_ms'], before + 400, delta=1)

    def test_resume_countdown_without_focus_stays_paused_and_can_retry(self):
        self.begin()
        self.windows.foreground = False
        self.flush()
        self.session.resume(countdown_seconds=3)
        self.clock.advance(3)
        self.flush()
        self.assertEqual(self.session.snapshot()['state'], 'paused')
        self.assertIn('前台', self.session.snapshot()['message'])
        self.session.resume(countdown_seconds=3)
        self.session.pause()
        self.assertEqual(self.session.snapshot()['state'], 'paused')
        self.clock.advance(4)
        self.windows.foreground = True
        self.flush()
        self.assertEqual(self.session.snapshot()['state'], 'paused')

    def test_stop_during_resume_countdown_keeps_only_recorded_time(self):
        self.begin()
        self.clock.advance(1)
        self.session.pause()
        self.session.resume(countdown_seconds=3)
        self.clock.advance(2)
        data = self.session.stop()
        self.assertEqual(data['duration_ms'], 1000)

    def test_construction_is_inert_and_explicit_start_owns_hooks(self):
        self.assertEqual(self.hooks.calls, 0)
        self.assertEqual(self.session.snapshot()["state"], "idle")
        self.session.start(42, 3)
        self.assertEqual(self.hooks.calls, 1)
        self.assertEqual(self.session.snapshot()["state"], "countdown")
        self.session.stop()
        self.assertTrue(all(listener.stopped and listener.joined for listener in self.hooks.listeners))

    def test_countdown_events_are_excluded_and_first_delay_is_preserved(self):
        self.session.start(42, 3)
        self.clock.advance(1)
        self.hooks.key(65)
        self.hooks.key(65, False)
        self.flush()
        self.clock.advance(2.25)
        self.hooks.key(66)
        self.flush()
        data = self.session.stop()
        self.assertEqual(data["actions"], [dict(t=250, op="keydown", key=66), dict(t=0, op="keyup", key=66)])
        self.assertEqual(data["duration_ms"], 250)
        self.assertEqual(data["base"], [1152, 720])

    def test_special_keys_repeat_and_orphan_releases_are_handled(self):
        self.begin()
        self.clock.advance(.1)
        self.hooks.callbacks["on_press"](SimpleNamespace(value=SimpleNamespace(vk=37)))
        self.hooks.key(37)
        self.hooks.key(65, False)
        self.clock.advance(.2)
        self.hooks.key(37, False)
        self.flush()
        data = self.session.stop()
        self.assertEqual(data["actions"], [dict(t=100, op="keydown", key=37), dict(t=200, op="keyup", key=37)])

    def test_pause_releases_keys_and_all_buttons_and_excludes_pause_time(self):
        self.begin()
        self.hooks.key(65)
        self.hooks.mouse(700, 425, "right")
        self.flush()
        self.clock.advance(1)
        self.session.pause()
        self.assertEqual(self.session.snapshot()["elapsed_ms"], 1000)
        self.clock.advance(5)
        self.assertEqual(self.session.snapshot()["elapsed_ms"], 1000)
        self.hooks.key(65, False)
        self.hooks.mouse(700, 425, "right", False)
        self.flush()
        self.session.resume()
        self.clock.advance(.5)
        self.hooks.key(66)
        self.flush()
        data = self.session.stop()
        self.assertEqual(data["duration_ms"], 1500)
        self.assertEqual([a["op"] for a in data["actions"]], ["keydown", "down", "keyup", "up", "keydown", "keyup"])
        self.assertEqual(sum(a["t"] for a in data["actions"]), 1500)
        self.assertEqual(data["actions"][1]["button"], "right")

    def test_focus_loss_pauses_without_automatic_resume_or_repeat_capture(self):
        self.begin()
        self.hooks.key(65)
        self.flush()
        self.clock.advance(1)
        self.windows.foreground = False
        self.flush()
        self.assertEqual(self.session.snapshot()["state"], "paused")
        self.windows.foreground = True
        self.clock.advance(3)
        self.flush()
        self.assertEqual(self.session.snapshot()["state"], "paused")
        self.session.resume()
        self.hooks.key(65)  # Still physically held: OS autorepeat must not re-arm it.
        self.hooks.key(65, False)
        self.hooks.key(65)
        self.flush()
        data = self.session.stop()
        self.assertEqual([a["op"] for a in data["actions"]], ["keydown", "keyup", "keydown", "keyup"])

    def test_mouse_drag_is_scaled_hover_ignored_and_scroll_preserved(self):
        self.begin()
        self.hooks.callbacks["on_move"](200, 200)
        self.hooks.mouse(700, 425, "middle")
        self.clock.advance(.05)
        self.hooks.callbacks["on_move"](800, 450)
        self.hooks.callbacks["on_scroll"](800, 450, -2, 3)
        self.flush()
        data = self.session.stop()
        self.assertEqual([a["op"] for a in data["actions"]], ["down", "move", "scroll", "up"])
        self.assertEqual(data["actions"][0], dict(t=0, op="down", x=576, y=360, button="middle"))
        self.assertEqual(data["actions"][1]["x"], 672)
        self.assertEqual(data["actions"][2], dict(t=0, op="scroll", dx=-2, dy=3))
        self.assertEqual(data["actions"][-1]["button"], "middle")

    def test_drag_outside_client_pauses_and_records_balanced_release(self):
        self.begin()
        self.hooks.mouse(700, 425)
        self.flush()
        self.clock.advance(.25)
        self.hooks.callbacks["on_move"](20, 20)
        self.flush()
        self.assertEqual(self.session.snapshot()["state"], "paused")
        data = self.session.stop()
        self.assertEqual(data["actions"][-1], dict(t=250, op="up", x=576, y=360, button="left"))

    def test_markers_use_absolute_active_time_without_changing_action_gaps(self):
        self.begin()
        self.clock.advance(.4)
        self.session.mark("第二阶段")
        self.clock.advance(.6)
        self.hooks.key(65)
        self.flush()
        data = self.session.stop()
        self.assertEqual(data["editor"]["markers"][0], dict(at_ms=400, label="第二阶段"))
        self.assertEqual(data["actions"][0]["t"], 1000)
        with self.assertRaises(ValueError):
            self.session.mark("")

    def test_hotkeys_are_filtered_and_f10_requests_save_after_cleanup(self):
        self.begin()
        self.hooks.key(65)
        self.hooks.key(0x79)
        self.flush()
        data = self.session.stop()
        self.assertTrue(self.session.snapshot()["save_requested"])
        self.assertEqual([a.get("key") for a in data["actions"]], [65, 65])
        self.assertTrue(all(listener.joined for listener in self.hooks.listeners))

    def test_f9_starts_armed_countdown_then_stops_and_f12_requests_exit(self):
        self.session.start(42, 3)
        self.hooks.key(0x78)
        self.hooks.key(0x78)  # Held F9 is one command.
        self.flush()
        self.assertEqual(self.session.snapshot()["state"], "recording")
        self.hooks.key(0x78, False)
        self.hooks.key(0x78)
        self.flush()
        self.session.stop()
        self.assertEqual(self.session.snapshot()["state"], "stopped")
        self.session.start(42, 0)
        self.hooks.key(0x7B)
        self.flush()
        self.session.stop()
        self.assertTrue(self.session.snapshot()["exit_requested"])

    def test_f8_cannot_resume_in_other_window_and_does_not_end_recording(self):
        self.begin()
        self.session.pause()
        self.windows.foreground = False
        self.hooks.key(0x77)
        self.flush()
        self.assertEqual(self.session.snapshot()["state"], "paused")
        self.assertFalse(self.session.snapshot()["error"])
        with self.assertRaisesRegex(RuntimeError, "切回"):
            self.session.resume()

    def test_window_identity_size_and_closure_stop_without_retargeting(self):
        for field, value in (("pid", 18), ("class_name", "Other"), ("width", 1210)):
            with self.subTest(field=field):
                self.windows.info.update(pid=17, class_name="Game", width=1200)
                self.begin()
                self.hooks.key(65)
                self.flush()
                self.windows.info[field] = value
                self.flush()
                data = self.session.stop()
                self.assertEqual(self.session.snapshot()["state"], "failed")
                self.assertEqual(data["actions"][-1]["op"], "keyup")
        self.windows.info.update(pid=17, class_name="Game", width=1200)
        self.begin()
        self.windows.closed = True
        self.flush()
        self.session.stop()
        self.assertIn("关闭", self.session.snapshot()["error"])

    def test_stale_callbacks_cannot_write_to_a_new_session(self):
        self.begin()
        old = self.hooks.callbacks
        self.session.stop()
        self.begin()
        old["on_press"](SimpleNamespace(vk=65))
        self.flush()
        self.assertEqual(self.session.stop()["actions"], [])

    def test_start_is_blocked_until_listeners_really_exit(self):
        self.begin()
        self.hooks.listeners[0].sticky = True
        with self.assertRaisesRegex(RuntimeError, "监听器"):
            self.session.stop()
        with self.assertRaisesRegex(RuntimeError, "清理"):
            self.session.start(42, 0)
        self.hooks.listeners[0].sticky = False
        self.session.stop()
        self.begin()

    def test_worker_start_failure_cleans_up_partial_listeners(self):
        listeners = [FakeListener(), FakeListener(fail=True)]
        session = CaptureSession(window_api=self.windows, listener_factory=lambda _: listeners, clock=self.clock)
        with self.assertRaisesRegex(RuntimeError, "fake hook failed"):
            session.start(42, 0)
        self.assertTrue(all(listener.stopped and listener.joined for listener in listeners))
        session.close()

    def test_callbacks_do_not_query_windows_or_mutate_timeline(self):
        self.begin()
        # Hold the owner lock to prevent the dispatcher running while callbacks
        # enqueue. The producer must return without touching Win32 or this lock.
        finished = threading.Event()
        with self.session._lock:
            before = len(self.session._actions)
            producer = threading.Thread(target=lambda: (self.hooks.key(65), finished.set()))
            producer.start()
            self.assertTrue(finished.wait(1))
            self.assertEqual(len(self.session._actions), before)
        producer.join(1)
        self.flush()
        self.assertEqual(self.session.snapshot()["event_count"], 1)

    def test_vk_decoder_accepts_special_char_and_ctrl_keys(self):
        self.assertEqual(key_vk(SimpleNamespace(value=SimpleNamespace(vk=13))), 13)
        self.assertEqual(key_vk(SimpleNamespace(char="a")), 65)
        self.assertEqual(key_vk(SimpleNamespace(char="\x03")), 67)
        self.assertIsNone(key_vk(SimpleNamespace(vk=999)))

    def test_recording_is_idempotent_and_accepted_by_migrated_editor(self):
        from script_studio.model import RecordingDocument

        self.begin()
        self.hooks.key(37)
        self.hooks.mouse(700, 425, "right")
        self.clock.advance(.1)
        self.hooks.callbacks["on_move"](800, 450)
        self.flush()
        self.session.mark("转场")
        raw = self.session.stop()
        self.assertEqual(raw, self.session.stop())
        document = RecordingDocument.from_dict(raw)
        self.assertEqual(document.diagnostics, [])
        self.assertEqual(document.to_dict()["actions"], raw["actions"])

    def test_listener_crash_preserves_data_and_synthesizes_releases(self):
        self.begin()
        self.hooks.key(65)
        self.flush()
        self.hooks.listeners[0].alive = False
        self.flush()
        raw = self.session.stop()
        self.assertEqual(self.session.snapshot()["state"], "failed")
        self.assertEqual([a["op"] for a in raw["actions"]], ["keydown", "keyup"])

    def test_dpi_scope_restores_the_original_thread_context_after_failure(self):
        from script_studio.capture import _Win32WindowAPI

        calls = []
        api = object.__new__(_Win32WindowAPI)
        api.user32 = SimpleNamespace(SetThreadDpiAwarenessContext=lambda value: (calls.append(value), 17)[1])
        with self.assertRaisesRegex(RuntimeError, "example"):
            with api._physical_pixels():
                raise RuntimeError("example")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], 17)


class CaptureWindowLockTests(unittest.TestCase):
    def test_recording_preserves_window_and_records_its_actual_aspect(self):
        from test_window_geometry import Geometry
        from window_geometry import WindowLease, SIZING_STYLE
        api=Geometry();api.is_foreground=lambda hwnd: True
        hooks=FakeHooks()
        session=CaptureSession(listener_factory=hooks)
        self.addCleanup(session.close)
        def ownership_only(hwnd,**kwargs):
            self.assertEqual(kwargs,dict(adapt=False))
            return WindowLease(hwnd,api=api,**kwargs)
        with patch('script_studio.capture._Win32WindowAPI',return_value=api), \
                patch('window_geometry.WindowLease',side_effect=ownership_only):
            session.start(42,0)
        self.assertEqual(session._recording()['base'],[1152,720])
        self.assertTrue(api.bits&SIZING_STYLE)
        self.assertFalse(api.changes)
        with self.assertRaisesRegex(RuntimeError,'busy'):
            WindowLease(42,api=api)
        api.info['width']=1000
        session._command('barrier')
        self.assertEqual(api.info['width'],1000)
        self.assertIn('尺寸已改变',session.snapshot()['error'])
        session.stop()
        self.assertTrue(api.bits&SIZING_STYLE);self.assertFalse(api.claimed)
        session.start(42,0);session.stop()
        self.assertFalse(api.claimed)

    def test_hook_failure_does_not_change_window(self):
        from test_window_geometry import Geometry
        from window_geometry import WindowLease, SIZING_STYLE
        api=Geometry();api.is_foreground=lambda hwnd: True
        session=CaptureSession(window_api=api,listener_factory=lambda callbacks:[FakeListener(fail=True)],
            lease_factory=lambda hwnd:WindowLease(hwnd,api=api,adapt=False))
        self.addCleanup(session.close)
        with self.assertRaises(RuntimeError):session.start(42,0)
        self.assertFalse(api.claimed);self.assertTrue(api.bits&SIZING_STYLE)
        self.assertFalse(api.changes)


if __name__ == "__main__":
    unittest.main()
