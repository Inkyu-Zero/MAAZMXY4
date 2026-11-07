"""Resource integrity and build-time reuse, with no shortest-path planner."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'agent')]
from build_navigation import compile_library
from navigation.catalog import task_routine


class RoutineResourceTests(unittest.TestCase):
    def test_compiler_is_reproducible_and_has_one_action_per_native_node(self):
        bank=json.loads((ROOT/'tools/navigation_library.json').read_text(encoding='utf8'))
        compiled=compile_library(bank)
        self.assertEqual(compiled,json.loads((ROOT/'assets/resource/base/pipeline/navigation.json').read_text(encoding='utf8')))
        self.assertLess(len(compiled),350)
        self.assertFalse(any(n.startswith('Nav_Edge_') for n in compiled))
        for name in bank['actions']:
            self.assertFalse(compiled[name]['next'])
            self.assertEqual(compiled[name]['recognition']['type'],'And')
    def test_authored_routines_reference_loaded_native_definitions(self):
        routines=json.loads((ROOT/'agent/data/navigation/routines.json').read_text(encoding='utf8'))
        nodes={n:v for p in (ROOT/'assets/resource/base/pipeline').glob('*.json') for n,v in json.loads(p.read_text(encoding='utf8')).items()}
        for s in routines['screens'].values():
            self.assertIn(s['detector'],nodes)
            if s.get('exit'):self.assertIn(s['exit'],routines['actions'])
        for name,a in routines['actions'].items():
            if name!='Nav_ChooseBounty':self.assertIn(name,nodes)
            for source in a['from']:self.assertIn(source,routines['screens'])
            for target in a['verify']:self.assertTrue(target in routines['screens'] or target=='task_destination')
        for route in routines['routes'].values():
            for action in route:self.assertIn(action,routines['actions'])
        self.assertEqual(len(routines['stages']),7+62)
        self.assertIn('nanzhan',routines['screens'])
        self.assertIn('world',routines['screens'])
        self.assertTrue(routines['actions']['Result_DeclineRevive']['wait_for_clear'])
        self.assertGreaterEqual(routines['actions']['Result_DeclineRevive']['timeout_ms'],15000)
    def test_task_binding_references_original_node_and_keeps_profiles_independent(self):
        class Context:
            def get_node_data(self,_):return dict(recognition=dict(type='And',param={'normalized':'not source syntax'}))
            def override_pipeline(self,p):self.patch=p;return True
        context=Context()
        a=task_routine(context,dict(navigation=dict(kind='stage',target_node='target')))
        self.assertEqual(context.patch['Nav_TaskTarget']['recognition']['param']['all_of'],['target'])
        self.assertEqual(a['screens']['task_destination']['detector'],'target')
        a['screens']['map']['label']='changed'
        b=task_routine(context,{})
        self.assertNotEqual(b['screens']['map']['label'],'changed')
    def test_planner_is_absent_from_runtime_and_package_inputs(self):
        self.assertFalse((ROOT/'agent/navigation/graph.py').exists())
        self.assertFalse((ROOT/'agent/data/navigation/graph.json').exists())
        for p in (ROOT/'agent/navigation').glob('*.py'):
            text=p.read_text(encoding='utf8')
            self.assertNotIn('shortest_path',text)
            self.assertNotIn('import heapq',text)

    def test_bounty_title_and_known_aliases_control_both_row_and_detail(self):
        class Context:
            def get_node_data(self,_):return dict(recognition=dict(type='TemplateMatch'))
            def override_pipeline(self,value):self.patch=value;return True
        context=Context()
        task_routine(context,dict(navigation=dict(kind='bounty',title='遁地大盗',
            title_aliases=['捉拿大盗'],target_node='target')))
        parts=context.patch['Nav_ChooseBounty']['recognition']['param']['all_of']
        self.assertEqual(parts[0]['recognition']['param']['expected'],['^遁地大盗$','^捉拿大盗$'])
        detail=context.patch['Nav_BountyDetailTitle']['recognition']
        self.assertEqual(detail['type'],'And')
        self.assertTrue(all(c['recognition']['param']['roi']==[510,228,430,94] for c in detail['param']['all_of']))
        self.assertIn('Nav_AcceptedBounties',parts)
        self.assertIn('Result_NoRevivePrompt',parts)
        for config in ({'title':'','target_node':'target'},
                       {'title':'遁地大盗','title_aliases':'捉拿大盗','target_node':'target'}):
            with self.assertRaises(ValueError):task_routine(context,dict(navigation=dict(kind='bounty',**config)))


if __name__=='__main__':unittest.main()
