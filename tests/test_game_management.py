"""Managed login/round discovery without a window, process restart, or real input."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
import tempfile
from contextlib import ExitStack
from uuid import uuid4
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
sys.path.insert(0,str(ROOT/'tools'))
from task_catalog import task_catalog
from test_bounty_candle import runtime


def load_login():
    modules={name:ModuleType(name) for name in ('maa','maa.agent','maa.agent.agent_server','maa.custom_action')}
    modules['maa.agent.agent_server'].AgentServer=SimpleNamespace(custom_action=lambda _:lambda cls:cls)
    modules['maa.custom_action'].CustomAction=object
    modules['script_task']=runtime
    spec=importlib.util.spec_from_file_location('isolated_game_login',ROOT/'agent/game_management_login.py')
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,modules):spec.loader.exec_module(module)
    return module


login=load_login()


class GameManagementTests(unittest.TestCase):
    def test_all_eleven_recording_tasks_declare_a_single_round_and_actual_counter(self):
        tasks=task_catalog(ROOT,bindings={},overrides={'朱电（琉璃宫）-刷取次数':{'max_hit':37}})
        managed=[t for t in tasks if t['management_profile']]
        self.assertEqual(len(managed),11)
        stage=next(t for t in managed if t['name']=='朱电（琉璃宫）')['management_profile']
        self.assertTrue(stage['native_repeat']);self.assertEqual(stage['repeat_limit'],37)
        candle=next(t for t in managed if t['name']=='悬赏（守卫宝烛）')['management_profile']
        self.assertFalse(candle['native_repeat']);self.assertEqual(candle['repeat_limit'],1)
        adventure=next(t for t in tasks if t['entry']=='生存大冒险_初始化')
        self.assertTrue(adventure['management_completion_only'])
        self.assertIsNone(adventure['management_profile'])

    def scenario(self,states,**params):
        class Context:
            def __init__(self):
                self.index=0;self.actions=[];self.patches=[];self.now=0
                self.tasker=SimpleNamespace(stopping=False,controller=SimpleNamespace(post_screencap=lambda:
                    SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=True,get=lambda:SimpleNamespace(ndim=3,shape=(720,1147,3))))))
            def override_pipeline(self,p):self.patches.append(deepcopy(p));return True
            def run_recognition(self,name,frame):return SimpleNamespace(hit=name.removeprefix('GameManagement_') in states[self.index])
            def run_task(self,name,overrides):
                self.assert_wrapper=(name=='GameManagement_Invoke' and overrides[name]['action']['type']=='DoNothing')
                self.actions.append(overrides[name]['next'][0]);self.index=min(self.index+1,len(states)-1)
                return SimpleNamespace(status=SimpleNamespace(succeeded=True))
            def sleep(self,seconds):self.now+=seconds
        ctx=Context()
        values=dict(account_name='历史昵称',role_name='角色名',save_slot=1,**params)
        return ctx,lambda:login.restore(ctx,values,clock=lambda:ctx.now,sleep=ctx.sleep)

    def test_full_saved_account_login_is_guarded_and_checks_role_before_map(self):
        ctx,run=self.scenario([{'BoxLogin'},{'BoxLogin','SelectAccount'},{'BoxLogin','AccountSelected'},
                              {'StartGame'},{'SavePage','ChooseRole'},{'MapReady'}])
        run()
        self.assertTrue(ctx.assert_wrapper)
        self.assertEqual(ctx.actions,['GameManagement_'+s for s in ['OpenAccounts','SelectAccount','LoginBox','StartGame','ChooseRole']])

    def test_captcha_wrong_slot_and_unverified_map_never_click(self):
        for state in ({'Blocking','BoxLogin'},{'SavePage'},{'MapReady'}):
            ctx,run=self.scenario([state])
            with self.assertRaises(ValueError):run()
            self.assertEqual(ctx.actions,[])

    def test_explicitly_connecting_selected_map_does_not_relogin(self):
        ctx,run=self.scenario([{'MapReady'}],allow_existing_map=True);run();self.assertEqual(ctx.actions,[])

    def test_selected_role_waits_through_title_transition_without_starting_again(self):
        ctx,run=self.scenario([{'SavePage','ChooseRole'},{'StartGame'},{'MapReady'}])
        def sleep(seconds):
            ctx.now+=seconds
            if ctx.index==1 and ctx.now>=2:ctx.index=2
        ctx.sleep=sleep
        run()
        self.assertEqual(ctx.actions,['GameManagement_ChooseRole'])

    def test_scene_change_between_probe_and_action_is_rechecked(self):
        ctx,run=self.scenario([{'StartGame'},{'SavePage','ChooseRole'},{'MapReady'}])
        original=ctx.run_task
        def dispatch(name,overrides):
            if ctx.index==0:
                ctx.index=1
                return SimpleNamespace(status=SimpleNamespace(succeeded=False))
            return original(name,overrides)
        ctx.run_task=dispatch
        run()
        self.assertEqual(ctx.actions,['GameManagement_ChooseRole'])

    def test_selected_role_wait_is_cancellable_and_times_out_without_relogin(self):
        for cancel in (True,False):
            with self.subTest(cancel=cancel):
                ctx,run=self.scenario([{'SavePage','ChooseRole'},{'StartGame'}])
                def sleep(seconds):
                    ctx.now+=seconds
                    if cancel and ctx.now>=1:ctx.tasker.stopping=True
                ctx.sleep=sleep
                with patch.object(login,'white_frame',return_value=False), self.assertRaises(runtime.Cancelled if cancel else ValueError):run()
                self.assertEqual(ctx.actions,['GameManagement_ChooseRole'])

    def test_failed_role_click_cannot_authorize_an_existing_map(self):
        ctx,run=self.scenario([{'SavePage','ChooseRole'},{'MapReady'}])
        def dispatch(*args):
            ctx.index=1
            return SimpleNamespace(status=SimpleNamespace(succeeded=False))
        ctx.run_task=dispatch
        with self.assertRaises(ValueError):run()

    def test_repeated_failed_action_remains_bounded(self):
        ctx,run=self.scenario([{'StartGame'}]);calls=[]
        def dispatch(*args):
            calls.append(ctx.now)
            return SimpleNamespace(status=SimpleNamespace(succeeded=False))
        ctx.run_task=dispatch
        with self.assertRaisesRegex(ValueError,'恢复操作未确认'):run()
        self.assertEqual(len(calls),3)
        self.assertTrue(all(b-a>=2 for a,b in zip(calls,calls[1:])))

    def test_stop_and_missing_recognition_never_advance(self):
        ctx,run=self.scenario([{'BoxLogin'}]);ctx.tasker.stopping=True
        with self.assertRaises(runtime.Cancelled):run()
        self.assertEqual(ctx.actions,[])
        ctx,run=self.scenario([set()]);ctx.run_recognition=lambda *_:None
        with self.assertRaisesRegex(ValueError,'缺少恢复节点'):run()

    def test_account_and_role_are_literal_and_eight_slots_stay_in_their_rows(self):
        first=login.login_overrides('name.*','role(1)',1)
        third=login.login_overrides('name.*','role(1)',3)
        roi=lambda p:p['GameManagement_ChooseRole']['recognition']['param']['all_of'][1]['recognition']['param']
        import re
        pattern=roi(first)['expected'][0]
        self.assertRegex('role(1) Lv.72',pattern)
        self.assertIsNone(re.fullmatch(pattern,'role(1)其它 Lv.72'))
        self.assertLess(roi(first)['roi'][1],roi(third)['roi'][1])
        eighth=login.login_overrides('name','role',8)
        self.assertEqual(roi(eighth)['roi'][:2],[655,504])
        with self.assertRaises(ValueError):login.login_overrides('name','role',9)

    def test_scroll_fallback_is_independent_of_game_process_management(self):
        NavigationError = runtime.NavigationError
        recording = dict(path='bound.json', sha256='a'*64)
        state = dict(reference='@task:fixture', recording=recording,
                     parameters=dict(game_management=dict(restart_on_failure=False),
                                     navigation=dict(kind='stage', target_node='target')))
        for management in (None, {}, {'restart_on_failure': False}):
            state['parameters']['game_management'] = management
            navigator=SimpleNamespace(run=Mock(side_effect=[NavigationError('offscreen',reason='unreachable'),'task_destination']))
            with self.subTest(management=management), patch.object(runtime, '_check'), \
                 patch.object(runtime, 'inspect_recording', return_value=recording), \
                 patch.object(runtime, '_navigator', return_value=navigator), \
                 patch.object(runtime, 'wait_stage_ready', return_value=False), \
                 patch.object(runtime, 'restore_stage_view', return_value=True) as restore:
                self.assertEqual(runtime.navigate_round(None, state), 'task_destination')
                restore.assert_called_once_with(None,navigator,'target')

    def test_transient_stage_miss_uses_native_wait_before_any_map_drag(self):
        recording=dict(path='bound.json',sha256='a'*64)
        state=dict(reference='bound',recording=recording,parameters=dict(navigation=dict(kind='stage',target_node='target')))
        nav=SimpleNamespace(run=Mock(side_effect=[runtime.NavigationError('miss',reason='unreachable'),'task_destination']))
        with patch.object(runtime,'_check'),patch.object(runtime,'inspect_recording',return_value=recording), \
             patch.object(runtime,'_navigator',return_value=nav),patch.object(runtime,'wait_stage_ready',return_value=True) as wait, \
             patch.object(runtime,'restore_stage_view') as drag:
            self.assertEqual(runtime.navigate_round(None,state),'task_destination')
            wait.assert_called_once_with(None,nav,'target')
            drag.assert_not_called()

    def test_scroll_does_not_recover_non_stage_or_other_navigation_failures(self):
        recording = dict(path='bound.json', sha256='a'*64)
        for kind, reason in [('bounty', 'unreachable'), ('stage', 'unknown'),
                             ('stage', 'blocked'), ('stage', 'budget')]:
            state = dict(reference='bound', recording=recording,
                         parameters=dict(navigation=dict(kind=kind, target_node='target')))
            nav = SimpleNamespace(run=lambda: (_ for _ in ()).throw(runtime.NavigationError('stopped', reason=reason)))
            with self.subTest(kind=kind, reason=reason), patch.object(runtime, '_check'), \
                 patch.object(runtime, 'inspect_recording', return_value=recording), \
                 patch.object(runtime, '_navigator', return_value=nav), patch.object(runtime, 'restore_stage_view') as restore:
                with self.assertRaises(runtime.NavigationError): runtime.navigate_round(None, state)
                restore.assert_not_called()

    def test_release_collection_contains_management_and_no_test_frames(self):
        spec = importlib.util.spec_from_file_location('managed_package', ROOT/'tools/build_release.py')
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        files = {relative for _, relative in builder.collect(str(ROOT))}
        expected = {'agent/game_management_login.py', 'agent/navigation/stage_view.py',
                    'agent/data/navigation/game-management.json', 'agent/data/navigation/stage-scroll.json',
                    'resource/base/pipeline/game_management.json', 'resource/base/pipeline/game_management_scroll.json'}
        self.assertTrue(expected <= files, expected-files)
        self.assertFalse(any('tests/' in path or 'fixtures/' in path for path in files))
        data = json.loads((ROOT/'agent/data/navigation/stage-scroll.json').read_text(encoding='utf8'))
        self.assertTrue(all('resource/base/image/'+asset['path'] in files for asset in data['assets']))


class AdventureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scope=ExitStack();self.addCleanup(self.scope.close)
        directory=Path(self.scope.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.scope.enter_context(patch.object(runtime,'ROOT',directory))
        self.scope.enter_context(patch.object(runtime,'STATUS_DIRECTORY',directory/'debug/task-runs'))
        modules={name:ModuleType(name) for name in ('maa','maa.agent','maa.agent.agent_server','maa.custom_action','maa.context','maa.custom_recognition')}
        decorator=lambda _:lambda cls:cls
        modules['maa.agent.agent_server'].AgentServer=SimpleNamespace(custom_action=decorator,custom_recognition=decorator)
        modules['maa.custom_action'].CustomAction=object
        modules['maa.custom_recognition'].CustomRecognition=object
        modules['maa.context'].Context=object
        modules['script_task']=runtime
        self.scope.enter_context(patch.dict(sys.modules,modules))
        spec=importlib.util.spec_from_file_location('isolated_managed_adventure',ROOT/'agent/adventure.py')
        self.adventure=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.adventure)
        self.adventure._log=lambda *_:None;self.adventure._panel=lambda *_:None;self.adventure._hook=None
        run=str(uuid4());self.path=directory/'debug/task-runs'/(run+'.json')
        params=dict(management_checkpoint=dict(run_id=run,instance_id='test',status_path=str(self.path)))
        self.assertTrue(self.adventure.AdventureInit().run(None,SimpleNamespace(custom_action_param=json.dumps(params))))

    def test_only_recognized_adventure_result_confirms_the_boundary(self):
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8'))['status'],'running')
        self.adventure._log_result(None,'冒险成功 获得75积分','冒险成功')
        state=json.loads(self.path.read_text(encoding='utf8'))
        self.assertEqual((state['status'],state['reason']),('success','adventure_finished'))

    def test_stalled_adventure_reports_fatal_and_never_continues_ocr_or_input(self):
        self.adventure.STATE.last_progress_ts=1
        with patch.object(self.adventure.time,'monotonic',return_value=200), \
             patch.object(self.adventure,'_locate_options',side_effect=AssertionError('Must stop before reading/clicking')):
            self.assertIsNone(self.adventure.AdventureRead()._analyze(None,SimpleNamespace(image=None)))
            self.assertIsNone(self.adventure.AdventureRead()._analyze(None,SimpleNamespace(image=None)))
        state=json.loads(self.path.read_text(encoding='utf8'))
        self.assertEqual((state['status'],state['reason']),('fatal','adventure_stalled'))


if __name__=='__main__':unittest.main()
