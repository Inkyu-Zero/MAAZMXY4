"""Native task installation touches only owned task/resource paths."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('task_asset_sync', ROOT / 'agent/task_asset_sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


TASK_NAME = '测试任务'
ENTRY = 'NewEntry'
OLD_ENTRY = 'OldEntry'
PIPELINE_FILE = 'test.json'

class TaskAssetSyncTests(unittest.TestCase):
    def test_runtime_navigation_dependencies_and_library_are_installed_atomically(self):
        path = self.source/('assets/resource/base/pipeline/'+PIPELINE_FILE)
        pipeline = self.read(path)
        pipeline[ENTRY]['action'] = dict(type='Custom', param=dict(custom_action='script_task_state',
            custom_action_param=dict(phase='begin', navigation=dict(kind='stage'))))
        self.write(path, pipeline)
        self.write(self.source/'agent/data/navigation/resources.json', dict(version=1, pipelines=['navigation.json','outcomes.json']))
        self.write(self.source/'agent/data/navigation/routines.json', dict(version=1, targets=['map']))
        self.write(self.source/'assets/resource/base/pipeline/navigation.json', {'Nav':dict(next=['Shared'])})
        self.write(self.source/'assets/resource/base/pipeline/outcomes.json', {'Shared':dict(action=dict(type='DoNothing'))})
        raw = b'library-only crop'
        self.bytes(self.source/'assets/resource/base/image/navigation/unused-in-current-graph.png', raw)
        self.write(self.source/'agent/data/navigation/library.json', dict(version=1, assets=[dict(
            path='navigation/unused-in-current-graph.png', sha256=hashlib.sha256(raw).hexdigest())]))
        sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertTrue((self.target/'resource/base/pipeline/navigation.json').is_file())
        self.assertTrue((self.target/'resource/base/pipeline/outcomes.json').is_file())
        self.assertEqual((self.target/'resource/base/image/navigation/unused-in-current-graph.png').read_bytes(),raw)
        self.assertTrue((self.target/'agent/data/navigation/routines.json').is_file())
        self.assertEqual(sync.sync_task_assets(self.source,self.target)['changes'], [])

    def test_duplicate_pipeline_node_is_rejected_before_writing(self):
        self.write(self.source/'assets/resource/base/pipeline/duplicate.json', {ENTRY:dict(next=[])})
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '重复声明'):
            sync.sync_task_assets(self.source,self.target,dry_run=False)
        self.assertEqual(before, self.snapshot())

    def test_installed_client_layout_can_migrate_an_old_install_without_assets(self):
        self.prepare_legacy_recording()
        shutil.copytree(self.source/'assets/resource',self.source/'resource')
        self.write(self.source/'interface.json',self.read(self.source/'assets/interface.json'))
        (self.source/'assets/interface.json').unlink()
        result=sync.sync_task_assets(self.source,self.target,dry_run=False)
        self.assertEqual(len(result['migrated_bindings']),1)
        self.assertEqual(self.read(self.target/'config/instances/default.json')['TaskItems'][-1]['repeat_count'],999)

    def test_complete_manual_upgrade_updates_version_without_replacing_controller_settings(self):
        source=self.read(self.source/'assets/interface.json')
        source['version']='2.2.1'
        self.write(self.source/'assets/interface.json',source)
        sync.sync_task_assets(self.source,self.target,dry_run=False,update_version=True)
        installed=self.read(self.target/'interface.json')
        self.assertEqual(installed['version'],'2.2.1')
        self.assertEqual(installed['controller'],self.before_interface['controller'])

    def test_attributed_cross_file_links_install_their_dependencies(self):
        for link in ['[JumpBack]External',dict(name='External',jump_back=True)]:
            with self.subTest(link=link):
                self.write(self.source/('assets/resource/base/pipeline/'+PIPELINE_FILE), {ENTRY:dict(next=[link])})
                self.write(self.source/'assets/resource/base/pipeline/external.json', {'External':dict(action=dict(type='DoNothing'))})
                sync.sync_task_assets(self.source,self.target,dry_run=False)
                self.assertTrue((self.target/'resource/base/pipeline/external.json').is_file())

    def prepare_legacy_recording(self):
        self.before_interface['option']['回放文件'] = dict(cases=[dict(name='manual'),
            dict(name='mine', pipeline_override={'OldFight': {'action': {'param': {'custom_action_param': {'file': '战斗.json'}}}}})])
        self.write(self.target/'interface.json',self.before_interface)
        self.write(self.target/'resource/base/pipeline/old.json', {
            OLD_ENTRY: dict(next=['OldFight']), 'OldFight': dict(action=dict(type='Custom',param=dict(custom_action='battle_play')))})
        self.write(self.source/('assets/resource/base/pipeline/'+PIPELINE_FILE), {
            ENTRY:dict(next=['Fight']), 'Fight':dict(action=dict(type='Custom',param=dict(custom_action='battle_play')))})
        self.write(self.target/'recordings/战斗.json',dict(version=1,name='战斗',base=[1148,720],duration_ms=100,
            actions=[dict(t=0,op='keydown',key=65),dict(t=100,op='keyup',key=65)]))

    def test_legacy_script_selection_is_included_in_same_backup_transaction(self):
        self.prepare_legacy_recording()
        original = (self.target/'recordings/战斗.json').read_bytes()
        result = sync.sync_task_assets(self.source,self.target,dry_run=False)
        self.assertEqual(len(result['migrated_bindings']),1)
        bindings=self.read(self.target/'recordings/.bindings.json')['tasks']
        self.assertEqual(next(iter(bindings.values())),dict(file='战斗.json',mode='pipeline'))
        self.assertEqual((self.target/'recordings/战斗.json').read_bytes(),original)
        instance=self.read(self.target/'config/instances/default.json')['TaskItems'][-1]
        self.assertEqual(instance['repeat_count'],999)
        self.assertTrue(instance['default_check'])

    def test_failed_binding_migration_rolls_back_task_and_instance_changes(self):
        self.prepare_legacy_recording()
        before=self.snapshot()
        original=sync._publish
        def fail_binding(path,raw,expected):
            if path.name=='.bindings.json': raise OSError('simulated binding write failure')
            return original(path,raw,expected)
        with patch.object(sync,'_publish',side_effect=fail_binding), self.assertRaises(OSError):
            sync.sync_task_assets(self.source,self.target,dry_run=False)
        after={key:value for key,value in self.snapshot().items() if not key.startswith('backup/')}
        self.assertEqual(after,before)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.task = dict(name=TASK_NAME, entry=ENTRY, option=['回放来源', '自动恢复'],
                         repeatable=True, repeat_count=1, description='原生说明')
        self.options = {
            '回放来源': dict(type='select', default_case='绑定脚本', cases=[dict(name='绑定脚本'), dict(name='通用脚本')]),
            '自动恢复': dict(type='select', default_case='关闭', cases=[dict(name='关闭'), dict(name='启用')]),
        }
        self.write(self.source / 'assets/interface.json', dict(task=[deepcopy(self.task)], option=deepcopy(self.options)))
        self.write(self.source / ('assets/resource/base/pipeline/' + PIPELINE_FILE), {
            ENTRY: dict(next=['完成'], recognition=dict(type='TemplateMatch', param=dict(template='task-fixtures/card.png'))),
            '完成': dict(action=dict(type='DoNothing'))})
        self.bytes(self.source / 'assets/resource/base/image/task-fixtures/card.png', b'card image')
        self.before_interface = dict(
            task=[dict(name='其它任务', entry='Other', custom=12), dict(name=TASK_NAME, entry=OLD_ENTRY, option=['刷取次数'])],
            option={'其它选项': {'untouched': True}, '刷取次数': {'old': True}},
            resource=[dict(name='默认', path=['resource/base'])], controller=[dict(name='Win32', win32={'keyboard': 'SendMessage'})],
            custom={'keep': [1, 2]})
        self.write(self.target / 'interface.json', self.before_interface)
        self.old_item = dict(name=TASK_NAME, entry=OLD_ENTRY, default_check=True,
                             option=[dict(name='刷取次数', index=0, data={'刷取次数': '999'}), dict(name='回放文件', index=1)],
                             user_extra={'keep': True})
        self.write(self.target / 'config/instances/default.json', dict(
            InstanceName='用户配置', TaskItems=[dict(name='其它任务', entry='Other', default_check=True), deepcopy(self.old_item)],
            CurrentTasks=['其它任务<|||>Other', TASK_NAME + '<|||>' + OLD_ENTRY], other={'keep': True}))
        self.bytes(self.target / 'recordings/战斗.json', b'user recording bytes')
        self.bytes(self.target / 'resource/base/model/ocr/rec.onnx', b'user model bytes')
        self.bytes(self.target / 'appsettings.json', b'{"user":true}')

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf8')

    @staticmethod
    def bytes(path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding='utf-8-sig'))

    def snapshot(self):
        return {path.relative_to(self.target).as_posix(): path.read_bytes() for path in self.target.rglob('*') if path.is_file()}

    def test_default_dry_run_has_zero_writes(self):
        before = self.snapshot()
        result = sync.sync_task_assets(self.source, self.target)
        self.assertFalse(result['applied'])
        self.assertTrue(result['changes'])
        self.assertEqual(self.snapshot(), before)

    def test_full_upgrade_retires_only_exact_owned_entries_and_unreferenced_options(self):
        retired = dict(name='录制入口', entry='RetiredEntry')
        custom = dict(name='录制入口', entry='CustomEntry', option=['保留来源'])
        self.write(self.source/'agent/task-migrations.json', dict(version=1, retired_tasks=[retired],
                   retired_options=['旧来源', '保留来源']))
        before = self.read(self.target/'interface.json')
        before['task'] += [retired, custom]
        before['option'].update({'旧来源': {}, '保留来源': {}})
        self.write(self.target/'interface.json', before)
        config = self.read(self.target/'config/instances/default.json')
        config['TaskItems'] += [retired, custom]
        config['CurrentTasks'] += ['录制入口<|||>RetiredEntry', '录制入口<|||>CustomEntry']
        self.write(self.target/'config/instances/default.json', config)
        sync.sync_task_assets(self.source, self.target, dry_run=False)
        after = self.read(self.target/'interface.json')
        self.assertNotIn(retired, after['task'])
        self.assertIn(custom, after['task'])
        self.assertNotIn('旧来源', after['option'])
        self.assertIn('保留来源', after['option'])
        config = self.read(self.target/'config/instances/default.json')
        self.assertNotIn(retired, config['TaskItems'])
        self.assertIn(custom, config['TaskItems'])
        self.assertNotIn('录制入口<|||>RetiredEntry', config['CurrentTasks'])
        self.assertIn('录制入口<|||>CustomEntry', config['CurrentTasks'])

    def test_selected_task_upgrade_does_not_retire_other_tasks(self):
        retired = dict(name='录制入口', entry='RetiredEntry')
        self.write(self.source/'agent/task-migrations.json', dict(version=1, retired_tasks=[retired]))
        before = self.read(self.target/'interface.json')
        before['task'].append(retired)
        self.write(self.target/'interface.json', before)
        sync.sync_task_assets(self.source, self.target, dry_run=False, task_names=[TASK_NAME])
        self.assertIn(retired, self.read(self.target/'interface.json')['task'])

    def test_default_bundle_installs_without_overwriting_user_library(self):
        raw = b'{"version":1,"name":"default"}'
        self.bytes(self.source/'agent/recordings/default.json', raw)
        self.write(self.source/'agent/recordings/manifest.json', dict(version=1, items=[
            dict(file='default.json', sha256=sync._sha(raw))]))
        self.bytes(self.target/'recordings/default.json', b'user edits')
        sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertEqual((self.target/'recordings/default.json').read_bytes(), b'user edits')
        self.assertEqual((self.target/'agent/recordings/default.json').read_bytes(), raw)
        self.assertFalse(sync.sync_task_assets(self.source, self.target)['changes'])

    def test_select_second_task_and_install_its_cross_file_dependencies(self):
        source = self.read(self.source/'assets/interface.json')
        source['task'].append(dict(name='第二任务',entry='Second',option=[]))
        self.write(self.source/'assets/interface.json',source)
        self.write(self.source/'assets/resource/base/pipeline/second.json', {'Second':dict(next=['Shared'])})
        self.write(self.source/'assets/resource/base/pipeline/shared.json', {'Shared':dict(action='DoNothing')})
        result=sync.sync_task_assets(self.source,self.target,dry_run=False,task_names=['第二任务'])
        changed={item['path'] for item in result['changes']}
        self.assertIn('resource/base/pipeline/second.json',changed)
        self.assertIn('resource/base/pipeline/shared.json',changed)
        self.assertNotIn('resource/base/pipeline/'+PIPELINE_FILE,changed)
        self.assertEqual(self.read(self.target/'config/instances/default.json')['TaskItems'][1],self.old_item)

    def test_renamed_options_preserve_nested_input_values_by_schema_labels(self):
        old = self.read(self.target/'interface.json')
        old['option']['旧恢复'] = dict(type='select',label='自动恢复',cases=[dict(name='关闭'),dict(name='启用',option=['旧设置'])])
        old['option']['旧设置'] = dict(type='input',label='恢复设置',inputs=[dict(name='旧路径',label='程序路径')])
        self.write(self.target/'interface.json',old)
        source = self.read(self.source/'assets/interface.json')
        source['option']['自动恢复'].update(label='自动恢复')
        source['option']['自动恢复']['cases'][1]['option']=['恢复设置']
        source['option']['恢复设置']=dict(type='input',label='恢复设置',inputs=[dict(name='程序',label='程序路径')])
        self.write(self.source/'assets/interface.json',source)
        config = self.read(self.target/'config/instances/default.json')
        config['TaskItems'][1]['option']=[dict(name='旧恢复',index=1,sub_options=[dict(name='旧设置',data={'旧路径':'C:/game.exe'})])]
        self.write(self.target/'config/instances/default.json',config)
        sync.sync_task_assets(self.source,self.target,dry_run=False)
        option=self.read(self.target/'config/instances/default.json')['TaskItems'][1]['option'][0]
        self.assertEqual(option,dict(name='自动恢复',index=1,sub_options=[dict(name='恢复设置',data={'程序':'C:/game.exe'})]))

    def test_owned_assets_install_and_unrelated_data_is_unchanged(self):
        before = self.snapshot()
        result = sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertTrue(result['applied'])
        self.assertTrue((self.target / result['backup'] / 'installation.json').is_file())
        interface = self.read(self.target / 'interface.json')
        self.assertEqual(interface['task'][0], self.before_interface['task'][0])
        self.assertEqual(interface['task'][1], self.task)
        for field in ('resource', 'controller', 'custom'):
            self.assertEqual(interface[field], self.before_interface[field])
        self.assertEqual(interface['option']['其它选项'], {'untouched': True})
        self.assertEqual(interface['option']['刷取次数'], {'old': True})
        for path in ('recordings/战斗.json', 'resource/base/model/ocr/rec.onnx', 'appsettings.json'):
            self.assertEqual((self.target / path).read_bytes(), before[path])

    def test_old_instance_keeps_checked_and_round_count_and_defaults_recovery_off(self):
        sync.sync_task_assets(self.source, self.target, dry_run=False)
        config = self.read(self.target / 'config/instances/default.json')
        task = config['TaskItems'][1]
        self.assertEqual(task['entry'], ENTRY)
        self.assertEqual(task['repeat_count'], 999)
        self.assertTrue(task['default_check'])
        self.assertEqual(task['user_extra'], {'keep': True})
        self.assertEqual(task['option'], [dict(name='回放来源', index=0), dict(name='自动恢复', index=0)])
        self.assertEqual(config['CurrentTasks'], ['其它任务<|||>Other', TASK_NAME + '<|||>' + ENTRY])
        self.assertEqual(config['other'], {'keep': True})

    def test_native_choices_and_count_survive_reinstallation_and_json_reformatting(self):
        sync.sync_task_assets(self.source, self.target, dry_run=False)
        path = self.target / 'config/instances/default.json'
        config = self.read(path)
        config['TaskItems'][1]['repeat_count'] = 17
        config['TaskItems'][1]['option'][0]['index'] = 1
        config['TaskItems'][1]['option'][1]['index'] = 1
        path.write_text(json.dumps(config, ensure_ascii=True, indent=4), encoding='utf-8-sig')
        interface = self.target / 'interface.json'
        interface.write_text(json.dumps(self.read(interface), sort_keys=True, indent=4), encoding='utf-8-sig')
        before = self.snapshot()
        result = sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertEqual(result['changes'], [])
        self.assertIsNone(result['backup'])
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_task_and_history_entries_are_merged_without_reenabling_deleted_task(self):
        path = self.target / 'config/instances/default.json'
        config = self.read(path)
        native = {**self.task, 'default_check': False, 'repeat_count': 7,
                  'option': [dict(name='回放来源', index=1), dict(name='自动恢复', index=0)]}
        config['TaskItems'].append(native)
        config['CurrentTasks'] += [TASK_NAME + '<|||>' + ENTRY, TASK_NAME + '<|||>' + OLD_ENTRY]
        self.write(path, config)
        deleted = self.target / 'config/instances/deleted.json'
        self.write(deleted, dict(TaskItems=[], CurrentTasks=[TASK_NAME + '<|||>' + OLD_ENTRY]))
        sync.sync_task_assets(self.source, self.target, dry_run=False)
        actual = self.read(path)
        tasks = [item for item in actual['TaskItems'] if item.get('name') == TASK_NAME]
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0]['default_check'])
        self.assertEqual(tasks[0]['repeat_count'], 7)
        self.assertEqual(actual['CurrentTasks'].count(TASK_NAME + '<|||>' + ENTRY), 1)
        self.assertEqual(self.read(deleted), dict(TaskItems=[], CurrentTasks=[TASK_NAME + '<|||>' + ENTRY]))

    def test_write_failure_rolls_back_only_installer_bytes(self):
        before = self.snapshot()
        original = sync._publish
        calls = []
        def fail_once(path, raw, expected):
            calls.append(str(path))
            if len(calls) == 2:
                raise OSError('simulated disk failure')
            return original(path, raw, expected)
        with patch.object(sync, '_publish', side_effect=fail_once):
            with self.assertRaises(OSError):
                sync.sync_task_assets(self.source, self.target, dry_run=False)
        after = {key: value for key, value in self.snapshot().items() if not key.startswith('backup/')}
        self.assertEqual(after, before)

    def test_precommit_configuration_change_is_detected_before_any_asset_write(self):
        original = sync._verify_inputs
        path = self.target / 'interface.json'
        external = {**self.before_interface, 'user_added_during_prepare': True}
        def change(inputs):
            self.write(path, external)
            return original(inputs)
        with patch.object(sync, '_verify_inputs', side_effect=change):
            with self.assertRaises(sync.SyncConflict):
                sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertEqual(self.read(path), external)
        self.assertFalse((self.target / 'resource/base/pipeline' / PIPELINE_FILE).exists())

    def test_rollback_preserves_a_file_edited_after_publication(self):
        original = sync._publish
        calls = []
        path = self.target / 'interface.json'
        external = {**self.before_interface, 'concurrent_user_change': True}
        def fail_once(destination, raw, expected):
            calls.append(destination)
            if len(calls) == 2:
                self.write(path, external)
                raise OSError('simulated next-file failure')
            return original(destination, raw, expected)
        with patch.object(sync, '_publish', side_effect=fail_once):
            with self.assertRaises(OSError):
                sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertEqual(self.read(path), external)

    def test_source_change_before_commit_is_rejected(self):
        original = sync._verify_inputs
        asset = self.source / 'assets/resource/base/image/task-fixtures/card.png'
        def change(inputs):
            asset.write_bytes(b'a newly edited source asset')
            return original(inputs)
        before = self.snapshot()
        with patch.object(sync, '_verify_inputs', side_effect=change):
            with self.assertRaises(sync.SyncConflict):
                sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertEqual(self.snapshot(), before)

    def test_missing_template_or_unknown_next_node_fails_without_writes(self):
        pipeline_path = self.source / 'assets/resource/base/pipeline' / PIPELINE_FILE
        source = self.read(pipeline_path)
        for node in (dict(recognition=dict(type='TemplateMatch', param=dict(template='task-fixtures/missing.png'))),
                     dict(next=['不存在节点'])):
            with self.subTest(node=node):
                self.write(pipeline_path, {ENTRY: node})
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    sync.sync_task_assets(self.source, self.target, dry_run=False)
                self.assertEqual(self.snapshot(), before)
        self.write(pipeline_path, source)

    def test_source_and_target_may_be_same_project(self):
        self.write(self.source / 'interface.json', self.before_interface)
        result = sync.sync_task_assets(self.source, self.source, dry_run=False)
        self.assertTrue(result['applied'])
        self.assertEqual(self.read(self.source / 'interface.json')['task'][1], self.task)
        self.assertEqual((self.source / 'resource/base/image/task-fixtures/card.png').read_bytes(), b'card image')

    def test_symlink_asset_directory_cannot_write_outside_target(self):
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.target / 'resource/base/image'
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Windows symlink privilege is not enabled')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            sync.sync_task_assets(self.source, self.target, dry_run=False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
