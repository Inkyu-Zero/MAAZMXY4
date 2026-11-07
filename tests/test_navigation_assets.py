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
from build_navigation import compile_library
from navigation.catalog import task_routine
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
        cls.routines = read(ROOT/'agent/data/navigation/routines.json')

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

    def test_routines_use_readonly_screens_and_do_not_enter_bounty_board(self):
        for screen in self.routines['screens'].values():
            self.assertEqual(self.nodes[screen['detector']]['action']['type'],'DoNothing')
        self.assertFalse(any('bounty_board' in a['verify'] for a in self.routines['actions'].values()))

    def test_each_public_task_binds_its_existing_native_target(self):
        class Context:
            def get_node_data(_self,name):return self.nodes[name]
            def override_pipeline(_self,patch):_self.patch=patch;return True
        count=0
        for task in self.interface['task']:
            parameters=next((custom_parameters(n) for name,n in self.nodes.items()
                if name.startswith(task['name']+'-') and custom_parameters(n).get('navigation')),None)
            if parameters is None:continue
            count+=1;context=Context();routine=task_routine(context,parameters)
            self.assertEqual(routine['targets'],['task_destination'])
            self.assertEqual(routine['screens']['map']['detector'],'Nav_ClearMap')
            parts=context.patch['Nav_TaskTarget']['recognition']['param']['all_of']
            self.assertIn(parameters['navigation']['target_node'],parts)
        self.assertEqual(count,11)

    def test_every_script_task_preserves_binding_and_declares_one_round_boundary(self):
        from task_catalog import task_catalog
        tasks=[t for t in task_catalog(ROOT,bindings={}) if t['management_profile']]
        self.assertEqual(len(tasks),11)
        for task in tasks:
            profile=task['management_profile'];params=profile['parameters']
            self.assertEqual(profile['entry'],profile['success_node'])
            self.assertEqual(params['file'],'@task:'+task['id'])
            self.assertFalse(params['failure_retry']['enabled'])
            self.assertIn(params['round_flow']['scene_node'],self.nodes)
            self.assertEqual(params['result_guard']['failed_next'],['Result_Failed'])
            self.assertEqual(params['result_guard']['revive_next'],['Result_RevivePrompt'])
            self.assertEqual(self.nodes[task['entry']]['action']['type'],'Custom')
            self.assertEqual(self.nodes[task['entry']]['next'],[task['entry'],'停止运行'] if profile['native_repeat'] else [])

    def test_standard_package_collection_includes_runtime_graph_and_images(self):
        spec=importlib.util.spec_from_file_location('navigation_update_collect',ROOT/'tools/build_release.py')
        builder=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        files={relative:Path(source) for source,relative in builder.collect(str(ROOT))}
        self.assertIn('agent/navigation/runtime.py',files)
        self.assertIn('agent/battle_outcomes.py',files)
        self.assertIn('agent/data/navigation/routines.json',files)
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
