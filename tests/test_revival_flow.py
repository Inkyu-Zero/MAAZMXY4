"""Revival rejection is a guarded transition, not an automatic failure count."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'));sys.path.insert(0,str(ROOT/'tools'))
from navigation.catalog import task_graph
from navigation import select_state,shortest_path
from task_catalog import custom_parameters
from configure_task_navigation import configure


class RevivalFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes={name:node for f in (ROOT/'assets/resource/base/pipeline').glob('*.json')
            for name,node in json.loads(f.read_text(encoding='utf8')).items()}

    def test_all_battle_tasks_reject_before_marking_failure_or_returning(self):
        count=0
        for name,node in self.nodes.items():
            params=custom_parameters(node)
            if not params.get('navigation'):continue
            count+=1
            prefix=name.rsplit('-',1)[0]+'-'
            # Entry may be named differently; derive the explicit policy branch.
            decline=params['result_guard']['revive_next'][0]
            prefix=decline.removesuffix('拒绝还魂丹')
            self.assertEqual(self.nodes[decline]['action']['type'],'Click')
            self.assertEqual(self.nodes[decline]['recognition']['type'],'And')
            self.assertEqual(self.nodes[decline]['next'],[prefix+'战斗失败'])
            self.assertFalse(custom_parameters(self.nodes[decline]))
            pending=prefix+'等待拒绝复活'
            self.assertEqual(self.nodes[pending]['action']['type'],'DoNothing')
            self.assertEqual(self.nodes[pending]['next'],[decline,prefix+'战斗失败'])
            self.assertEqual(self.nodes[pending]['on_error'],[prefix+'失败'])
            for route in params['routes'].values():
                self.assertLess(route.index(decline),route.index(pending))
                self.assertLess(route.index(pending),route.index(prefix+'战斗失败'))
            for suffix in ('战斗失败','失败返回地图','成功返回地图'):
                self.assertIn('Result_NoRevivePrompt',self.nodes[prefix+suffix]['recognition']['param']['all_of'])
        self.assertEqual(count,11)

    def test_overlay_wins_over_background_and_has_only_the_decline_route(self):
        class Context:
            def get_node_data(self,name):return RevivalFlowTests.nodes[name]
            def override_pipeline(self,_):return True
        params=next(custom_parameters(n) for n in self.nodes.values() if custom_parameters(n).get('navigation'))
        graph=task_graph(Context(),params)
        self.assertEqual(select_state(graph,{'Result_RevivePrompt','Result_Failed','Result_Succeeded','Nav_Probe_hub_scene'}),'revive_prompt')
        route=shortest_path(graph,'revive_prompt')
        self.assertEqual(route[0]['entry'],'Result_DeclineRevive')
        self.assertEqual(route[0]['target'],'challenge_failed')

    def test_generator_is_idempotent_and_retry_count_has_no_second_off_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            target=root/'assets/resource/base/pipeline'
            shutil.copytree(ROOT/'assets/resource/base/pipeline',target)
            shutil.copyfile(ROOT/'assets/interface.json',root/'assets/interface.json')
            before={f.relative_to(root):f.read_bytes() for f in root.rglob('*.json')}
            configure(root)
            after={f.relative_to(root):f.read_bytes() for f in root.rglob('*.json')}
            self.assertEqual(before,after)
            interface=json.loads((root/'assets/interface.json').read_text(encoding='utf8'))
            import re
            pattern=interface['option']['失败重试设置']['inputs'][0]['verify']
            for value in ('1','3','20'): self.assertIsNotNone(re.fullmatch(pattern,value))
            for value in ('0','-1','21'): self.assertIsNone(re.fullmatch(pattern,value))

    def test_imported_ui_assets_match_the_recorded_hashes(self):
        manifest=json.loads((ROOT/'agent/data/navigation/battle-ui.json').read_text(encoding='utf8'))
        self.assertEqual(len(manifest['assets']),14)
        for asset in manifest['assets']:
            path=ROOT/'assets/resource/base/image'/asset['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),asset['sha256'],asset['path'])


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1','opt-in memory-only native recognition')
class NativeRevivalTests(unittest.TestCase):
    def test_only_decline_buttons_match_and_background_returns_are_blocked(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        from battle_ui import NoRevivePrompt
        folder=ROOT/'tests/fixtures/navigation/revival'
        cases=json.loads((folder/'cases.json').read_text(encoding='utf8'))
        seen=[];completed=[]
        class Pixels(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'revival-memory-only'
            def screencap(self):return read_png_bgr(folder/cases[0]['file'])
            def forbidden(self,*args):raise AssertionError('Recognition-only check cannot send input')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        class Inspect(CustomAction):
            def run(self,context,argv):
                for case in cases:
                    pixels=read_png_bgr(folder/case['file'])
                    prompt=context.run_recognition('Result_RevivePrompt',pixels)
                    cancel=context.run_recognition('Result_DeclineRevive',pixels)
                    assert prompt and prompt.hit and cancel and cancel.hit,case
                    x,y,w,h=case['cancel_box'];box=cancel.box
                    assert x<=box[0] and y<=box[1] and box[0]+box[2]<=x+w and box[1]+box[3]<=y+h,(case,box)
                    clear=context.run_recognition('Result_NoRevivePrompt',pixels)
                    assert clear and not clear.hit
                    # Caption without a visible cancel must interrupt playback,
                    # but cannot authorize a click on the result behind it.
                    pixels[y:y+h,x:x+w]=0
                    assert context.run_recognition('Result_RevivePrompt',pixels).hit
                    assert not context.run_recognition('Result_DeclineRevive',pixels).hit
                    captionless=read_png_bgr(folder/case['file'])
                    # Remove the caption, not the panel's upper-right corner.
                    # The latter would test a partly missing panel instead.
                    cx,cy,cw,ch=prompt.box
                    captionless[cy:cy+ch,cx:cx+cw]=0
                    assert not context.run_recognition('Result_RevivePrompt',captionless).hit
                    assert not context.run_recognition('Result_DeclineRevive',captionless).hit
                    assert context.run_recognition('Nav_CommonConfirm',captionless).hit
                    assert not context.run_recognition('Result_NoRevivePrompt',captionless).hit
                    seen.append(case['file'])
                for fixture in ('profile','map-a','challenge-failed'):
                    image=read_png_bgr(ROOT/'tests/fixtures/navigation'/(fixture+'.png'))
                    assert not context.run_recognition('Result_RevivePrompt',image).hit,fixture
                    assert not context.run_recognition('Result_DeclineRevive',image).hit,fixture
                    assert context.run_recognition('Result_NoRevivePrompt',image).hit,fixture
                for kind in ('Failed','Succeeded'):
                    background=read_png_bgr(ROOT/'tests/fixtures/navigation'/('challenge-'+kind.lower()+'.png'))
                    assert context.run_recognition('Result_'+kind+'Return',background).hit,kind
                    # Retain the result title/button and add only the public
                    # caption elsewhere in its ROI, so absence is decisive.
                    caption=read_png_bgr(ROOT/'assets/resource/base/image/navigation/outcomes/revive-normal-caption.png')
                    h,w=caption.shape[:2];background[175:175+h,205:205+w]=caption
                    assert context.run_recognition('Result_RevivePrompt',background).hit,kind
                    assert not context.run_recognition('Result_'+kind+'Return',background).hit,kind
                for case in json.loads((folder/'return-cases.json').read_text(encoding='utf8')):
                    pixels=read_png_bgr(folder/case['file'])
                    kind=case['kind'].capitalize()
                    returned=context.run_recognition('Result_'+kind+'Return',pixels)
                    assert returned and returned.hit,case
                    x,y,w,h=case['button_box'];box=returned.box
                    # Legacy crops can win first and include extra padding;
                    # their click center must still lie in the real button.
                    assert x<=box[0]+box[2]/2<=x+w and y<=box[1]+box[3]/2<=y+h,(case,box)
                    swf=context.run_recognition('__Swf'+kind+'Return',pixels)
                    assert swf and swf.hit and abs(swf.box[0]-x)<=2 and abs(swf.box[1]-y)<=2,(case,swf)
                    other='Succeeded' if kind=='Failed' else 'Failed'
                    assert not context.run_recognition('Result_'+other+'Return',pixels).hit,case
                    pixels[120:500,200:980]=0
                    assert not context.run_recognition('Result_'+kind+'Return',pixels).hit,case
                completed.append(True)
                return True
        logs=ROOT/'.runtime-cache/native-revival';logs.mkdir(parents=True,exist_ok=True);Tasker.set_log_dir(logs)
        resource,controller,tasker=Resource(),Pixels(),Tasker()
        self.assertTrue(controller.post_connection().wait().succeeded);controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
        definitions=json.loads((ROOT/'assets/resource/base/pipeline/battle_outcomes.json').read_text(encoding='utf8'))
        for kind in ('Failed','Succeeded'):
            variants=definitions['Result_'+kind+'Return']['recognition']['param']['all_of'][0]['recognition']['param']['any_of'][1]
            resource.override_pipeline({'__Swf'+kind+'Return':variants})
        resource.register_custom_action('__revive_inspect',Inspect())
        resource.override_pipeline({'__ReviveInspect':{'action':{'type':'Custom','param':{'custom_action':'__revive_inspect'}}}})
        self.assertTrue(tasker.bind(resource,controller));self.assertTrue(tasker.post_task('__ReviveInspect').wait().succeeded)
        self.assertEqual(len(seen),12)
        self.assertEqual(completed,[True],'All negative cases must finish inside the native callback')


if __name__=='__main__':unittest.main()
