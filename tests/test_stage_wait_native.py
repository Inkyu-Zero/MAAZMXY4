"""Exercise Maa's polling and recognition-only references without device input."""
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('MAA_RESULT_UI_TESTS') == '1', 'opt-in memory-only stage wait')
class StageWaitNativeTests(unittest.TestCase):
    def test_polling_recovery_modal_and_timeout_never_execute_target_action(self):
        import numpy as np
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        sys.path.insert(0, str(ROOT / 'agent'))
        from navigation.stage_view import wait_stage_ready
        from navigation.runtime import Navigator

        empty = np.zeros((720, 1147, 3), dtype=np.uint8)
        ready = empty.copy()
        ready[20:70, 20:70] = 255
        modal = read_png_bgr(ROOT / 'tests/fixtures/navigation/common-dialog/confirm-290-1.png')
        completed, errors = [], []

        class Pixels(CustomController):
            frames = [empty, ready]
            captures = 0

            def connect(self): return True
            def request_uuid(self): return 'stage-wait-memory-only'
            def screencap(self):
                frame = self.frames[min(self.captures, len(self.frames) - 1)]
                self.captures += 1
                return frame.copy()
            def forbidden(self, *args):
                errors.append('Device input was attempted')
                raise AssertionError(errors[-1])
            click = swipe = touch_down = touch_up = touch_move = key_down = key_up = input_text = start_app = stop_app = forbidden

        controller = Pixels()

        class Inspect(CustomAction):
            def run(self, context, argv):
                for label, frames, seconds, expected in (
                    ('delayed icon', [empty, empty, ready], 5, True),
                    ('modal interrupts wait', [modal], 5, True),
                    ('missing icon', [empty], .35, False),
                ):
                    controller.frames, controller.captures = frames, 0
                    deadline = time.monotonic() + seconds
                    navigator = Navigator(context,{},capture=controller.screencap,check=lambda:None,log=lambda _:None)
                    navigator.deadline=deadline
                    navigator._check=lambda:None  # This case isolates the wait's own remaining budget.
                    actual = wait_stage_ready(context, navigator, '__StageWaitTarget')
                    if actual != expected: errors.append((label, expected, actual))
                    if navigator.deadline != deadline: errors.append('Wait reset the caller budget')
                    if label == 'delayed icon' and controller.captures < 3:
                        errors.append('A stale frame satisfied the delayed target')
                    completed.append(label)
                return True

        log = Path(os.environ.get('MAA_RESULT_TEST_LOG', str(ROOT / '.runtime-cache/stage-wait-native')))
        log.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log)
        resource, tasker = Resource(), Tasker()
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        from battle_ui import NoRevivePrompt
        resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
        resource.register_custom_action('__inspect_stage_wait', Inspect())
        resource.override_pipeline({
            '__StageWaitTest': dict(action=dict(type='Custom', param=dict(custom_action='__inspect_stage_wait'))),
            '__StageWaitTarget': dict(recognition=dict(type='ColorMatch', param=dict(
                roi=[20, 20, 50, 50], lower=[200, 200, 200], upper=[255, 255, 255], count=2000)),
                action=dict(type='Click', param=dict(target=True))),
        })
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__StageWaitTest').wait().succeeded)
        self.assertEqual(len(completed), 3)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
