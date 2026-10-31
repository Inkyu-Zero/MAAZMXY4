"""Real rendering regression: identify the dialog and click its cancel region."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/navigation/revival-live'


class RevivalAnchorAssetsTests(unittest.TestCase):
    def test_public_dialog_fixtures_keep_provenance_and_remove_inventory(self):
        data = json.loads((FIXTURES / 'cases.json').read_text(encoding='utf8'))
        for case in data['cases']:
            self.assertEqual(len(case['source_sha256']), 64)
            self.assertTrue(case['redactions'])
            self.assertEqual(hashlib.sha256((FIXTURES / case['file']).read_bytes()).hexdigest(), case['sha256'])

    def test_all_tasks_wait_through_game_countdown_and_keep_confirmed_failure(self):
        found = 0
        for path in (ROOT / 'assets/resource/base/pipeline').glob('*.json'):
            nodes = json.loads(path.read_text(encoding='utf8'))
            for name, node in nodes.items():
                if not name.endswith('-等待拒绝复活'): continue
                found += 1
                prefix = name.removesuffix('等待拒绝复活')
                self.assertEqual(node['next'], [prefix+'拒绝还魂丹', prefix+'战斗失败'])
                self.assertGreaterEqual(node['timeout'], 15000)
                self.assertEqual(node['on_error'], [prefix+'失败'])
                self.assertEqual(nodes[prefix+'拒绝还魂丹']['pre_delay'], 0)
                self.assertEqual(nodes[prefix+'拒绝还魂丹']['next'], [prefix+'战斗失败'])
        self.assertEqual(found, 11)


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS') == '1', 'opt-in memory-only revival regression')
class RevivalAnchorNativeTests(unittest.TestCase):
    def run_memory_case(self, inspect, first_frame):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        sys.path.insert(0,str(ROOT/'agent'))
        from battle_ui import NoRevivePrompt
        errors, completed, events = [], [], []

        class MemoryOnly(CustomController):
            frame = first_frame
            source = None
            def connect(self): return True
            def request_uuid(self): return 'revival-anchor-simulated-input'
            def screencap(self): return (self.source() if self.source else self.frame).copy()
            def touch_down(self, contact, x, y, pressure):
                events.append(('down', x, y)); return True
            def touch_up(self, contact):
                events.append(('up', contact)); return True
            def forbidden(self, *args): raise AssertionError('Unexpected controller operation')
            click = swipe = touch_move = key_down = key_up = input_text = start_app = stop_app = forbidden

        controller = MemoryOnly()
        class Inspect(CustomAction):
            def run(self, context, argv):
                try:
                    inspect(context, controller, events)
                    completed.append(True)
                except Exception as error:
                    errors.append(repr(error))
                return not errors

        log = Path(os.environ.get('MAA_REVIVAL_TEST_LOG', str(ROOT / '.runtime-cache/revival-anchor')))
        log.mkdir(parents=True, exist_ok=True); Tasker.set_log_dir(log)
        resource, tasker = Resource(), Tasker()
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        resource.register_custom_recognition('battle_no_revive', NoRevivePrompt())
        resource.register_custom_action('__inspect_revival_scenario', Inspect())
        resource.override_pipeline({'__RevivalScenario': dict(action=dict(type='Custom', param=dict(custom_action='__inspect_revival_scenario')))})
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__RevivalScenario').wait().succeeded, errors)
        self.assertEqual(errors, [])
        self.assertEqual(completed, [True])

    def test_actual_log_dialogs_produce_cancel_boxes(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        cases = json.loads((FIXTURES / 'cases.json').read_text(encoding='utf8'))['cases']
        frames = {c['file']: read_png_bgr(FIXTURES / c['file']) for c in cases}
        checked, errors = [], []

        class MemoryOnly(CustomController):
            def connect(self): return True
            def request_uuid(self): return 'revival-anchor-memory-only'
            def screencap(self): return next(iter(frames.values())).copy()
            def forbidden(self, *args): raise AssertionError('No device input permitted')
            click = swipe = touch_down = touch_up = touch_move = key_down = key_up = input_text = start_app = stop_app = forbidden

        class Inspect(CustomAction):
            def run(self, context, argv):
                for case in cases:
                    result = context.run_recognition('Result_DeclineRevive', frames[case['file']])
                    x,y,w,h = case['cancel_box']
                    if result is None or not result.hit:
                        errors.append((case['file'], 'cancel missed'))
                    elif not (x <= result.box[0] and y <= result.box[1]
                              and result.box[0]+result.box[2] <= x+w and result.box[1]+result.box[3] <= y+h):
                        errors.append((case['file'], 'outside cancel', result.box))
                    checked.append(case['file'])
                return True

        log = Path(os.environ.get('MAA_REVIVAL_TEST_LOG', str(ROOT / '.runtime-cache/revival-anchor')))
        log.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log)
        resource, controller, tasker = Resource(), MemoryOnly(), Tasker()
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__inspect_revival_anchor', Inspect())
        resource.override_pipeline({'__RevivalAnchorTest': dict(action=dict(type='Custom', param=dict(custom_action='__inspect_revival_anchor')))})
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__RevivalAnchorTest').wait().succeeded)
        self.assertEqual(len(checked), len(cases))
        self.assertEqual(errors, [])

    def test_captured_free_revive_reaches_decline_and_confirmed_defeat(self):
        from fixture_pixels import read_png_bgr
        from battle_outcomes import BattleOutcomeMonitor
        prompt = read_png_bgr(FIXTURES/'vip-p1-countdown10.png')
        failed = read_png_bgr(ROOT/'tests/fixtures/navigation/challenge-failed.png')
        prefix = '悬赏（守卫太宗）-'
        policy = dict(failed_next=[prefix+'已确认战斗失败'],
            succeeded_next=[prefix+'已确认战斗成功'], default_next=[prefix+'回放后分流'],
            revive_next=[prefix+'拒绝还魂丹', prefix+'等待拒绝复活', prefix+'战斗失败'])

        def inspect(context, controller, events):
            # The semantic fallback may identify a resurrection prompt, but
            # never authorizes a click without the complete button layout.
            obscured = prompt.copy()
            obscured[400:460,430:720] = 0
            self.assertTrue(context.run_recognition('Result_RevivePrompt', obscured).hit)
            self.assertFalse(context.run_recognition('Result_DeclineRevive', obscured).hit)
            pending = [name for path in (ROOT/'assets/resource/base/pipeline').glob('*.json')
                for name in json.loads(path.read_text(encoding='utf8'))
                if name.endswith('-等待拒绝复活')]
            self.assertEqual(len(pending), 11)
            for name in pending:
                self.assertTrue(context.run_recognition(name, obscured).hit, name)
            # This captured caption misses the template. Text by itself must
            # not enable the fallback without a confirmation frame.
            import numpy as np
            caption_only = np.full_like(prompt, (50,60,70))
            caption_only[316:350,440:810] = prompt[316:350,440:810]
            self.assertFalse(context.run_recognition('Result_RevivePrompt', caption_only).hit)
            for path in sorted((ROOT/'tests/fixtures/navigation/common-dialog').glob('*.png')):
                other = read_png_bgr(path)
                self.assertFalse(context.run_recognition('Result_RevivePrompt', other).hit, path.name)
                self.assertFalse(context.run_recognition('Result_DeclineRevive', other).hit, path.name)
            # This real free-revival caption scored 0.858 against the SWF
            # template (threshold 0.88). It used to fall into common-confirm
            # fatal handling before the decline / defeat route could run.
            monitor = BattleOutcomeMonitor(context, policy)
            try:
                monitor.start()
                self.assertIsNone(monitor.error)
                self.assertEqual(monitor.branch, policy['revive_next'])
                self.assertEqual(events, [])
            finally:
                monitor.close()
            controller.source = lambda: failed if events and events[-1][0]=='up' else prompt
            result = context.run_task('__CapturedRevive', {
                '__CapturedRevive': dict(action=dict(type='DoNothing'),
                    next=policy['revive_next'], timeout=2000, post_delay=0),
                prefix+'战斗失败': dict(action=dict(type='DoNothing'), next=[]),
            })
            self.assertTrue(result and result.status.succeeded)
            self.assertEqual([e[0] for e in events], ['down', 'up'])
            self.assertTrue(614<=events[0][1]<710 and 416<=events[0][2]<458, events)

        self.run_memory_case(inspect, prompt)

    def test_layouts_follow_window_and_ignore_cancel_glyphs(self):
        import numpy as np
        from fixture_pixels import read_png_bgr
        folder = ROOT/'tests/fixtures/navigation/revival'
        cases = json.loads((folder/'cases.json').read_text(encoding='utf8'))
        frames = [(c, read_png_bgr(folder/c['file'])) for c in cases]
        for c in json.loads((FIXTURES/'cases.json').read_text(encoding='utf8'))['cases']:
            frames.append(({**c, 'players':'p1'}, read_png_bgr(FIXTURES/c['file'])))

        def inspect(context, controller, events):
            for case, original in frames:
                for dx,dy in [(0,0),(-100,-60),(80,70)]:
                    frame = np.full_like(original, (50,60,70))
                    sx,sy,tx,ty = max(0,-dx),max(0,-dy),max(0,dx),max(0,dy)
                    h,w = original.shape[:2]; cw,ch=min(w-sx,w-tx),min(h-sy,h-ty)
                    frame[ty:ty+ch,tx:tx+cw] = original[sy:sy+ch,sx:sx+cw]
                    x,y,w,h = case['cancel_box']; x+=dx; y+=dy
                    # Keep outlines while destroying cancel text and countdown.
                    frame[y+12:y+h-5,x+8:x+w-8] = (170,120,50)
                    other = 'P2' if case['players']=='p1' else 'P1'
                    self.assertFalse(context.run_recognition('Result_ReviveLayout'+other,frame).hit,case)
                    controller.frame=frame; events.clear()
                    result=context.run_task('__CancelInvoke', {'__CancelInvoke':dict(
                        action=dict(type='DoNothing'),next=['Result_DeclineRevive'],timeout=1200,post_delay=0)})
                    self.assertTrue(result and result.status.succeeded,(case,dx,dy))
                    self.assertEqual([e[0] for e in events],['down','up'])
                    self.assertTrue(x<=events[0][1]<=x+w and y<=events[0][2]<=y+h,(case,events))
            # Shared border skins, even with a recognizable resurrection caption,
            # cannot turn an arbitrary confirmation into a cancel target.
            caption=read_png_bgr(ROOT/'assets/resource/base/image/navigation/outcomes/revive-normal-caption.png')
            for path in sorted((ROOT/'tests/fixtures/navigation/common-dialog').glob('*.png')):
                frame=read_png_bgr(path); h,w=caption.shape[:2]; frame[285:285+h,440:440+w]=caption
                self.assertFalse(context.run_recognition('Result_DeclineRevive',frame).hit,path.name)

        self.run_memory_case(inspect,frames[0][1])

    def test_missing_layout_waits_for_ten_second_expiry_and_confirms_failure(self):
        from fixture_pixels import read_png_bgr
        prompt=read_png_bgr(FIXTURES/'normal-p1-countdown1.png')
        # Cover the entire button row, retaining identity and all three corners.
        prompt[400:452,330:810]=0
        failed=read_png_bgr(ROOT/'tests/fixtures/navigation/challenge-failed.png')
        prefix='悬赏（守卫太宗）-'

        def inspect(context, controller, events):
            self.assertTrue(context.run_recognition('Result_RevivePrompt',prompt).hit)
            self.assertFalse(context.run_recognition('Result_DeclineRevive',prompt).hit)
            start=time.monotonic()
            controller.source=lambda: prompt if time.monotonic()-start<10.1 else failed
            result=context.run_task('__WaitForNaturalDefeat',{
                '__WaitForNaturalDefeat':dict(action=dict(type='DoNothing'),next=[prefix+'等待拒绝复活'],timeout=2000,post_delay=0),
                # Keep the production recognition and predecessor's budget;
                # end at the actual defeat instead of entering another battle.
                prefix+'战斗失败':dict(action=dict(type='DoNothing'),next=[]),
            })
            self.assertTrue(result and result.status.succeeded)
            self.assertGreaterEqual(time.monotonic()-start,10.1)
            self.assertEqual(events,[])
            self.assertTrue(context.run_recognition(prefix+'战斗失败',failed).hit)

        self.run_memory_case(inspect,prompt)


if __name__ == '__main__':
    unittest.main()
