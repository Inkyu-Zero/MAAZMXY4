"""Arena configuration and reuse contracts, independent of live captures."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from task_catalog import task_catalog, task_key
from navigation.catalog import task_routine


class ArenaTasksTests(unittest.TestCase):
    def test_fifth_opponent_is_default_and_all_choices_use_shared_round(self):
        interface=json.loads((ROOT/'assets/interface.json').read_text(encoding='utf8'))
        task=next(t for t in task_catalog(ROOT,bindings={}) if t['entry']=='Arena')
        self.assertEqual(task['id'],task_key('竞技场','Arena'))
        params=task['management_profile']['parameters']
        self.assertEqual(params['round_flow']['scene_node'],'Nav_InStage')
        self.assertTrue(params['round_flow']['require_victory'])
        self.assertFalse(params['failure_retry']['enabled'])
        option=interface['option']['竞技场对手']
        self.assertEqual(option['default_case'],'第 5 位普通对手')
        positions=[c['pipeline_override']['Arena_Target']['action']['param']['target'] for c in option['cases']]
        self.assertEqual(positions,[[386+159*i,273,1,1] for i in range(5)]+[[386,490,1,1]])
        nodes=json.loads((ROOT/'assets/resource/base/pipeline/arena.json').read_text(encoding='utf8'))
        self.assertEqual(nodes['Arena_Target']['action']['param']['target'],positions[4])
        self.assertEqual(nodes['Arena_Target']['recognition']['param']['all_of'],['Nav_See_arena','Result_NoRevivePrompt'])
        class Context:
            def get_node_data(self,name):return nodes[name]
            def override_pipeline(self,patch):return True
        routine=task_routine(Context(),params)
        for action in ('Result_FailedReturn','Result_SucceededReturn'):
            self.assertEqual(routine['actions'][action]['verify'],['arena'])

    def test_result_crops_keep_source_and_content_hashes(self):
        data=json.loads((ROOT/'agent/data/navigation/arena-assets.json').read_text(encoding='utf8'))
        self.assertEqual(len(data['source_sha256']),64)
        self.assertEqual(len(data['assets']),2)
        for asset in data['assets']:
            self.assertEqual(hashlib.sha256((ROOT/'assets/resource/base/image'/asset['template']).read_bytes()).hexdigest(),asset['sha256'])


if __name__=='__main__':unittest.main()
