"""Regression contract for the normal quest-panel candle route, without a game."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))
from task_catalog import custom_parameters, execution_plan, node_links
from task_asset_sync import sync_task_assets

PREFIX = '悬赏（守卫宝烛）-'
RETIRED = ('须弥山', '洲地图', '世界地图', '悬赏榜', '目标卡片', '大厅')


class CandleTaskEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = ROOT / 'assets/resource/base/pipeline'
        cls.nodes = json.loads((folder / '悬赏（守卫宝烛）.json').read_text(encoding='utf8'))
        cls.taizong = json.loads((folder / '悬赏（守卫太宗）.json').read_text(encoding='utf8'))

    def test_uses_the_shared_quest_panel_route(self):
        params=custom_parameters(self.nodes[PREFIX+'初始化'])
        self.assertEqual(self.nodes[PREFIX+'初始化']['next'],[])
        self.assertEqual(params['round_flow']['scene_node'],'Nav_InStage')
        self.assertEqual(params['navigation']['target_node'],'Bounty_Target')
        self.assertEqual(params['task_binding_id'],'bounty-candle')
        self.assertEqual(params['execution']['resume_entry'],'GameManagement_Resume')
        self.assertEqual(set(self.nodes),{PREFIX+'初始化'})

    def test_shared_scene_keeps_pause_and_settings_resources(self):
        nodes=json.loads((ROOT/'assets/resource/base/pipeline/bounty_common.json').read_text(encoding='utf8'))
        self.assertEqual(nodes['Bounty_StageSettings']['recognition']['type'],'And')
        self.assertEqual(nodes['Bounty_StagePause']['recognition']['type'],'Or')
        self.assertEqual(nodes['Bounty_ResumeStage']['action']['type'],'Click')
        self.assertEqual(nodes['Bounty_Play']['recognition']['param']['all_of'],['Nav_InStage'])

    def test_existing_install_keeps_binding_and_options_when_the_route_is_replaced(self):
        interface = json.loads((ROOT / 'assets/interface.json').read_text(encoding='utf8'))
        name = '悬赏（守卫宝烛）'
        task = deepcopy(next(item for item in interface['task'] if item['name'] == name))
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve()
            def write(relative, value):
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
                return path
            write('interface.json', dict(task=[task], option=interface['option'], resource=interface['resource'],
                  controller=[{'name': 'personal controller'}]))
            write('resource/base/pipeline/悬赏（守卫宝烛）.json', {
                task['entry']: {'next': [PREFIX + '进入须弥山']},
                PREFIX + '进入须弥山': {'action': {'type': 'Click'}}})
            instance = dict(TaskItems=[dict(task, repeat_count=9, default_check=True,
                option=[{'name': '录制内容', 'index': 1}, {'name': '自动恢复', 'index': 0}])],
                CurrentTasks=[name + '<|||>' + task['entry']])
            write('config/instances/default.json', instance)
            script = write('recordings/personal.json', dict(version=1, name='personal',
                base=[1148, 720], duration_ms=1,
                actions=[dict(t=0, op='keydown', key=65), dict(t=1, op='keyup', key=65)]))
            bindings = write('recordings/.bindings.json', dict(version=2,
                tasks={'bounty-candle': dict(file='personal.json', mode='pipeline')}))
            before = {path: path.read_bytes() for path in (script, bindings)}
            result = sync_task_assets(ROOT, target, dry_run=False, task_names=[name])
            self.assertTrue(result['applied'])
            for path, raw in before.items(): self.assertEqual(path.read_bytes(), raw)
            saved = json.loads((target / 'config/instances/default.json').read_text(encoding='utf8'))
            self.assertEqual(saved['TaskItems'][0]['repeat_count'], 9)
            self.assertTrue(saved['TaskItems'][0]['default_check'])
            self.assertEqual(saved['TaskItems'][0]['option'],
                 [instance['TaskItems'][0]['option'][0],{'name':'挂机模式','index':0},
                  {'name':'盒子与脚本倍速','index':0},{'name':'失败自动重试','index':0}])
            archived=json.loads((target/result['backup']/'config/instances/default.json').read_text(encoding='utf8'))
            self.assertEqual(archived,instance)  # Retired restart settings remain recoverable in the migration backup.
            self.assertEqual(json.loads((target / 'interface.json').read_text(encoding='utf8'))['controller'],
                             [{'name': 'personal controller'}])
            installed = json.loads((target / 'resource/base/pipeline/悬赏（守卫宝烛）.json').read_text(encoding='utf8'))
            self.assertNotIn(PREFIX + '进入须弥山', installed)
            plan = execution_plan(target, name, task['entry'], report_unbound=True)
            self.assertNotIn('stop_reason', plan)
            self.assertEqual(plan['id'], 'bounty-candle')
            self.assertEqual(plan['recording']['file'], 'personal.json')


if __name__ == '__main__':
    unittest.main()
