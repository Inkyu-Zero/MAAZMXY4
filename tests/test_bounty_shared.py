"""One bounty lifecycle with four stable, independently bound script profiles."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'agent'),str(ROOT/'tools')]
from task_catalog import task_catalog, node_links
from build_bounty_tasks import build


class SharedBountyTests(unittest.TestCase):
    def setUp(self):
        self.base=ROOT/'assets/resource/base/pipeline'
        self.nodes={n:v for p in self.base.glob('*.json') for n,v in json.loads(p.read_text(encoding='utf8')).items()}
        self.common=json.loads((self.base/'bounty_common.json').read_text(encoding='utf8'))
        self.tasks=[t for t in task_catalog(ROOT,bindings={}) if t['name'].startswith('悬赏（')]

    def test_all_profiles_use_one_playback_and_preserve_binding_ids_and_quotas(self):
        self.assertEqual(len(self.tasks),4)
        self.assertEqual(len({t['id'] for t in self.tasks}),4)
        for task in self.tasks:
            self.assertEqual(task['playback_nodes'],['Bounty_Play'])
            profile=task['management_profile']
            self.assertIsNotNone(profile)
            self.assertEqual(profile['resume_entry'],'GameManagement_Resume')
            parameters=profile['parameters']
            self.assertEqual(parameters['navigation']['target_node'],'Bounty_Target')
            self.assertEqual(profile['entry'],profile['success_node'])
            self.assertIn('round_flow',parameters)
            self.assertEqual(parameters['file'],'@task:'+task['id'])
            wrapper=json.loads((self.base/(task['name']+'.json')).read_text(encoding='utf8'))
            self.assertLessEqual(len(wrapper),3)
            self.assertFalse(any('-恢复' in name or '-定位' in name for name in wrapper))
        candle=next(t for t in self.tasks if '宝烛' in t['name'])
        self.assertEqual(candle['id'],'bounty-candle')
        self.assertFalse(candle['management_profile']['native_repeat'])
        for task in self.tasks:
            if task is not candle:self.assertTrue(task['management_profile']['native_repeat'])

    def test_resources_are_complete_and_only_confirmed_map_finishes(self):
        def assets(v):
            if isinstance(v,dict):
                for k,x in v.items():
                    if k=='template':
                        for name in [x] if isinstance(x,str) else x:
                            self.assertTrue((ROOT/'assets/resource/base/image'/name).is_file(),name)
                    else:assets(x)
            elif isinstance(v,list):
                for x in v:assets(x)
        for name,definition in self.common.items():
            for link in node_links(self.nodes,definition):self.assertIn(link,self.nodes,name)
            assets(definition)
        self.assertLessEqual(len(self.common),8)
        self.assertEqual(self.common['Bounty_Play']['recognition']['param']['all_of'],['Nav_InStage'])
        self.assertFalse(any(n in self.common for n in ('Bounty_Navigate','Bounty_AfterPlay','Bounty_Finish')))

    def test_every_script_profile_preserves_blockers_and_unknown_dialog_handoff(self):
        from task_catalog import custom_parameters
        profiles=[custom_parameters(n) for n in self.nodes.values()
                  if custom_parameters(n).get('phase')=='begin']
        self.assertEqual(len(profiles),11+62)
        for params in profiles:
            self.assertEqual(params['result_guard']['error_next'],['Result_MonitorStopped'])
        self.assertEqual(custom_parameters(self.nodes['Result_MonitorStopped']),
                         dict(phase='fatal',reason='battle_monitor_stopped'))
        self.assertEqual(custom_parameters(self.nodes['Result_MonitorUnrecognizedDialog']),
                         dict(phase='unrecognized_dialog'))

    def test_generator_rebuilds_without_old_task_chains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            paths=['assets/interface.json','tools/bounty_task_template.json','assets/resource/base/pipeline/battle_outcomes.json']
            paths += ['assets/resource/base/pipeline/'+t['name']+'.json' for t in self.tasks]
            for rel in paths:
                out=root/rel;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes((ROOT/rel).read_bytes())
            build(root)
            for rel in paths[3:]+['assets/resource/base/pipeline/bounty_common.json']:
                self.assertEqual(json.loads((ROOT/rel).read_text(encoding='utf8')),json.loads((root/rel).read_text(encoding='utf8')))


if __name__=='__main__':unittest.main()
