"""Navigation packaging, stable task IDs and retry/quota integration."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'agent'))
sys.path.insert(0, str(ROOT/'tools'))
from navigation import select_state, validate_navigation
from navigation.catalog import task_graph
from task_catalog import custom_parameters, task_key


def read(path): return json.loads(path.read_text(encoding='utf8'))


class NavigationAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes, cls.owners = {}, {}
        for path in (ROOT/'assets/resource/base/pipeline').glob('*.json'):
            for name,node in read(path).items():
                if name in cls.nodes: raise AssertionError('Duplicate native node: '+name)
                cls.nodes[name], cls.owners[name] = node,path
        cls.interface = read(ROOT/'assets/interface.json')
        cls.graph = read(ROOT/'agent/data/navigation/graph.json')

    def test_complete_source_library_is_self_contained_with_original_provenance(self):
        library = read(ROOT/'agent/data/navigation/library.json')
        original = read(ROOT/'agent/data/navigation/source-catalog.json')
        privacy = read(ROOT/'tools/navigation_privacy_masks.json')
        self.assertEqual(library['privacy_masks'],{key:item['boxes'] for key,item in privacy['frames'].items()})
        self.assertEqual(library['source_node_count'],len(original['nodes']))
        self.assertEqual(library['source_edge_count'],len(original['edges']))
        self.assertEqual(library['source_probe_count'],len(original['probes']))
        self.assertEqual(library['base'],[1147,720])
        ids = {a['id'] for a in library['assets']}
        self.assertEqual(len(ids),len(library['assets']))
        self.assertTrue({p['reference'] for p in original['probes'].values() if 'reference' in p} <= ids)
        references={p['reference'] for p in original['probes'].values() if 'reference' in p}
        self.assertFalse(any(item['redacted'] and item['id'] in references for item in library['assets']))
        for item in library['assets']:
            path = (ROOT/'assets/resource/base/image'/item['path']).resolve()
            self.assertTrue(path.is_relative_to((ROOT/'assets/resource/base/image/navigation').resolve()))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),item['sha256'])
            self.assertIn(item['action_policy'],['recognize_only','candidate'])

    def test_compiled_graph_uses_only_readonly_probes_and_no_board_entry(self):
        validate_navigation(self.graph,self.nodes)
        self.assertFalse(any(e['target']=='bounty_board' for e in self.graph['edges']))
        self.assertNotIn('daily_menu_scene',self.nodes)
        for edge in self.graph['edges']:
            self.assertLessEqual(edge['max_attempts'],2)

    def test_task_destination_reuses_native_recognition_without_roundtrip_serialization(self):
        class Context:
            patches=[]
            def get_node_data(_self,name):
                # Maa's normalized composite format is not source JSON.
                return {'recognition':{'type':'And','param':{'all_of':[{'type':'OCR','sub_name':'OCR','param':{}}]}}}
            def override_pipeline(_self,patch):
                _self.patches.append(patch)
                return True
        context=Context()
        graph=task_graph(context,dict(navigation=dict(kind='bounty',title='守卫太宗',target_node='ActualTarget')))
        node=next(n for n in graph['nodes'] if n['id']=='task_destination')
        self.assertEqual(node['recognition'],['ActualTarget'])
        self.assertFalse(any('Nav_TaskDestination' in patch for patch in context.patches))
        for state in ('task_map','map_a','map_b'):
            self.assertTrue(any(e['source']==state and e['target']=='quest_panel' for e in graph['edges']))

    def test_every_injected_task_graph_can_locate_its_map_and_target(self):
        class Context:
            def get_node_data(_self, name): return self.nodes[name]
            def override_pipeline(_self, patch): return True
        tasks = 0
        for task in self.interface['task']:
            parameters = next((custom_parameters(node) for name, node in self.nodes.items()
                               if name.startswith(task['name'] + '-')
                               and custom_parameters(node).get('navigation')), None)
            if parameters is None: continue
            tasks += 1
            with self.subTest(task=task['name']):
                graph = task_graph(Context(), parameters)
                by_id = {node['id']: node for node in graph['nodes']}
                map_hits = set(by_id['task_map']['recognition'])
                self.assertEqual(select_state(graph, map_hits), 'task_map')
                target_hits = map_hits | set(by_id['task_destination']['recognition'])
                self.assertEqual(select_state(graph, target_hits), 'task_destination')
                if parameters['navigation']['kind'] == 'stage':
                    panel_hits = target_hits | set(by_id['profile']['recognition'])
                    self.assertEqual(select_state(graph, panel_hits), 'profile')
        self.assertEqual(tasks, 11)

    def test_every_script_task_preserves_binding_and_keeps_failure_out_of_quota(self):
        count=0
        for task in self.interface['task']:
            prefix=task['name']+'-'
            prepare=next((n for n in self.nodes if n.startswith(prefix)
                          and custom_parameters(self.nodes[n]).get('navigation')),None)
            if prepare is None: continue
            count+=1
            self.assertIn('失败自动重试',task['option'])
            parameters=custom_parameters(self.nodes[prepare])
            self.assertFalse(parameters['failure_retry']['enabled'])
            self.assertEqual(self.nodes[parameters['navigation_entry']]['next'],[parameters['navigation']['target_node']])
            self.assertEqual(self.nodes[prefix+'失败返回确认']['next'],[])
            confirmed_failure=parameters['result_guard']['failed_next'][0]
            self.assertEqual(self.nodes[confirmed_failure]['recognition']['type'],'DirectHit')
            self.assertIn(prefix+'失败返回确认',self.nodes[confirmed_failure]['next'])
            self.assertEqual(custom_parameters(self.nodes[confirmed_failure])['phase'],'battle_failure')
            for suffix in ('战斗失败','失败返回地图','失败返回确认','通用导航'):
                self.assertNotIn(prefix+'刷取次数', self.nodes[prefix+suffix].get('next',[]))
            if parameters.get('execution'):
                self.assertEqual(parameters['task_binding_id'],'bounty-candle')
                self.assertNotIn(prefix+'刷取次数',self.nodes)
            else:
                playback=custom_parameters(self.nodes[parameters['playback_node']])
                self.assertEqual(playback['file'],'@task:'+task_key(task['name'],task['entry']))
                self.assertEqual(self.nodes[prefix+'本轮成功']['next'],[prefix+'刷取次数','停止运行'])
                self.assertNotIn(self.nodes[task['entry']]['action']['type'],('Click','KeyDown','Key'))
                self.assertEqual(self.nodes[prefix+'刷取次数']['next'],[prepare])
        self.assertGreaterEqual(count,11)

    def test_standard_package_collection_includes_runtime_graph_and_images(self):
        spec=importlib.util.spec_from_file_location('navigation_update_collect',ROOT/'tools/build_release.py')
        builder=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        files={relative:Path(source) for source,relative in builder.collect(str(ROOT))}
        self.assertIn('agent/navigation/runtime.py',files)
        self.assertIn('agent/battle_outcomes.py',files)
        self.assertIn('agent/data/navigation/graph.json',files)
        self.assertIn('resource/base/pipeline/navigation.json',files)
        self.assertIn('resource/base/pipeline/battle_outcomes.json',files)
        for name in ('common_ui','settings'):
            self.assertIn('resource/base/pipeline/navigation_'+name+'.json',files)
        for name in ('common-ui','settings-ui'):
            data=read(ROOT/'agent/data/navigation'/f'{name}.json')
            for asset in data['assets']:
                self.assertIn('resource/base/image/'+asset['path'],files)
        self.assertFalse(any(name.startswith(('tests/','tools/')) or name.endswith('.swf') for name in files))
        self.assertIn('resource/base/image/navigation/outcomes/failed-return.png',files)
        self.assertFalse(any(Path(name).name.casefold()=='agents.md' for name in files))


if __name__=='__main__': unittest.main()
