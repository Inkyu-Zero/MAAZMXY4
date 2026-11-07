"""Task discovery and execution bindings use existing interface/instance data."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from script_studio_service import StudioService
from script_library import read_binding_records, unbind_task_recording
from task_catalog import execution_plan, task_catalog, task_key


class TaskCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.write('interface.json', dict(task=[dict(name='挑战甲',entry='A'),dict(name='挑战乙',entry='B')],resource=[dict(path=['resource/base'])]))
        self.write('resource/base/pipeline/task.json', {
            'A':dict(next=['Fight']), 'Fight':dict(action=dict(type='Custom',param=dict(custom_action='battle_play', custom_action_param=dict(file='@task:'+task_key('挑战甲','A'))))),
            'B':dict(action='Click')})
        self.write('recordings/test.json',dict(version=1,name='测试',base=[1148,720],duration_ms=200,
                    actions=[dict(t=0,op='keydown',key=65),dict(t=200,op='keyup',key=65)]))
        self.service=StudioService(self.root)

    def tearDown(self):
        self.service.dirty=False
        self.service.close()
        self.temp.cleanup()

    def write(self, path, data):
        destination=self.root/path
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')

    def bind(self,name,entry,mode):
        self.service.dispatch('load',dict(file='test.json'))
        return self.service.dispatch('bind_task',dict(task_id=task_key(name,entry),mode=mode))

    def test_each_task_can_bind_independently_with_explicit_capabilities(self):
        tasks=task_catalog(self.root)
        self.assertEqual(tasks[0]['modes'],['pipeline','replace'])
        self.assertEqual(tasks[1]['modes'],['replace'])
        self.bind('挑战甲','A','pipeline');self.bind('挑战乙','B','replace')
        first=execution_plan(self.root,'挑战甲','A');second=execution_plan(self.root,'挑战乙','B')
        self.assertEqual(first['playback_nodes'],['Fight'])
        self.assertEqual(second['binding']['mode'],'replace')
        self.assertTrue(first['handled'] and second['handled'])
        self.assertEqual(first['recording']['sha256'],hashlib.sha256((self.root/'recordings/test.json').read_bytes()).hexdigest())
        self.assertEqual(len(read_binding_records(self.root/'recordings')),2)

    def test_saved_custom_tasks_and_imported_definitions_are_discovered(self):
        self.write('interface.json',dict(task=[],import_=[]))
        self.write('interface.json',{'task':[],'import':'extra.json'})
        self.write('extra.json',dict(task=[dict(name='附加',entry='Extra')]))
        self.write('config/instances/test.json',dict(TaskItems=[dict(name='自定义',entry='Custom')]))
        self.assertEqual([t['name'] for t in task_catalog(self.root)],['附加','自定义'])
        self.bind('自定义','Custom','replace')
        self.assertTrue(execution_plan(self.root,'自定义','Custom')['handled'])

    def test_binding_does_not_depend_on_library_editor_session(self):
        self.bind('挑战乙','B','replace')
        query=StudioService(self.root)
        try:
            self.assertTrue(query.dispatch('execution_plan',dict(name='挑战乙',entry='B'))['handled'])
            self.assertIsNone(query.document)
        finally: query.close()

    def test_unbind_preserves_other_tasks_and_checks_expected_file(self):
        self.bind('挑战甲','A','pipeline');self.bind('挑战乙','B','replace')
        ident=task_key('挑战甲','A')
        with self.assertRaisesRegex(ValueError,'修改'):
            unbind_task_recording(self.root/'recordings',ident,expected_file='different.json')
        self.service.dispatch('unbind_task',dict(task_id=ident,expected_file='test.json'))
        with self.assertRaisesRegex(ValueError, '绑定脚本'):
            execution_plan(self.root,'挑战甲','A')
        self.assertTrue(execution_plan(self.root,'挑战乙','B')['handled'])

    def test_unknown_tasks_and_unsupported_modes_cannot_be_bound(self):
        with self.assertRaises(ValueError): self.bind('挑战乙','B','pipeline')
        with self.assertRaises(ValueError): self.bind('不存在','X','replace')
        self.assertEqual(execution_plan(self.root,'不存在','X'),dict(handled=False))

    def test_unbound_task_can_report_a_normal_stop_to_the_frontend(self):
        plan=self.service.dispatch('execution_plan',dict(name='挑战甲',entry='A',report_unbound=True))
        self.assertEqual(plan['stop_reason'],'unbound_script')
        self.assertTrue(plan['handled'])
        self.assertIn('尚未绑定脚本',plan['message'])
        self.assertNotIn('recording',plan)
        self.assertFalse((self.root/'recordings/.bindings.json').exists())

    def test_missing_bound_file_is_not_reported_as_an_unbound_stop(self):
        self.bind('挑战甲','A','pipeline')
        (self.root/'recordings/test.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.service.dispatch('execution_plan',dict(name='挑战甲',entry='A',report_unbound=True))

    def test_deleted_or_invalid_bound_file_stops_execution(self):
        self.bind('挑战乙','B','replace')
        (self.root/'recordings/test.json').unlink()
        with self.assertRaises(FileNotFoundError): execution_plan(self.root,'挑战乙','B')

    def test_legacy_index_is_read_without_rewriting_or_losing_other_bindings(self):
        ident=task_key('挑战甲','A')
        self.write('recordings/.bindings.json',dict(version=1,tasks={ident:'test.json'}))
        before=(self.root/'recordings/.bindings.json').read_bytes()
        self.assertEqual(execution_plan(self.root,'挑战甲','A')['binding']['mode'],'pipeline')
        self.assertEqual((self.root/'recordings/.bindings.json').read_bytes(),before)
        self.bind('挑战乙','B','replace')
        self.assertEqual(len(read_binding_records(self.root/'recordings')),2)
