"""Native recognition on independently rendered SWF states; never desktop input."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
import tempfile

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests/fixtures/navigation/stage-states'


class StageVariantAssetsTests(unittest.TestCase):
    def test_generated_json_uses_stable_lf_bytes_on_every_platform(self):
        path = ROOT / 'tools/research/import_stage_variants.py'
        spec = importlib.util.spec_from_file_location('stage_variants_generator', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'generated.json'
            module.write_json(output, {'关卡': ['普通', '悬停']})
            first = output.read_bytes()
            self.assertNotIn(b'\r\n', first)
            module.write_json(output, json.loads(first.decode('utf8')))
            self.assertEqual(output.read_bytes(), first)

    def test_sources_and_templates_are_hashed_and_clicks_stay_guarded(self):
        data=json.loads((ROOT/'agent/data/navigation/stage-variants.json').read_text(encoding='utf8'))
        self.assertEqual(len(data['stages']),7)
        for stage in data['stages']:
            for variant in stage['variants']:
                path=ROOT/'assets/resource/base/image'/variant['path']
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),variant['sha256'])
            pipeline=json.loads((ROOT/'assets/resource/base/pipeline'/(stage['task']+'.json')).read_text(encoding='utf8'))
            node=pipeline[stage['task']+'-关卡入口']
            self.assertEqual(node['recognition']['type'],'And')
            self.assertEqual(node['recognition']['param']['box_index'],1)
            self.assertEqual(node['action'],{'type':'Click'})
            # Upstream adds two bounded retries. Each must keep the same target
            # recognition/modal guard and end at failure, not a blind click.
            previous = node
            for attempt in (1, 2):
                name = stage['task'] + '-关卡入口_重试' + str(attempt)
                self.assertEqual(previous['on_error'], [name])
                retry = pipeline[name]
                self.assertEqual(retry['recognition'], node['recognition'])
                self.assertEqual(retry['action'], {'type':'Click'})
                previous = retry
            self.assertEqual(previous['on_error'], [stage['task']+'-失败'])
            if stage['town'] == 'town5':
                # Both legacy and SWF states must follow a movable icon across
                # the viewport, instead of assuming its initial map position.
                alternatives = node['recognition']['param']['all_of'][1]['recognition']['param']['any_of']
                for choice in alternatives:
                    self.assertNotIn('roi', choice['recognition']['param'])


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1','opt-in memory-only native recognition')
class StageVariantNativeTests(unittest.TestCase):
    def test_non_blood_sea_button_states_match_only_the_intended_stage(self):
        self.check_states(blood_sea=False)

    @unittest.skipUnless(os.environ.get('NAVIGATION_BLOOD_SEA_TESTS')=='1','blood-sea validation explicitly deferred')
    def test_blood_sea_button_states_match_only_the_intended_stage(self):
        self.check_states(blood_sea=True)

    def check_states(self, *, blood_sea):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        import sys
        sys.path.insert(0,str(ROOT/'agent'))
        from battle_ui import NoRevivePrompt
        cases=json.loads((FIXTURES/'cases.json').read_text(encoding='utf8'))
        blood_tasks={'无冥（孤魂岭）','伽岚（万世冥阁）'}
        cases=[c for c in cases if (c['task'] in blood_tasks)==blood_sea]
        tasks=list(dict.fromkeys(case['task'] for case in cases))
        checks=[];completed=[]
        class Pixels(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'swf-states-memory-only'
            def screencap(self):return read_png_bgr(FIXTURES/cases[0]['file'])
            def forbidden(self,*args):raise AssertionError('This analysis must never send input')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        class Inspect(CustomAction):
            def run(self,context,argv):
                for case in cases:
                    pixels=read_png_bgr(FIXTURES/case['file'])
                    hits=[]
                    for task in tasks:
                        detail=context.run_recognition(task+'-关卡入口',pixels)
                        assert detail is not None,task
                        if detail.hit:hits.append(task)
                    assert hits==[case['task']],(case,hits)
                    checks.append(case)
                # Removing the map's task button must block even a perfect icon.
                pixels=read_png_bgr(FIXTURES/cases[0]['file']);pixels[600:]=0
                detail=context.run_recognition(tasks[0]+'-关卡入口',pixels)
                assert detail is not None and not detail.hit
                completed.append(True)
                return True
        log=ROOT/'.runtime-cache/native-stage-variants';log.mkdir(parents=True,exist_ok=True);Tasker.set_log_dir(log)
        resource,controller,tasker=Resource(),Pixels(),Tasker()
        resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
        self.assertTrue(controller.post_connection().wait().succeeded);controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__inspect_stage_states',Inspect())
        resource.override_pipeline({'__StageStates':{'action':{'type':'Custom','param':{'custom_action':'__inspect_stage_states'}}}})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__StageStates').wait().succeeded)
        self.assertEqual(len(checks),6 if blood_sea else 15)
        self.assertEqual(completed,[True],'The missing-map negative must finish inside the native callback')


if __name__=='__main__':unittest.main()
