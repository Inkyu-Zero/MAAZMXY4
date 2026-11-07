"""Input ownership boundaries; no OS input, screenshots or game access."""
from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'agent'))
import background_mouse_guard as guard


class GuardTests(unittest.TestCase):
    def test_memory_controller_and_non_air_window_do_not_load_native_module(self):
        with patch.object(guard, '_native') as native, patch.object(guard, '_air_window', return_value=False):
            for controller in (None, SimpleNamespace(info={}), SimpleNamespace(info={'hwnd': 42})):
                with guard.hold_background_mouse(controller): pass
            native.assert_not_called()

    def test_exception_and_cancellation_release_the_same_lease(self):
        lib = SimpleNamespace(MaaMouseGuardBegin=Mock(return_value=123), MaaMouseGuardEnd=Mock(return_value=True))
        with patch.object(guard, '_native', return_value=lib), patch.object(guard, '_air_window', return_value=True):
            for error in (ValueError('input failed'), KeyboardInterrupt('cancelled')):
                with self.assertRaises(type(error)):
                    with guard.hold_background_mouse(hwnd=42): raise error
                self.assertEqual(lib.MaaMouseGuardEnd.call_args.args[0], 123)
        self.assertEqual(lib.MaaMouseGuardEnd.call_count, 2)

    def test_failure_to_start_never_sends_input(self):
        lib = SimpleNamespace(MaaMouseGuardBegin=Mock(return_value=None), MaaMouseGuardEnd=Mock())
        sent = []
        with patch.object(guard, '_native', return_value=lib), patch.object(guard, '_air_window', return_value=True), \
             patch.object(guard.ctypes, 'get_last_error', return_value=170, create=True):
            with self.assertRaisesRegex(RuntimeError, '未发送输入'):
                with guard.hold_background_mouse(hwnd=42): sent.append(True)
        self.assertFalse(sent)
        lib.MaaMouseGuardEnd.assert_not_called()

    def test_missing_module_and_closed_window_fail_before_input(self):
        sent = []
        for which in ('_air_window', '_native'):
            with patch.object(guard, '_air_window', return_value=True), \
                 patch.object(guard, which, side_effect=RuntimeError('unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                    with guard.hold_background_mouse(hwnd=42): sent.append(True)
        self.assertFalse(sent)

    def test_failed_cleanup_is_not_published_as_success(self):
        lib = SimpleNamespace(MaaMouseGuardBegin=Mock(return_value=123), MaaMouseGuardEnd=Mock(return_value=False))
        with patch.object(guard, '_native', return_value=lib), patch.object(guard, '_air_window', return_value=True), \
             patch.object(guard.ctypes, 'get_last_error', return_value=5, create=True):
            with self.assertRaisesRegex(RuntimeError, '清理未确认'):
                with guard.hold_background_mouse(hwnd=42): pass


if __name__ == '__main__': unittest.main()
