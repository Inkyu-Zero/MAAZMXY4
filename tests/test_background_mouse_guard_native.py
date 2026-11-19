"""Opt-in cross-process Win32 hook tests, using only hidden owned fixtures.

Build with tools/build_mouse_guard.ps1 -OutputDirectory <external> -BuildTests.
Set MAA_MOUSE_GUARD_TEST_DLL to that output's MaaBackgroundMouseGuard.dll,
and MAA_MOUSE_GUARD_TEST_HOST to tests/mouse_guard_host.exe underneath it.
"""
import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def library():
    lib = ctypes.WinDLL(os.environ['MAA_MOUSE_GUARD_TEST_DLL'], use_last_error=True)
    lib.MaaMouseGuardBegin.argtypes = [W.HWND]
    lib.MaaMouseGuardBegin.restype = ctypes.c_void_p
    lib.MaaMouseGuardEnd.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.DWORD)]
    lib.MaaMouseGuardEnd.restype = W.BOOL
    return lib


@unittest.skipUnless(sys.platform == 'win32' and os.environ.get('MAA_MOUSE_GUARD_TEST_HOST')
                     and os.environ.get('MAA_MOUSE_GUARD_TEST_DLL'), 'isolated native fixture not configured')
class NativeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ready = Path(self.tmp.name)/'ready.json'
        host = Path(os.environ['MAA_MOUSE_GUARD_TEST_HOST'])
        self.process = subprocess.Popen([str(host), str(host.with_name('Adobe AIR.dll')), str(ready)],
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self.close_host)
        deadline = time.monotonic()+5
        while not ready.exists() and self.process.poll() is None and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertIsNone(self.process.poll())
        self.windows = json.loads(ready.read_text())
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.api.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM,
                                                W.UINT, W.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.api.SendMessageTimeoutW.restype = W.LPARAM
        self.lib = library()

    def close_host(self):
        if self.process.poll() is None:
            if hasattr(self, 'windows'): self.api.PostMessageW(self.windows[0], 0x8002, 0, 0)
            try: self.process.wait(3)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(3)

    def send(self, hwnd, message, value=0):
        result = ctypes.c_size_t()
        self.assertTrue(self.api.SendMessageTimeoutW(hwnd, message, value, 0, 2, 1000, ctypes.byref(result)))
        return result.value

    def post(self, hwnd, message):
        self.assertTrue(self.api.PostMessageW(hwnd, message, 0, 0))

    def drain(self, hwnd):
        expected = self.send(hwnd, 0x8003, 5)+1
        self.post(hwnd, 0x8001)
        deadline = time.monotonic()+2
        while self.send(hwnd, 0x8003, 5)<expected and time.monotonic()<deadline: time.sleep(.005)
        self.assertEqual(self.send(hwnd, 0x8003, 5), expected)

    def begin(self):
        lease = self.lib.MaaMouseGuardBegin(self.windows[0])
        self.assertTrue(lease, ctypes.get_last_error())
        return lease

    def end(self, lease):
        count = W.DWORD()
        self.assertTrue(self.lib.MaaMouseGuardEnd(lease, ctypes.byref(count)), ctypes.get_last_error())
        return count.value

    def test_only_target_queued_leave_is_filtered_and_restored(self):
        target, other, _ = self.windows
        self.post(target, 0x2a3); self.drain(target)
        self.assertEqual(self.send(target, 0x8003, 0), 1)  # old behavior
        lease = self.begin()
        try:
            self.post(target, 0x2a3); self.post(other, 0x2a3)
            for message in (0x201, 0x202, 0x200, 0x100): self.post(target, message)
            self.drain(target)
            self.assertEqual(self.send(target, 0x8003, 0), 1)
            self.assertEqual(self.send(other, 0x8003, 0), 1)
            for bucket in range(1,5): self.assertEqual(self.send(target, 0x8003, bucket), 1)
            # A sent leave isn't the queued Windows tracking notification.
            self.send(target, 0x2a3)
            self.assertEqual(self.send(target, 0x8003, 0), 2)
        finally:
            self.assertEqual(self.end(lease), 1)
        self.drain(target)
        self.assertEqual(self.send(target, 0x8003, 0), 3)  # reconcile physical outside
        self.post(target, 0x2a3); self.drain(target)
        self.assertEqual(self.send(target, 0x8003, 0), 4)

    def test_busy_owner_and_recursive_acquisition_are_rejected(self):
        lease = self.begin()
        try:
            self.assertFalse(self.lib.MaaMouseGuardBegin(self.windows[0]))
            self.assertEqual(ctypes.get_last_error(), 170)
            child = subprocess.run([sys.executable, '-B', __file__, 'try-owner', str(self.windows[0])],
                                   capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {'started':False, 'error':170})
        finally: self.end(lease)
        self.end(self.begin())

    def test_end_drains_posted_input_before_removing_filter(self):
        target = self.windows[0]
        lease = self.begin()
        for _ in range(100):
            self.post(target, 0x2a3)
            self.post(target, 0x202)
        self.assertEqual(self.end(lease), 100)
        self.assertEqual(self.send(target, 0x8003, 2), 100)
        self.drain(target)
        self.assertEqual(self.send(target, 0x8003, 0), 1)

    def test_owner_crash_does_not_leave_filter_and_new_owner_can_acquire(self):
        child = subprocess.run([sys.executable, '-B', __file__, 'try-owner', str(self.windows[0])],
                               capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertTrue(json.loads(child.stdout)['started'])  # child exits without End
        self.post(self.windows[0], 0x2a3); self.drain(self.windows[0])
        self.assertEqual(self.send(self.windows[0], 0x8003, 0), 1)
        self.end(self.begin())

    def test_busy_target_after_release_can_acknowledge_without_false_failure(self):
        target=self.windows[0]
        self.send(target,0x8004,1800)
        lease=self.begin()
        self.post(target,0x2a3);self.post(target,0x202)
        started=time.monotonic()
        self.assertEqual(self.end(lease),1)
        self.assertGreater(time.monotonic()-started,1.5)
        self.assertEqual(self.send(target,0x8003,2),1)
        self.end(self.begin())  # Cleanup released the ownership lock.

    def test_persistent_busy_target_fails_boundedly_and_releases_ownership(self):
        target=self.windows[0]
        self.send(target,0x8004,6500)
        lease=self.begin();self.post(target,0x202)
        started=time.monotonic();count=W.DWORD()
        self.assertFalse(self.lib.MaaMouseGuardEnd(lease,ctypes.byref(count)))
        self.assertEqual(ctypes.get_last_error(),1460)
        elapsed=time.monotonic()-started
        self.assertGreaterEqual(elapsed,4.8);self.assertLess(elapsed,6)
        time.sleep(max(0,6.6-elapsed))
        self.end(self.begin())

    def test_closed_target_can_be_cleaned_and_non_air_is_rejected(self):
        self.assertFalse(self.lib.MaaMouseGuardBegin(self.windows[2]))
        self.assertEqual(ctypes.get_last_error(), 1400)
        lease = self.begin()
        self.close_host()
        self.end(lease)


if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='try-owner':
        lib=library(); lease=lib.MaaMouseGuardBegin(int(sys.argv[2]))
        print(json.dumps(dict(started=bool(lease), error=0 if lease else ctypes.get_last_error())), flush=True)
        os._exit(0)
    unittest.main()
