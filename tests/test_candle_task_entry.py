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

    def test_uses_the_existing_quest_panel_route(self):
        p = PREFIX
        self.assertEqual(self.nodes[p + '初始化']['next'], [p + '通用导航'])
        navigation = custom_parameters(self.nodes[p + '初始化'])['navigation']
        self.assertEqual(navigation['kind'], 'bounty')
        self.assertEqual(navigation['target_node'], p+'点击前往副本')
        self.assertEqual(custom_parameters(self.nodes[p+'通用导航'])['phase'], 'navigate')
        self.assertIn(p + '点击任务', self.nodes[p + '定位']['next'])
        for suffix, target in (('点击任务', '点击悬赏任务'),
                               ('点击悬赏任务', '点击前往副本'),
                               ('点击前往副本', '等待场内')):
            node = self.nodes[p + suffix]
            self.assertIn(p + target, node['next'])
            self.assertEqual(node['action']['type'], 'Click')
            self.assertEqual(node['on_error'], [p + ('通用导航' if suffix == '点击任务' else '失败')])
        for suffix in ('点击任务', '点击悬赏任务'):
            self.assertEqual(self.nodes[p + suffix]['recognition'],
                             self.taizong['悬赏（守卫太宗）-' + suffix]['recognition'])

    def test_no_retired_route_can_be_reached_or_used_as_a_resume_target(self):
        for name, node in self.nodes.items():
            self.assertFalse(any(term in name for term in RETIRED), name)
            for target in node_links(self.nodes, node):
                self.assertIn(target, self.nodes)
                self.assertFalse(any(term in target for term in RETIRED), target)
        parameters = custom_parameters(self.nodes[PREFIX + '初始化'])
        self.assertEqual(parameters['execution']['binding_id'], 'bounty-candle')
        self.assertEqual(parameters['task_binding_id'], 'bounty-candle')
        self.assertEqual(parameters['execution']['resume_entry'], PREFIX + '恢复登录')
        self.assertIn(PREFIX + '恢复地图就绪', self.nodes[PREFIX + '恢复等待进图']['next'])

    def test_return_and_recovery_use_the_normal_task_map(self):
        task_map = self.taizong['悬赏（守卫太宗）-点击任务']['recognition']
        for suffix in ('返回初检', '返回复检', '恢复地图就绪'):
            self.assertEqual(self.nodes[PREFIX + suffix]['recognition'], task_map)
        for suffix in ('恢复界面分流', '恢复等待进图'):
            self.assertEqual(self.nodes[PREFIX + suffix]['on_error'], [PREFIX + '恢复失败'])

    def test_playback_and_exit_keep_the_existing_round_contract(self):
        p = PREFIX
        parameters = custom_parameters(self.nodes[p + '初始化'])
        self.assertEqual(custom_parameters(self.nodes[p + '回放战斗'])['file'], '@task:bounty-candle')
        self.assertEqual(parameters['routes']['battle_only'], [p+'阻断提示',p+'拒绝还魂丹',p+'等待拒绝复活',p+'战斗失败',p+'成功返回地图',p+'点击设置'])
        self.assertEqual(parameters['routes']['recorded_return'], [p+'阻断提示',p+'拒绝还魂丹',p+'等待拒绝复活',p+'战斗失败',p+'成功返回地图',p+'等待返回'])
        self.assertEqual(self.nodes[p + '返回复检']['next'], [p + '成功'])
        self.assertEqual(custom_parameters(self.nodes[p + '成功'])['phase'], 'success')
        self.assertFalse(any(node.get('action', {}).get('param', {}).get('custom_action') == 'loop_count'
                             for node in self.nodes.values()))

    def test_open_settings_menu_stops_before_scene_confirmation_or_playback(self):
        p = PREFIX
        blocker = p + '场内设置中止'
        for branch in ('场内恢复', '场内初检'):
            self.assertLess(self.nodes[p + '等待场内']['next'].index(blocker),
                            self.nodes[p + '等待场内']['next'].index(p + branch))
        self.assertLess(self.nodes[p + '场内初检']['next'].index(blocker),
                        self.nodes[p + '场内初检']['next'].index(p + '回放战斗'))
        self.assertEqual(custom_parameters(self.nodes[blocker])['phase'], 'fatal')

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
                         [instance['TaskItems'][0]['option'][0],{'name':'挂机模式','index':0},{'name':'失败自动重试','index':0}])
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
