"""Settings recognition and click boxes on stored pixels only."""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('MAA_RESULT_UI_TESTS') == '1', 'opt-in memory-only settings recognition')
class SettingsNativeTests(unittest.TestCase):
    def test_panel_variants_close_boxes_and_independent_menu_anchors(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        sys.path.insert(0, str(ROOT / 'agent'))
        from battle_ui import NoRevivePrompt
        folder = ROOT / 'tests/fixtures/navigation'
        cases = json.loads((folder / 'settings/cases.json').read_text(encoding='utf8'))['cases']
        assets = json.loads((ROOT / 'agent/data/navigation/settings-ui.json').read_text(encoding='utf8'))['assets']
        titles = {Path(a['path']).stem.removesuffix('-title'): a['crop']
                  for a in assets if a['path'].endswith('-title.png')}
        menu = read_png_bgr(folder / 'settings-menu.png')
        errors, completed = [], []

        class Pixels(CustomController):
            def connect(self): return True
            def request_uuid(self): return 'settings-memory-only'
            def screencap(self): return menu.copy()
            def forbidden(self, *args): raise AssertionError('No device input allowed')
            click = swipe = touch_down = touch_up = touch_move = key_down = key_up = input_text = start_app = stop_app = forbidden

        class Inspect(CustomAction):
            def run(self, context, argv):
                for case in cases:
                    frame = read_png_bgr(folder / 'settings' / case['file'])
                    result = context.run_recognition('Nav_CloseSettingsSubpanel', frame)
                    if result is None or not result.hit:
                        errors.append((case['file'], 'close missed'))
                    elif not 780 <= result.box[0] + result.box[2] / 2 <= 845:
                        errors.append((case['file'], 'close box', result.box))
                    if context.run_recognition('Nav_SettingsReturn', frame).hit:
                        errors.append((case['file'], 'subpanel mistaken for return'))
                    # Keep the close button and content label visible, so this
                    # negative specifically checks the independent title guard.
                    x1,y1,x2,y2 = [round(v * 720 / 590) for v in titles[Path(case['file']).stem]]
                    frame[y1-2:y2+2, x1-2:x2+2] = 0
                    if context.run_recognition('Nav_CloseSettingsSubpanel', frame).hit:
                        errors.append((case['file'], 'titleless close permitted'))
                    completed.append(case['file'])
                for offset in (0, 20, 'small-window'):
                    shifted = menu.copy()
                    if offset == 'small-window':
                        shifted=read_png_bgr(folder/'regressions/settings-small-window.png')
                    elif offset:
                        shifted[110:670, 400:770] = 0
                        shifted[130:670, 400:770] = menu[110:650, 400:770]
                    for node in ('Nav_SettingsMenu', 'Nav_SettingsReturn', 'Nav_SettingsContinue'):
                        result = context.run_recognition(node, shifted)
                        if result is None or not result.hit: errors.append((node, offset, 'missed'))
                    completed.append('menu' + str(offset))
                for name in ('map-a', 'world', 'profile', 'challenge-failed','revival/normal-p1-up'):
                    frame = read_png_bgr(folder / (name + '.png'))
                    for node in ('Nav_SettingsSubpanel', 'Nav_SettingsReturn'):
                        if context.run_recognition(node, frame).hit: errors.append((name, node, 'false match'))
                return True

        log = Path(os.environ.get('MAA_RESULT_TEST_LOG', str(ROOT / '.runtime-cache/settings-native')))
        log.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log)
        resource, controller, tasker = Resource(), Pixels(), Tasker()
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        resource.register_custom_recognition('battle_no_revive', NoRevivePrompt())
        resource.register_custom_action('__inspect_settings', Inspect())
        resource.override_pipeline({'__SettingsTest': dict(action=dict(type='Custom', param=dict(custom_action='__inspect_settings')))})
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__SettingsTest').wait().succeeded)
        self.assertEqual(len(completed), 6)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
