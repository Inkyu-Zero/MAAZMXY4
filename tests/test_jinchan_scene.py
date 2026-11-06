"""Masked, non-personal scene excerpts preserve both reported orientations."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests/fixtures/navigation/jinchan'


class JinchanSceneAssetsTests(unittest.TestCase):
    def test_fixtures_keep_exact_provenance(self):
        data=json.loads((FIXTURES/'cases.json').read_text(encoding='utf8'))
        self.assertEqual(len(data['cases']),2)
        for case in data['cases']:
            self.assertEqual(hashlib.sha256((FIXTURES/case['file']).read_bytes()).hexdigest(),case['sha256'])
            self.assertEqual(case['roi'][2:],[134,125])


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1','opt-in memory-only recognition')
class JinchanSceneNativeTests(unittest.TestCase):
    def test_both_orientations_match_and_maps_do_not(self):
        sys.path.insert(0,str(ROOT/'agent'))
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        cases=[(FIXTURES/'left.png',True),(FIXTURES/'right.png',True),
               (ROOT/'tests/fixtures/navigation/map-a.png',False),
               (ROOT/'tests/fixtures/navigation/challenge-failed.png',False)]
        failures=[]
        class Pixels(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'jinchan-background-readonly'
            def screencap(self):return read_png_bgr(cases[-1][0])
            def forbidden(self,*args):raise AssertionError('Recognition must not send input')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        class Inspect(CustomAction):
            def run(self,context,argv):
                for file,expected in cases:
                    result=context.run_recognition('贪妄（金蟾宝宫）-识别场景1',read_png_bgr(file))
                    if bool(result and result.hit)!=expected:failures.append(file.name)
                return True
        Tasker.set_log_dir(ROOT/'.runtime-cache/native-jinchan')
        controller,resource,tasker=Pixels(),Resource(),Tasker()
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        from battle_ui import NoRevivePrompt
        resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
        resource.register_custom_action('__gold_scene',Inspect())
        resource.override_pipeline({'__GoldScene':dict(action=dict(type='Custom',param=dict(custom_action='__gold_scene')))})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__GoldScene').wait().succeeded)
        self.assertFalse(failures)
