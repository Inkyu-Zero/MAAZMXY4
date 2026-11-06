"""Bounded viewport restoration using public SWF pixels, never a desktop window."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))
from navigation.stage_view import DATA, restore_stage_view
from navigation.runtime import Navigator


class StageScrollTests(unittest.TestCase):
    def navigator(self, context, *, capture=lambda:object(), check=lambda:None):
        nav=Navigator(context,{},capture=capture,check=check,log=lambda _:None)
        nav.locate=lambda:'map'
        nav._settle=lambda:None
        return nav

    def test_horizontal_targets_share_safe_landmark_and_bounded_held_endpoint_gesture(self):
        data = json.loads(DATA.read_text(encoding='utf8'))
        nodes = json.loads((ROOT / 'assets/resource/base/pipeline/game_management_scroll.json').read_text(encoding='utf8'))
        recipes = {item['task']: item for item in data['maps']}
        self.assertNotIn('GameManagement_town5_TerrainBack',nodes)
        for task, end_x in [('伽岚（万世冥阁）', 20), ('无冥（孤魂岭）', 1000)]:
            self.assertEqual(recipes[task]['max_steps'],2)
            self.assertEqual(recipes[task]['end_x'],end_x)
            node = nodes[recipes[task]['drag_node']]
            guard = node['recognition']['param']
            self.assertEqual(guard['all_of'][:2], ['Nav_TaskMap', 'GameManagement_NormalMap'])
            self.assertEqual(guard['all_of'][3], 'Result_NoRevivePrompt')
            self.assertEqual(guard['box_index'], 2)
            action = node['action']['param']
            self.assertIs(action['begin'], True)
            self.assertIs(action['end'], True)
            self.assertEqual(action['end_offset'], [0, 0, 0, 0])
            self.assertEqual(action['duration'],900)
            self.assertEqual(action['end_hold'],200)
            self.assertEqual(guard['all_of'][2],'GameManagement_town5_Terrain')
            terrain=nodes[guard['all_of'][2]]['recognition']['param']
            self.assertEqual(terrain['template'],['navigation/scroll/town5-02.png'])
            self.assertEqual(terrain['roi'],[20,410,1020,120])

    def test_cancellation_after_recognition_prevents_pending_drag(self):
        recipe = json.loads(DATA.read_text(encoding='utf8'))['maps'][0]
        cancelled = False
        def check():
            if cancelled: raise InterruptedError('cancelled during recognition')
        def recognize(name, frame):
            nonlocal cancelled
            cancelled = name == recipe['drag_node']
            return SimpleNamespace(hit=cancelled)
        actions = []
        context = SimpleNamespace(run_recognition=recognize, run_action=lambda *args,**kwargs: actions.append(args))
        navigator = self.navigator(context,check=check)
        with self.assertRaises(InterruptedError): restore_stage_view(context, navigator, recipe['target'])
        self.assertFalse(actions)

    def test_relocalizes_after_drag_and_never_uses_a_saved_target_box(self):
        recipe = json.loads(DATA.read_text(encoding='utf8'))['maps'][0]
        frames = iter(['before', 'after'])
        seen, actions = [], []
        def recognize(name, frame):
            seen.append((name, frame))
            return SimpleNamespace(hit=name == recipe['drag_node'] or frame == 'after',box=(10,20,30,40))
        context = SimpleNamespace(run_recognition=recognize, run_action=lambda name,box,**kwargs:
            actions.append((name,box,kwargs)) or SimpleNamespace(success=True))
        navigator = self.navigator(context,capture=lambda:next(frames))
        navigator.locate=lambda:'task_destination' if navigator.recognize(recipe['target']).hit else 'map'
        self.assertTrue(restore_stage_view(context, navigator, recipe['target']))
        self.assertEqual(len(actions), 1)
        self.assertEqual(seen[-1], (recipe['target'], 'after'))
        swipe=actions[0][2]['pipeline_override'][recipe['drag_node']]['action']['param']
        self.assertEqual(swipe['begin'],[25,40,1,1])
        self.assertEqual(swipe['end'],[recipe['end_x'],40,1,1])
        self.assertEqual(swipe['begin_offset'],[0,0,0,0])
        self.assertEqual(swipe['end_offset'],[0,0,0,0])

    def test_landmarks_exclude_clickable_stages_and_have_pinned_sources(self):
        data = json.loads(DATA.read_text(encoding='utf8'))
        for asset in data['assets']:
            path = ROOT / 'assets/resource/base/image' / asset['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), asset['sha256'])
            self.assertEqual(len(asset['source_sha256']), 64)
        for recipe in data['maps']:
            self.assertLessEqual(recipe['max_steps'], 12)
            if 'crops' not in recipe:
                recipe=next(base for base in data['maps'] if base['drag_node']==recipe['drag_node'] and 'crops' in base)
            for crop in recipe['crops']:
                self.assertFalse(any(crop[0] < b[2] and crop[2] > b[0] and crop[1] < b[3] and crop[3] > b[1]
                                     for b in recipe['excluded_stage_bounds']))

    def test_partial_horizontal_gesture_relocalizes_before_one_correction(self):
        for recipe in json.loads(DATA.read_text(encoding='utf8'))['maps']:
            if recipe['town']!='town5':continue
            frames=iter(['start','partial','endpoint']);actions=[]
            def recognize(name,frame):
                return SimpleNamespace(hit=name==recipe['drag_node'] or frame=='endpoint',
                    box=(100 if frame=='start' else 300,420,40,40))
            context=SimpleNamespace(run_recognition=recognize,tasker=SimpleNamespace(controller=SimpleNamespace(info={})),
                run_action=lambda name,box,**kwargs:actions.append(kwargs) or SimpleNamespace(success=True))
            nav=self.navigator(context,capture=lambda:next(frames))
            nav.locate=lambda:'task_destination' if nav.recognize(recipe['target']).hit else 'map'
            self.assertTrue(restore_stage_view(context,nav,recipe['target']))
            self.assertEqual(len(actions),2)
            starts=[a['pipeline_override'][recipe['drag_node']]['action']['param']['begin'][0] for a in actions]
            self.assertEqual(starts,[120,320])

    def test_partial_drag_that_enters_a_stage_never_sends_a_second_gesture(self):
        recipe=next(r for r in json.loads(DATA.read_text(encoding='utf8'))['maps'] if r['town']=='town5')
        actions=[]
        context=SimpleNamespace(run_recognition=lambda name,frame:SimpleNamespace(hit=name==recipe['drag_node'],box=(100,420,40,40)),
            run_action=lambda name,box,**kwargs:actions.append(name) or SimpleNamespace(success=True))
        nav=self.navigator(context)
        nav.locate=lambda:'map' if not actions else 'settings_menu'
        self.assertFalse(restore_stage_view(context,nav,recipe['target']))
        self.assertEqual(len(actions),1)

    def test_no_drag_on_unknown_map_missing_guard_or_cancellation_and_bounded_failure(self):
        recipe = json.loads(DATA.read_text(encoding='utf8'))['maps'][0]
        target = recipe['target']
        actions = []
        context = SimpleNamespace(
            run_recognition=lambda name, frame: SimpleNamespace(hit=name == recipe['drag_node'],box=(10,20,30,40)),
            run_action=lambda name, box, **kwargs: actions.append((name, box)) or SimpleNamespace(success=True))
        navigator = self.navigator(context)
        navigator.locate=lambda:'profile'
        self.assertFalse(restore_stage_view(context, navigator, target))
        self.assertEqual(actions, [])
        navigator.locate = lambda: 'map'
        context.run_recognition = lambda *_: SimpleNamespace(hit=False)
        self.assertFalse(restore_stage_view(context, navigator, target))
        self.assertEqual(actions, [])
        def cancelled(): raise InterruptedError('cancelled')
        navigator._check = cancelled
        with self.assertRaises(InterruptedError): restore_stage_view(context, navigator, target)
        self.assertEqual(actions, [])
        navigator._check = lambda: None
        navigator.invalidate()
        context.run_recognition = lambda name, _: SimpleNamespace(hit=name == recipe['drag_node'],box=(10,20,30,40))
        self.assertFalse(restore_stage_view(context, navigator, target))
        self.assertEqual(len(actions), recipe['max_steps'])
        self.assertTrue(all(n == recipe['drag_node'] and box==(10,20,30,40) for n,box in actions))


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS') == '1', 'opt-in Maa memory controller')
class StageScrollNativeTests(unittest.TestCase):
    def test_scroll_targets_restore_and_reject_wrong_mode_map_and_prompt(self):
        import numpy as np
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from battle_ui import NoRevivePrompt
        from fixture_pixels import read_png_bgr
        from navigation.catalog import task_routine
        from navigation.runtime import Navigator
        fixtures = ROOT / 'tests/fixtures/game-management/scroll'
        recipes = [r for r in json.loads(DATA.read_text(encoding='utf8'))['maps'] if 'fixture' in r]
        maps = {r['town']: read_png_bgr(fixtures / r['fixture']) for r in recipes}
        mode = read_png_bgr(ROOT / 'assets/resource/base/image/navigation/scroll/normal-mode.png')
        task = read_png_bgr(ROOT / 'assets/resource/base/image/任务.png')
        checks, errors = [], []

        class Pixels(CustomController):
            recipe = recipes[0]
            offset = 0
            gestures = []
            entering = False
            entries = []
            stage_bounds = ()
            def connect(self): return True
            def request_uuid(self): return 'scroll-memory-only'
            def screencap(self):
                x, y = (self.offset, 0) if self.recipe['axis'] == 'x' else (0, self.offset)
                frame = maps[self.recipe['town']][y:y+720, x:x+1147].copy()
                frame[620:620+task.shape[0], 800:800+task.shape[1]] = task
                # Green-mask pixels are replaced with the actual map; the fixture
                # map uses an independently resampled whole SWF frame.
                region = frame[617:617+mode.shape[0], 1036:1036+mode.shape[1]]
                mask = np.any(mode != (0, 255, 0), axis=2)
                region[mask] = mode[mask]
                return frame
            def touch_down(self, contact, x, y, pressure):
                self.start = self.end = (x, y)
                return True
            def touch_move(self, contact, x, y, pressure):
                self.end = (x, y)
                return True
            def touch_up(self, contact):
                if self.entering:
                    x, y = self.start
                    x += self.offset if self.recipe['axis'] == 'x' else 0
                    y += self.offset if self.recipe['axis'] == 'y' else 0
                    x1,y1,x2,y2 = [v*720/590 for v in self.stage_bounds]
                    assert x1 <= x <= x2 and y1 <= y <= y2, ('stale stage click',self.recipe['task'],self.offset,self.start)
                    self.entries.append((self.offset,self.start))
                    return True
                if self.recipe['axis']=='x':
                    # The source MovieClipButton has no drag-distance filter.
                    # Check the press and the complete clamped gesture against
                    # independently extracted clickable stage bounds.
                    maximum=round((1500-940)*720/590)
                    for fraction in (i/100 for i in range(101)):
                        x=self.start[0]+(self.end[0]-self.start[0])*fraction
                        offset=min(maximum,max(0,self.offset+self.start[0]-x))
                        sx,sy=(x+offset)*590/720,self.start[1]*590/720
                        assert not any(a<=sx<=c and b<=sy<=d
                            for a,b,c,d in self.recipe['excluded_stage_bounds']), ('unsafe drag',sx,sy)
                self.gestures.append((self.start, self.end))
                axis = 0 if self.recipe['axis'] == 'x' else 1
                delta = self.start[axis] - self.end[axis]
                maximum = round(((1500-940) if axis == 0 else (2714-590))*720/590)
                self.offset = min(maximum, max(0, self.offset + delta))
                return True
            def forbidden(self, *args): raise AssertionError('Unexpected input')
            click = swipe = key_down = key_up = input_text = start_app = stop_app = forbidden

        controller = Pixels()
        class Inspect(CustomAction):
            def run(self, context, argv):
                try:
                    for recipe in recipes:
                        controller.recipe = recipe
                        # Authored stage bounds from Town5/6 levels_container,
                        # independent of the cropped runtime templates. Check
                        # endpoints and intermediate cached viewport positions.
                        stage_bounds = {'伽岚（万世冥阁）': (1099.8, 342.95, 1202.8, 426.95),
                                        '无冥（孤魂岭）': (211, 66, 361, 216),
                                        '贪妄（金蟾宝宫）': (71, 2352, 346, 2560)}[recipe['task']]
                        maximum = round(((1500-940) if recipe['axis']=='x' else (2714-590))*720/590)
                        positions=[round(maximum*f) for f in (0, .25, .5, .75, 1)]
                        if recipe['town']=='town6':positions += [1850,1887,1920]
                        for position in positions:
                            controller.offset = position
                            pixels = controller.screencap()
                            hit = context.run_recognition(recipe['target'], pixels)
                            assert hit is not None
                            if hit.hit:
                                x,y,w,h = hit.box
                                map_x = x+w/2+(controller.offset if recipe['axis']=='x' else 0)
                                map_y = y+h/2+(controller.offset if recipe['axis']=='y' else 0)
                                x1,y1,x2,y2 = [v*720/590 for v in stage_bounds]
                                assert x1 <= map_x <= x2 and y1 <= map_y <= y2, (recipe['task'], controller.offset, hit.box)
                            else:
                                assert context.run_recognition(recipe['drag_node'], pixels).hit, ('viewport gap',recipe['task'],controller.offset)
                            if recipe['axis']=='x':
                                graph=task_routine(context,dict(navigation=dict(kind='stage',target_node=recipe['target'])))
                                nav=Navigator(context,graph,capture=controller.screencap,check=lambda:None)
                                previous=len(controller.gestures)
                                assert restore_stage_view(context,nav,recipe['target'])
                                assert len(controller.gestures)-previous==int(not hit.hit)
                                if not hit.hit:
                                    assert controller.offset==(0 if recipe['view']=='left' else maximum)
                        controller.offset = round((1500-940)*720/590) if recipe['drag_node'].endswith('Back') else 0
                        controller.gestures = []
                        frame = controller.screencap()
                        assert not context.run_recognition(recipe['target'], frame).hit, recipe['town']
                        assert context.run_recognition(recipe['drag_node'], frame).hit, ('initial guard', recipe['town'])
                        wrong_mode = frame.copy(); wrong_mode[580:720, 980:1147] = 0
                        assert not context.run_recognition(recipe['drag_node'], wrong_mode).hit, 'wrong mode'
                        other = next(r for r in recipes if r['town'] != recipe['town'])
                        assert not context.run_recognition(other['drag_node'], frame).hit, 'wrong map'
                        prompt = read_png_bgr(ROOT / 'tests/fixtures/navigation/revival/normal-p1-up.png')
                        # Preserve recognized map/mode/terrain under the modal.
                        modal = frame.copy(); modal[100:550, 270:880] = prompt[100:550, 270:880]
                        assert not context.run_recognition(recipe['drag_node'], modal).hit, 'revival prompt'
                        graph = task_routine(context, dict(navigation=dict(kind='stage', target_node=recipe['target'])))
                        navigator = Navigator(context, graph, capture=controller.screencap, check=lambda: None)
                        assert restore_stage_view(context, navigator, recipe['target']), (recipe['town'], controller.offset)
                        assert 0 < len(controller.gestures) <= recipe['max_steps'], controller.gestures
                        assert context.run_recognition(recipe['target'], controller.screencap()).hit
                        checks.append((recipe['town'], controller.offset, len(controller.gestures)))
                        # An unchanged next round must not drag. Manual drift
                        # out of the ROI gets one correction, then a fresh click.
                        controller.stage_bounds, controller.entries = stage_bounds, []
                        initial_offset = controller.offset
                        changed_offset=round(maximum/2) if recipe['axis']=='x' else initial_offset-80
                        for offset in (initial_offset, initial_offset, changed_offset):
                            controller.offset = offset
                            before=len(controller.gestures)
                            nav = Navigator(context, graph, capture=controller.screencap, check=lambda: None)
                            assert restore_stage_view(context, nav, recipe['target'])
                            if offset==initial_offset:
                                assert len(controller.gestures)==before
                            elif recipe['axis']=='x':
                                assert len(controller.gestures)==before+1
                                assert controller.offset==initial_offset
                            controller.entering = True
                            try:
                                assert nav.perform(recipe['target']), ('entry failed',recipe['task'])
                            finally:
                                controller.entering = False
                        assert len(controller.entries) == 3
                        if recipe['axis']=='x':
                            assert all(entry[0]==initial_offset for entry in controller.entries)
                        else:
                            assert controller.entries[0][0] != controller.entries[-1][0], controller.entries
                    return True
                except Exception as error:
                    import traceback
                    errors.append(traceback.format_exc())
                    return False
        logs = Path(os.environ.get('NAVIGATION_SCROLL_TEST_LOG',ROOT / '.runtime-cache/native-scroll'))
        logs.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(logs)
        resource, tasker = Resource(), Tasker()
        resource.register_custom_recognition('battle_no_revive', NoRevivePrompt())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__scroll_inspect', Inspect())
        resource.override_pipeline({'__ScrollInspect': dict(action=dict(type='Custom', param=dict(custom_action='__scroll_inspect')))})
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__ScrollInspect').wait().succeeded, '\n'.join(errors))
        self.assertEqual(errors, [])
        self.assertEqual(len(checks), len(recipes), checks)
        print('SCROLL', checks)


if __name__ == '__main__': unittest.main()
