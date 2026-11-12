"""Stage entry depends on common controls, never on background identity."""
import json
import hashlib
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]

def with_stage_controls(frame):
    """Synthetic offline scene: add a real shared HUD, no desktop capture."""
    from fixture_pixels import read_png_bgr
    hud=read_png_bgr(ROOT/'tests/fixtures/bounty-candle/candle-battle-ready.png')
    result=frame.copy()
    result[600:720,:]=hud[600:720,:result.shape[1]]
    return result

class SharedStageAssetsTests(unittest.TestCase):
    def test_all_rounds_and_playback_nodes_use_shared_stage_recognition(self):
        nodes={n:v for p in (ROOT/'assets/resource/base/pipeline').glob('*.json')
               for n,v in json.loads(p.read_text(encoding='utf8')).items()}
        rounds=[]
        for node in nodes.values():
            if not isinstance(node.get('action'),dict):continue
            params=node.get('action',{}).get('param',{}).get('custom_action_param',{})
            if not isinstance(params,dict) or 'round_flow' not in params:continue
            rounds.append(params)
            self.assertEqual(params['round_flow']['scene_node'],'Nav_InStage')
            self.assertEqual(nodes[params['playback_node']]['recognition'],
                             {'type':'And','param':{'all_of':['Nav_InStage']}})
        self.assertEqual(len(rounds),73)
        self.assertEqual(nodes['Nav_InStage']['recognition']['param']['all_of'][0],'Nav_Wushuang')
        self.assertEqual(len(nodes['Nav_Wushuang']['recognition']['param']['template']),2)
        for asset in json.loads((ROOT/'agent/data/navigation/stage-ui.json').read_text(encoding='utf8'))['assets']:
            self.assertEqual(len(asset['source_sha256']),64)
            self.assertEqual(hashlib.sha256((ROOT/'assets/resource/base/image'/asset['path']).read_bytes()).hexdigest(),asset['sha256'])

@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1','opt-in Maa memory recognition')
class SharedStageNativeTests(unittest.TestCase):
    def test_background_is_irrelevant_but_map_hub_loading_and_modal_are_rejected(self):
        import numpy as np
        from fixture_pixels import read_png_bgr
        from test_revival_anchor import RevivalAnchorNativeTests
        actual=read_png_bgr(ROOT/'tests/fixtures/bounty-candle/candle-battle-ready.png')
        plain=with_stage_controls(np.full_like(actual,35))
        # The gear is absent: Wushuang alone supplies the positive stage cue.
        plain[600:720,400:]=35
        ready=plain.copy()
        ready[641:681,14:92]=read_png_bgr(ROOT/'assets/resource/base/image/navigation/stage/wushuang-ready.png')
        def inspect(context,controller,events):
            from unittest.mock import patch
            from navigation.recognition import clear_cache
            for frame in (actual,plain,ready):
                clear_cache();calls=[]
                original=type(context).run_recognition
                def observed(ctx,name,*args,**kwargs):
                    calls.append(name)
                    return original(ctx,name,*args,**kwargs)
                with patch.object(type(context),'run_recognition',observed):
                    for name in ('Nav_InStage','Mainline_034_Play','Mainline_046_Play','Bounty_Play',
                                 '伽岚（万世冥阁）-识别场景1'):
                        self.assertTrue(context.run_recognition(name,frame).hit,name)
                self.assertEqual(calls.count('Nav_ModalPresent'),1)
                self.assertEqual(calls.count('Nav_Probe_hub_world_entry'),1)
            no_button=actual.copy();no_button[580:720,:180]=35
            self.assertFalse(context.run_recognition('Nav_InStage',no_button).hit,'gear alone is insufficient')
            for folder,name in [('navigation','task-map-real'),('navigation','world'),
                                ('navigation','login'),('navigation','daily-expanded'),
                                ('bounty-candle','candle-hub-ready'),('bounty-candle','candle-loading')]:
                frame=read_png_bgr(ROOT/f'tests/fixtures/{folder}/{name}.png')
                self.assertFalse(context.run_recognition('Nav_InStage',frame).hit,name)
            for file in ('revival-live/vip-p1-countdown10.png','revival-live/normal-p1-countdown1.png'):
                frame=with_stage_controls(read_png_bgr(ROOT/'tests/fixtures/navigation'/file))
                self.assertFalse(context.run_recognition('Nav_InStage',frame).hit,file)
            self.assertEqual(events,[])
        RevivalAnchorNativeTests().run_memory_case(inspect,actual)

if __name__=='__main__':unittest.main()
