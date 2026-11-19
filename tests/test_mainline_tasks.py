"""Complete, add-only stage inventory and shared lifecycle/resource contracts."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
sys.path.insert(0,str(ROOT/'tools'))
from mainline_tasks import names, LEGACY_STAGES, target_recognition
from task_catalog import task_catalog, task_key


def read(relative):return json.loads((ROOT/relative).read_text(encoding='utf-8'))


class MainlineTasksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stages=read('agent/data/navigation/mainline-stages.json')['stages']
        cls.scenes=read('agent/data/navigation/mainline-scenes.json')['stages']
        cls.interface=read('assets/interface.json')
        cls.nodes=read('assets/resource/base/pipeline/mainline.json')

    def test_all_six_maps_have_exact_complete_add_only_inventory(self):
        self.assertEqual([s['id'] for s in self.stages],list(range(1,66)))
        self.assertEqual([sum(s['town']==f'town{i}' for s in self.stages) for i in range(1,7)],[16,13,10,7,16,3])
        tasks=[t for t in self.interface['task'] if t['entry'].startswith('Mainline_')]
        self.assertEqual(len(tasks),65)
        for stage,task in zip(self.stages,tasks):
            self.assertEqual(task['entry'],names(stage)['entry'])
            self.assertIs(task['default_add'],False)
            self.assertEqual(task['group'],[stage['province']])
            self.assertEqual(task['option'],['挂机模式','盒子与脚本倍速','刷取次数','战斗录制内容','失败自动重试'])
        for task in self.interface['task']:
            if task['name'] in LEGACY_STAGES:
                stage=next(s for s in self.stages if s['id']==LEGACY_STAGES[task['name']])
                self.assertEqual(task['group'],[stage['province']])
                profile=read('tools/script_task_profiles.json')[task['name']]
                node=read('assets/resource/base/pipeline/'+task['name']+'.json')[profile['target_node']]
                self.assertEqual(node['recognition'],target_recognition(stage))
                self.assertEqual(task['entry'],profile['entry'])

    def test_each_stage_reuses_one_round_and_stable_independent_binding(self):
        catalog={task['entry']:task for task in task_catalog(ROOT,bindings={})}
        self.assertEqual(len(self.nodes),65*4)
        for stage in self.stages:
            spec=names(stage);task=catalog[spec['entry']]
            self.assertEqual(task['id'],task_key(spec['name'],spec['entry']))
            self.assertIsNone(task['binding'])
            self.assertEqual(task['playback_nodes'],[spec['play']])
            profile=task['management_profile'];params=profile['parameters']
            self.assertEqual(params['file'],'@task:'+task['id'])
            self.assertEqual(params['navigation']['kind'],'stage')
            self.assertEqual(params['round_flow']['scene_node'],'Nav_InStage')
            self.assertEqual(params['round_flow']['repeat_node'],spec['repeat'])
            self.assertEqual(self.interface['option']['刷取次数']['pipeline_override'][spec['repeat']],{'max_hit':'{刷取次数}'})

    def test_clicks_match_current_icon_with_province_normal_and_modal_guards(self):
        routines=read('agent/data/navigation/routines.json')
        for stage in self.stages:
            target=names(stage)['target'];node=self.nodes[target]
            parts=node['recognition']['param']['all_of']
            self.assertEqual(parts[0]['recognition'],stage['recognition'])
            expected = ['Nav_Province_'+stage['town']]
            if stage['town'] not in ('town1', 'town2'):
                expected.append('GameManagement_NormalMap')
            self.assertEqual(parts[1:], [*expected, 'Nav_ClearMap'])
            self.assertEqual(node['recognition']['param']['box_index'],0)
            self.assertEqual(node['action'],{'type':'LongPress','param':{'duration':100}})
            self.assertEqual(node['pre_delay'],0)
            self.assertEqual(routines['stages'][target],stage['town'])
            self.assertLess(stage['roi'][2]*stage['roi'][3]/(1147*720),0.05)

    def test_archived_scene_assets_keep_provenance_but_are_not_entry_guards(self):
        self.assertEqual([s['id'] for s in self.scenes],list(range(1,63)))
        for scene in self.scenes:
            self.assertFalse(scene['source']['missing_layers'])
            self.assertEqual(len(scene['checks']),2)
            for check in scene['checks']:
                self.assertLess(check['roi'][2]*check['roi'][3]/(1147*720),0.08)
                self.assertEqual(hashlib.sha256((ROOT/'assets/resource/base/image'/check['template']).read_bytes()).hexdigest(),check['sha256'])
        for stage in self.stages:
            for variant in stage['variants']:
                self.assertEqual(hashlib.sha256((ROOT/'assets/resource/base/image'/variant['template']).read_bytes()).hexdigest(),variant['sha256'])

    def test_blood_sea_reuses_bounded_endpoint_gestures_only(self):
        scroll={item['target']:item for item in read('agent/data/navigation/stage-scroll.json')['maps']}
        for stage in self.stages:
            target=names(stage)['target']
            if stage['town']=='town6':
                recipe=scroll[target]
                self.assertEqual((recipe['axis'],recipe['end_y'],recipe['max_steps']),('y',20,12))
                self.assertEqual(stage['offset_y'],2124)
                continue
            if stage['town']!='town5':self.assertNotIn(target,scroll);continue
            recipe=scroll[target];left=stage['endpoint']=='left'
            self.assertEqual(recipe['max_steps'],2)
            self.assertEqual(recipe['end_x'],1000 if left else 20)
            self.assertEqual(recipe['drag_node'],'GameManagement_town5_DragBack' if left else 'GameManagement_town5_Drag')


if __name__=='__main__':unittest.main()
