"""Integration boundaries introduced by upstream 2.4.2; no OS input or app launch."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'agent')]
from build_release import collect
from navigation.catalog import task_graph
from task_catalog import custom_parameters


class UpstreamMergeTests(unittest.TestCase):
    def test_alchemy_code_templates_entry_and_default_scripts_are_packaged(self):
        names={name for _,name in collect(str(ROOT))}
        for name in ('agent/jzld_action.py','agent/jzld/board.py','agent/jzld/bot.py',
                     'agent/jzld/templates_small/tslag.png','resource/base/pipeline/九转炼丹.json',
                     'agent/recordings/manifest.json','agent/recordings/示例-移动与攻击.json'):
            self.assertIn(name,names)
        interface=json.loads((ROOT/'assets/interface.json').read_bytes())
        self.assertEqual(len(interface['task']),13)
        self.assertIn('九转炼丹-入口',[t['entry'] for t in interface['task']])
        self.assertFalse((ROOT/'updater.py').exists())
        self.assertFalse(list((ROOT/'update_engine').glob('*.py')))

    def test_renamed_bounty_navigation_accepts_exact_alias_without_dropping_guards(self):
        nodes={k:v for p in (ROOT/'assets/resource/base/pipeline').glob('*.json') for k,v in json.loads(p.read_bytes()).items()}
        class Context:
            def get_node_data(self,name):return nodes[name]
            def override_pipeline(self,values):nodes.update(values);return True
        params=custom_parameters(nodes['悬赏（遁地大盗）-准备本轮'])
        task_graph(Context(),params)
        parts=nodes['Nav_ChooseBounty']['recognition']['param']['all_of']
        expected=parts[0]['recognition']['param']['expected']
        self.assertIn('^遁地大盗$',expected)
        self.assertIn('^捉拿大盗$',expected)
        self.assertIn('Result_NoRevivePrompt',parts)

    def test_runtime_builder_includes_alchemy_imaging_dependency(self):
        import setup_runtime, runtime_check
        self.assertIn('PIL',setup_runtime.SITE_PACKAGES)
        self.assertIn('Lib/site-packages/PIL/_imaging.pyd',runtime_check.REQUIRED)

    def cursor_module(self):
        modules={name:ModuleType(name) for name in ('maa','maa.agent','maa.agent.agent_server','maa.custom_action')}
        modules['maa.agent.agent_server'].AgentServer=SimpleNamespace(custom_action=lambda _:lambda cls:cls)
        modules['maa.custom_action'].CustomAction=type('CustomAction',(),{'RunResult':staticmethod(lambda **kw:SimpleNamespace(**kw))})
        spec=importlib.util.spec_from_file_location('isolated_cursor',ROOT/'agent/cursor_action.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,modules):spec.loader.exec_module(module)
        return module

    def test_cursor_uses_only_explicit_handle_and_never_falls_back_to_another_window(self):
        cursor=self.cursor_module()
        argv=SimpleNamespace(custom_action_param='{"x":20,"y":30}')
        context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=SimpleNamespace(info={'type':'win32','hwnd':2**40+1})))
        with patch.object(cursor,'_post_move',return_value=(True,'synthetic')) as move:
            self.assertTrue(cursor.MoveCursor().run(context,argv).success)
            move.assert_called_once_with(2**40+1,20,30)
        for info in ({},{'hwnd':0},{'hwnd':True},{'hwnd':-1}):
            context.tasker.controller.info=info
            with patch.object(cursor,'_post_move') as move:
                self.assertFalse(cursor.MoveCursor().run(context,argv).success);move.assert_not_called()
        context.tasker.stopping=True
        with patch.object(cursor,'_post_move') as move:
            self.assertFalse(cursor.MoveCursor().run(context,argv).success);move.assert_not_called()


if __name__=='__main__':unittest.main()
