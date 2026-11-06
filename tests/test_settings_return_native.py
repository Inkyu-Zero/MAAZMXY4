"""Return navigation on stored pixels; no Win32 controller or physical input."""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS') == '1', 'opt-in memory-only return test')
class SettingsReturnNativeTests(unittest.TestCase):
    def test_all_battle_only_routes_accept_an_open_menu_and_a_shifted_return_map(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        sys.path.insert(0, str(ROOT / 'agent'))
        from battle_ui import NoRevivePrompt
        from task_catalog import custom_parameters
        from test_bounty_candle import load_runtime
        finish_round=load_runtime().finish_round
        from navigation.runtime import Navigator
        from navigation.catalog import task_routine

        menu = read_png_bgr(ROOT / 'tests/fixtures/navigation/settings-menu.png')
        battle = read_png_bgr(ROOT / 'tests/fixtures/bounty-candle/candle-battle-ready.png')
        returned = read_png_bgr(ROOT / 'tests/fixtures/navigation/task-map-real.png')
        task_icon = read_png_bgr(ROOT / 'assets/resource/base/image/任务.png')
        stage_cases = json.loads((ROOT / 'tests/fixtures/navigation/stage-states/cases.json').read_bytes())
        errors, checks = [], []

        class Pixels(CustomController):
            stage = 'battle'
            clicks = []
            def connect(self): return True
            def request_uuid(self): return 'return-route-memory-only'
            def screencap(self): return {'battle': battle, 'menu': menu, 'map': returned}[self.stage].copy()
            def touch_down(self, contact, x, y, pressure):
                self.clicks.append((self.stage, x, y))
                expected = (680 <= x <= 745 and 650 <= y <= 720) if self.stage == 'battle' else (450 <= x <= 700 and 220 <= y <= 280)
                if not expected: errors.append(('unexpected click', self.stage, x, y)); return False
                return True
            def touch_up(self, contact):
                if errors: return False
                self.stage = 'menu' if self.stage == 'battle' else 'map'
                return True
            def forbidden(self, *args): raise AssertionError('Unexpected input')
            click = swipe = touch_move = key_down = key_up = input_text = start_app = stop_app = forbidden

        controller = Pixels()
        class Terminal(CustomAction):
            def run(self, context, argv):
                phase = json.loads(argv.custom_action_param).get('phase') if isinstance(argv.custom_action_param, str) else argv.custom_action_param.get('phase')
                if phase != 'success' or controller.stage != 'map':
                    errors.append(('terminal', phase, controller.stage)); return False
                return True

        class Inspect(CustomAction):
            def run(self, context, argv):
                nonlocal returned
                for path in sorted((ROOT / 'assets/resource/base/pipeline').glob('*.json')):
                    graph = json.loads(path.read_bytes())
                    params = next((custom_parameters(n) for n in graph.values()
                                   if isinstance(n, dict) and custom_parameters(n).get('round_flow')), None)
                    if not params: continue
                    prefix = path.stem + '-'
                    shared=params.get('completion_node') is not None
                    if shared:graph.update(json.loads((ROOT/'assets/resource/base/pipeline/bounty_common.json').read_bytes()))
                    if params['navigation']['kind'] == 'stage':
                        if path.stem in ('伽岚（万世冥阁）', '无冥（孤魂岭）', '贪妄（金蟾宝宫）'):
                            town = 'town6' if path.stem.startswith('贪妄') else 'town5'
                            full = read_png_bgr(ROOT / 'tests/fixtures/game-management/scroll' / (town+'.png'))
                            # On re-entry the view can reset to the top/left;
                            # the stage icon need not still be inside the viewport.
                            returned = full[:720, :1147].copy()
                            returned[620:620+task_icon.shape[0], 800:800+task_icon.shape[1]] = task_icon
                        else:
                            case = next(c for c in stage_cases if c['task'] == path.stem)
                            from fixture_pixels import read_stage_state
                            returned = read_stage_state(ROOT / 'tests/fixtures/navigation/stage-states', case)
                    else:
                        returned = read_png_bgr(ROOT / 'tests/fixtures/navigation/task-map-real.png')
                    for initial in ('battle', 'menu', 'map'):
                        controller.stage, controller.clicks = initial, []
                        def capture():
                            controller.post_screencap().wait()
                            return controller.cached_image
                        state=dict(parameters={**params,'exit_mode':'battle_only'})
                        nav=Navigator(context,task_routine(context,params),capture=capture,check=lambda:None)
                        if not finish_round(context,state,nav) or errors:
                            errors.append((path.stem, initial, 'route failed')); return True
                        expected_count = {'battle': 2, 'menu': 1, 'map': 0}[initial]
                        if len(controller.clicks) != expected_count:
                            errors.append((path.stem, initial, controller.clicks)); return True
                        checks.append((path.stem, initial))
                return True

        log = ROOT / '.runtime-cache/native-settings-routes'; log.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log)
        resource, tasker = Resource(), Tasker()
        resource.register_custom_recognition('battle_no_revive', NoRevivePrompt())
        resource.register_custom_action('__return_terminal', Terminal())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__return_routes', Inspect())
        resource.override_pipeline({'__ReturnRoutes': dict(action=dict(type='Custom', param=dict(custom_action='__return_routes')))})
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__ReturnRoutes').wait().succeeded)
        self.assertEqual(errors, [])
        self.assertEqual(len(checks), 33)

    def test_real_menu_preserves_return_and_continue_click_boxes(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        sys.path.insert(0, str(ROOT / 'agent'))
        from battle_ui import NoRevivePrompt

        menu = read_png_bgr(ROOT / 'tests/fixtures/navigation/settings-menu.png')
        checks, errors = [], []

        class Pixels(CustomController):
            def connect(self): return True
            def request_uuid(self): return 'settings-return-memory-only'
            def screencap(self): return menu.copy()
            def forbidden(self, *args): raise AssertionError('No input in recognition probe')
            click = swipe = touch_down = touch_up = touch_move = key_down = key_up = input_text = start_app = stop_app = forbidden

        class Inspect(CustomAction):
            def run(self, context, argv):
                for name, expected_y in [('Nav_SettingsMenu', 180), ('Nav_SettingsContinue', 180), ('Nav_SettingsReturn', 250)]:
                    result = context.run_recognition(name, menu)
                    if result is None or not result.hit:
                        errors.append(name + ' missed the real menu')
                        continue
                    x, y, w, h = result.box
                    if not (450 < x + w / 2 < 700 and abs(y + h / 2 - expected_y) < 30):
                        errors.append((name, list(result.box)))
                    checks.append(name)
                for name in ('Nav_SettingsMenu', 'Nav_SettingsReturn'):
                    # A single return label cannot authorize a menu click.
                    partial = menu.copy(); partial[130:215, 400:770] = 0
                    result = context.run_recognition(name, partial)
                    if result is None or result.hit: errors.append(name + ' accepted an incomplete menu')
                return True

        log = ROOT / '.runtime-cache/native-settings-return'; log.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log)
        controller, resource, tasker = Pixels(), Resource(), Tasker()
        resource.register_custom_recognition('battle_no_revive', NoRevivePrompt())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__settings_return_probe', Inspect())
        resource.override_pipeline({'__SettingsReturnProbe': dict(action=dict(type='Custom', param=dict(custom_action='__settings_return_probe')))})
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__SettingsReturnProbe').wait().succeeded)
        self.assertEqual(errors, [])
        self.assertEqual(len(checks), 3)


if __name__ == '__main__': unittest.main()
