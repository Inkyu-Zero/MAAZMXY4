"""Native task preflight and outcomes; no native library or game inputs."""
from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / 'agent'
sys.path.insert(0, str(AGENT))


def load_runtime():
    modules = {name: ModuleType(name) for name in ('maa', 'maa.agent', 'maa.agent.agent_server', 'maa.custom_action')}
    class Register:
        @staticmethod
        def custom_action(_name): return lambda implementation: implementation
    modules['maa.agent.agent_server'].AgentServer = Register
    modules['maa.custom_action'].CustomAction = object
    spec = importlib.util.spec_from_file_location('isolated_bounty_candle', AGENT/'script_task.py')
    runtime = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(runtime)
    return runtime


runtime = load_runtime()


class Frame:
    ndim = 3
    shape = (720, 1148, 3)


class Controller:
    def __init__(self):
        self.calls = 0
        self.frame = Frame()
        self.succeeded = True
    def post_screencap(self):
        self.calls += 1
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=self.succeeded, get=lambda: self.frame))


class Context:
    def __init__(self):
        self.tasker = SimpleNamespace(stopping=False, controller=Controller())
        self.patches = []
        self.reject = False
        self.nodes = {}
        for path in (ROOT/'assets/resource/base/pipeline').glob('*.json'):
            self.nodes.update(json.loads(path.read_text(encoding='utf8')))
        for name in ('Bounty_Finish', 'Bounty_AfterPlay', 'Bounty_Navigate', 'Bounty_FailedMap', PREFIX+'本轮成功'):
            self.nodes[name] = dict(next=[], action=dict(type='DoNothing'))
    def override_pipeline(self, value):
        self.patches.append(deepcopy(value))
        if not self.reject:
            def merge(target, patch):
                for key, item in patch.items():
                    if isinstance(item, dict) and isinstance(target.get(key), dict):
                        merge(target[key], item)
                    else:
                        target[key] = deepcopy(item)
            merge(self.nodes, value)
        return not self.reject
    def get_node_data(self, name):
        return deepcopy(self.nodes.get(name))
    def override_next(self, name, targets):
        return self.override_pipeline({name: {'next': targets}})
    def run_recognition(self, name, frame):
        if name not in self.nodes:
            return None
        # No desktop controller: the fixture remains on a positively known map.
        return SimpleNamespace(hit=name in ('Nav_ClearMap', 'Nav_TaskMap', 'Nav_Probe_hub_scene'))


PREFIX = '悬赏（守卫宝烛）-'
PIPELINE = json.loads((AGENT.parent/'assets/resource/base/pipeline/悬赏（守卫宝烛）.json').read_text(encoding='utf8'))

class BountyCandleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recordings = self.root/'recordings'
        self.recordings.mkdir()
        self.status = self.root/'debug/task-runs'
        self.patch = patch.multiple(runtime, ROOT=self.root, RECORDINGS=self.recordings, STATUS_DIRECTORY=self.status)
        self.patch.start()
        runtime._rounds.clear()
        self.context = Context()
        self.modal_patch = patch.dict(runtime.Navigator.locate.__globals__, {'modal_present': lambda c,f: bool(c.run_recognition('Nav_ModalPresent', f).hit)})
        self.modal_patch.start()
        self.addCleanup(self.modal_patch.stop)
        self.ident = str(uuid4())
        self.argv = SimpleNamespace(task_detail=SimpleNamespace(task_id=100))
        self.save_recording('one.json', 65)
        self.bind('one.json')

    def tearDown(self):
        runtime._rounds.clear()
        self.patch.stop()
        self.temp.cleanup()

    def bind(self, filename):
        (self.recordings/'.bindings.json').write_text(json.dumps({'version':1, 'tasks':{'bounty-candle':filename}}), encoding='utf8')

    def save_recording(self, filename, key, **fields):
        data = dict(version=1, name='测试录制', base=[1148,720], duration_ms=400,
                    actions=[dict(t=50,op='keydown',key=key),dict(t=250,op='keyup',key=key)], **fields)
        (self.recordings/filename).write_text(json.dumps(data,ensure_ascii=False), encoding='utf8')
        return data

    def run_phase(self, phase, **parameters):
        defaults = json.loads((ROOT/'tests/fixtures/legacy_script_task_parameters.json').read_text(encoding='utf8'))
        self.argv.custom_action_param = dict(defaults, **dict(phase=phase, run_id=self.ident, instance_id='default', **parameters))
        self.argv.node_name = 'Bounty_FailedMap'
        with redirect_stdout(io.StringIO()):
            return runtime.ScriptTaskState().run(self.context, self.argv)

    def outcome(self):
        return json.loads((self.status/(self.ident+'.json')).read_text(encoding='utf8'))

    def test_state_action_accepts_an_unrelated_task_route_and_playback_node(self):
        self.assertTrue(self.run_phase('begin',file='one.json',playback_node='OtherPlayback',
                        route_node='OtherRoute',exit_mode='finish',routes={'finish':['OtherExit']},
                        navigation=None,result_guard=None,completion_node=None))
        patch = self.context.patches[0]
        self.assertEqual(set(patch),{'OtherPlayback','OtherRoute'})
        self.assertEqual(patch['OtherRoute']['next'],['OtherExit'])
        self.assertEqual(Path(patch['OtherPlayback']['action']['param']['custom_action_param']['file']).name,'one.json')

    def test_begin_pins_current_saved_file_and_hash_in_the_native_pipeline(self):
        self.assertTrue(self.run_phase('begin'))
        playback = self.context.patches[0]['Bounty_Play']['action']['param']['custom_action_param']
        self.assertEqual(Path(playback['file']), self.recordings/'one.json')
        self.assertEqual(playback['expected_sha256'], hashlib.sha256((self.recordings/'one.json').read_bytes()).hexdigest())
        self.assertTrue(playback['strict_task'])
        self.assertEqual(playback['expected_base'], [1148,720])
        self.assertEqual(self.context.patches[0]['Bounty_AfterPlay']['next'],
                         ['Bounty_Blocked', 'Bounty_DeclineRevive', 'Bounty_WaitRevive', 'Bounty_Failed', 'Bounty_ReturnSucceeded', 'Bounty_MapReady'])
        self.assertEqual(self.outcome()['status'], 'running')
        self.assertEqual(self.outcome()['recording_sha256'],playback['expected_sha256'])

    def test_next_round_reads_saved_edit_without_any_recording_copy(self):
        self.assertTrue(self.run_phase('begin'))
        original = runtime._rounds[100]['recording']['sha256']
        self.assertTrue(self.run_phase('success'))
        self.save_recording('one.json', 66)
        self.ident = str(uuid4())
        self.context = Context()  # Maa overrides belong to one top-level task.
        self.assertTrue(self.run_phase('begin'))
        self.assertNotEqual(runtime._rounds[100]['recording']['sha256'], original)
        self.assertEqual(sorted(path.name for path in self.recordings.iterdir()), ['.bindings.json', 'one.json'])

    def test_battle_only_mode_uses_native_exit_and_full_mode_does_not(self):
        self.assertTrue(self.run_phase('begin',exit_mode='battle_only'))
        self.assertEqual(self.context.patches[0]['Bounty_AfterPlay']['next'],
                         ['Bounty_Blocked', 'Bounty_DeclineRevive', 'Bounty_WaitRevive', 'Bounty_Failed', 'Bounty_ReturnSucceeded', 'Bounty_MapReady', 'Bounty_ReturnSettings', 'Bounty_OpenSettings'])

    def test_playback_monitor_follows_effective_retry_setting_and_clears_old_overrides(self):
        settings=self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']
        for enabled,limit,afk,monitoring in ((True,3,False,True),(False,3,False,False),
                                            (True,0,False,False),(False,0,True,True),
                                            (False,3,False,False)):
            with self.subTest(enabled=enabled,limit=limit,afk=afk):
                settings.update(failure_retry=dict(enabled=enabled,max_retries=limit),afk_mode=afk)
                self.context.nodes['Bounty_Play']['next']=['Bounty_ConfirmedFailed']
                self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
                playback=self.context.nodes['Bounty_Play']
                guard=playback['action']['param']['custom_action_param'].get('result_guard')
                self.assertEqual(guard is not None,monitoring)
                self.assertEqual(playback['next'],['Bounty_AfterPlay'])
                self.assertEqual(self.outcome()['failure_retry']['enabled'],monitoring)

    def test_missing_binding_is_fatal_without_latest_fallback_or_capture(self):
        (self.recordings/'.bindings.json').unlink()
        self.assertFalse(self.run_phase('begin'))
        self.assertEqual(self.outcome()['status'],'fatal')
        self.assertEqual(self.context.tasker.controller.calls,0)
        self.assertFalse(runtime._rounds)

    def test_managed_defeat_without_in_game_retries_requests_restart(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['failure_retry'] = {
            'enabled': False, 'max_retries': 3}
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        self.assertFalse(self.run_phase('battle_failure'))
        self.assertEqual((self.outcome()['status'],self.outcome()['reason']),('recoverable','battle_failed'))
        self.assertFalse(runtime._rounds)

    def test_managed_restart_waits_for_all_in_game_retries(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['failure_retry'] = {
            'enabled': True, 'max_retries': 2}
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        for attempt in (1, 2):
            self.assertTrue(self.run_phase('battle_failure'))
            self.assertEqual(self.outcome()['reason'], 'battle_failed_returning')
            self.assertTrue(self.run_phase('retry_after_failure'))
            self.assertEqual(self.outcome()['failure_retries'], attempt)
            self.assertEqual(self.outcome()['status'], 'running')
        self.assertFalse(self.run_phase('battle_failure'))
        self.assertEqual((self.outcome()['status'], self.outcome()['reason']),
                         ('recoverable', 'battle_retry_exhausted'))
        self.assertEqual(self.outcome()['failure_retries'], 2)
        self.assertFalse(runtime._rounds)

    def test_afk_and_managed_restart_keep_retrying_in_game_without_restart(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['afk_mode'] = True
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        for attempt in range(1, 26):
            self.assertTrue(self.run_phase('battle_failure'))
            self.assertTrue(self.run_phase('retry_after_failure'))
            self.assertEqual(self.outcome()['status'], 'running')
            self.assertEqual(self.outcome()['failure_retries'], attempt)
        self.context.tasker.stopping = True
        self.assertFalse(self.run_phase('battle_failure'))
        self.assertEqual(self.outcome()['status'], 'cancelled')

    def test_confirmed_defeat_with_terminal_return_failure_requests_restart(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['afk_mode'] = True
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        self.assertTrue(self.run_phase('battle_failure'))
        self.context.run_recognition = lambda *_args: SimpleNamespace(hit=False)
        with patch.object(runtime, 'confirm_white_screen', return_value=False), patch.object(runtime.Navigator, '_settle'):
            self.assertFalse(self.run_phase('failure'))
        self.assertEqual((self.outcome()['status'], self.outcome()['reason']),
                         ('recoverable', 'battle_return_failed'))

    def return_timeout_context(self, *, succeeds_after=2, after_click=None):
        settings=self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']
        settings['afk_mode']=True
        self.assertTrue(self.run_phase('begin'))
        self.assertTrue(self.run_phase('battle_failure'))
        self.context.scene='failed';self.context.return_actions=[]
        def recognition(name, frame):
            hits={'failed':{'Result_Failed','Result_FailedReturn','Result_FailedClear'},'map':{'Nav_ClearMap','Nav_TaskMap','Nav_Probe_hub_scene'},
                  'modal':{'Result_Failed','Nav_CommonConfirm','Nav_ModalPresent'},'unknown':set()}
            return SimpleNamespace(hit=name in hits[self.context.scene],box=[600,500,200,80])
        def dispatch(entry, *, box):
            target=[entry]
            self.assertEqual(target,['Result_FailedReturn'])
            self.assertEqual(box,[600,500,200,80])
            self.context.return_actions.append(target)
            if after_click:after_click()
            elif len(self.context.return_actions)>=succeeds_after:self.context.scene='map'
            return SimpleNamespace(success=True)
        self.context.run_recognition=recognition
        self.context.run_action=dispatch

    def test_return_timeout_retries_missed_click_then_continues_same_round(self):
        self.return_timeout_context()
        runtime._rounds[100]['failure_retries']=5
        with patch.object(runtime.Navigator,'_settle'), patch.object(runtime,'confirm_white_screen',return_value=False):
            self.assertTrue(self.run_phase('failure'))
        self.assertEqual(len(self.context.return_actions),2)
        self.assertEqual(self.outcome()['reason'],'battle_retry')
        self.assertEqual(self.outcome()['failure_retries'],6)
        self.assertEqual(self.context.patches[-1],{'Bounty_FailedMap':{'next':['Bounty_Navigate']}})
        self.assertTrue(runtime._rounds)

    def test_return_timeout_is_bounded_and_never_changes_completed_count(self):
        self.return_timeout_context(succeeds_after=999)
        with patch.object(runtime.Navigator,'_settle'), patch.object(runtime,'confirm_white_screen',return_value=False):
            self.assertFalse(self.run_phase('failure'))
        self.assertEqual(len(self.context.return_actions),2)
        self.assertEqual(self.outcome()['failure_retries'],0)
        self.assertEqual(self.outcome()['status'],'fatal')

    def test_return_timeout_does_not_click_an_unknown_scene_or_foreground_modal(self):
        for scene in ('unknown','modal'):
            with self.subTest(scene=scene):
                self.return_timeout_context()
                self.context.scene=scene
                with patch.object(runtime.Navigator,'_settle'), patch.object(runtime,'confirm_white_screen',return_value=False):
                    self.assertFalse(self.run_phase('failure'))
                self.assertEqual(self.context.return_actions,[])

    def test_unrecognized_dialog_during_return_preserves_failure_reason(self):
        for restart in (False,True):
            self.context=Context();self.ident=str(uuid4())
            self.return_timeout_context()
            runtime._rounds[100]['parameters']['game_management']={'restart_on_failure':restart}
            self.context.scene='modal'
            with patch.object(runtime.Navigator,'_settle'), patch.object(runtime,'confirm_white_screen',return_value=False):
                self.assertFalse(self.run_phase('failure'))
            result=self.outcome()
            self.assertEqual(result['status'],'recoverable' if restart else 'fatal')
            self.assertEqual(result['reason'],'task_failed' if restart else 'unrecognized_dialog')
            if restart:self.assertEqual(result['navigation_failure'],'unrecognized_dialog')
            self.assertEqual(result['failure_retries'],0)
            self.assertEqual(self.context.return_actions,[])

    def test_return_timeout_checks_stop_and_recording_revision_before_more_input(self):
        self.return_timeout_context(after_click=lambda:setattr(self.context.tasker,'stopping',True))
        with patch.object(runtime.Navigator,'_settle'):
            self.assertFalse(self.run_phase('failure'))
        self.assertEqual(len(self.context.return_actions),1)
        self.assertEqual(self.outcome()['status'],'cancelled')
        self.context=Context()
        self.return_timeout_context()
        self.save_recording('one.json',67)
        self.assertFalse(self.run_phase('failure'))
        self.assertEqual(self.context.return_actions,[])
        self.assertEqual(self.outcome()['reason'],'recording_changed')

    def test_return_timeout_already_on_map_does_not_click_return_again(self):
        self.return_timeout_context()
        self.context.scene='map'
        self.assertTrue(self.run_phase('failure'))
        self.assertEqual(self.context.return_actions,[])
        self.assertEqual(self.outcome()['reason'],'battle_retry')

    def test_late_revival_during_return_confirmation_resumes_same_round(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['afk_mode']=True
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        self.assertTrue(self.run_phase('battle_failure'))
        with patch.object(runtime.Navigator,'locate',return_value='revive_prompt'), \
             patch.object(runtime,'recover_failed_return',return_value=True) as recovery:
            self.assertTrue(self.run_phase('retry_after_failure'))
        recovery.assert_called_once()
        self.assertEqual(self.outcome()['reason'],'battle_retry')
        self.assertEqual(self.outcome()['failure_retries'],1)

    def test_known_nonmap_return_is_operational_failure_not_bad_recording(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['afk_mode']=True
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        self.assertTrue(self.run_phase('battle_failure'))
        with patch.object(runtime.Navigator,'locate',return_value='quest_panel'):
            self.assertFalse(self.run_phase('retry_after_failure'))
        self.assertEqual(self.outcome()['status'],'recoverable')
        self.assertEqual(self.outcome()['reason'],'task_failed')

    def test_return_timeout_stops_if_recording_changes_after_first_retry_click(self):
        self.return_timeout_context(after_click=lambda:self.save_recording('one.json',67))
        with patch.object(runtime.Navigator,'_settle'):
            self.assertFalse(self.run_phase('failure'))
        self.assertEqual(len(self.context.return_actions),1)
        self.assertEqual(self.outcome()['reason'],'recording_changed')

    def test_return_timeout_preserves_disabled_battle_retry_policy(self):
        self.return_timeout_context(succeeds_after=1)
        runtime._rounds[100]['failure_retry']={'enabled':False,'max_retries':0}
        with patch.object(runtime.Navigator,'_settle'):
            self.assertFalse(self.run_phase('failure'))
        self.assertEqual(self.context.scene,'map')
        self.assertEqual(self.outcome()['reason'],'battle_failed')
        self.assertEqual(self.outcome()['failure_retries'],0)

    def test_unrecognized_dialog_failure_follows_restart_toggle_and_pinned_recording(self):
        for restart in (False, True):
            self.context=Context();self.ident=str(uuid4())
            self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':restart}))
            self.assertFalse(self.run_phase('unrecognized_dialog'))
            result=self.outcome()
            self.assertEqual(result['status'],'recoverable' if restart else 'fatal')
            self.assertEqual(result['reason'],'task_failed' if restart else 'unrecognized_dialog')
            self.assertEqual(result['failure_retries'],0)
        self.context=Context();self.ident=str(uuid4())
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        self.save_recording('one.json', 66)
        self.assertFalse(self.run_phase('unrecognized_dialog'))
        self.assertEqual((self.outcome()['status'],self.outcome()['reason']),('fatal','recording_changed'))

    def test_unrecognized_dialog_from_navigation_respects_restart_cancel_and_recording(self):
        for restart in (False,True):
            self.context=Context();self.ident=str(uuid4())
            self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':restart}))
            with patch.object(runtime,'navigate_round',side_effect=runtime.NavigationError('unknown',reason='unrecognized_dialog')):
                self.assertFalse(self.run_phase('navigate'))
            result=self.outcome()
            self.assertEqual(result['status'],'recoverable' if restart else 'fatal')
            self.assertEqual(result['reason'],'task_failed' if restart else 'unrecognized_dialog')
            self.assertEqual(result['failure_retries'],0)
        for cancelled in (False,True):
            self.context=Context();self.ident=str(uuid4())
            self.save_recording('one.json',65)
            self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
            def fail(*_args):
                if cancelled:self.context.tasker.stopping=True
                else:self.save_recording('one.json',66)
                raise runtime.NavigationError('unknown',reason='unrecognized_dialog')
            with patch.object(runtime,'navigate_round',side_effect=fail):
                self.assertFalse(self.run_phase('navigate'))
            result=self.outcome()
            self.assertEqual(result['status'],'cancelled' if cancelled else 'fatal')
            self.assertEqual(result['reason'],'user_cancelled' if cancelled else 'recording_changed')

    def test_unknown_terminal_failure_hands_off_but_blocked_dialog_still_stops(self):
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        self.context.run_recognition = lambda *_args: SimpleNamespace(hit=False)
        with patch.object(runtime, 'confirm_white_screen', return_value=False):
            self.assertFalse(self.run_phase('failure'))
        self.assertEqual((self.outcome()['status'], self.outcome()['reason']), ('recoverable', 'task_failed'))
        self.assertEqual(self.outcome()['navigation_failure'], 'unknown')
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        self.assertFalse(self.run_phase('fatal', reason='blocked_dialog'))
        self.assertEqual(self.outcome()['status'], 'fatal')

    def test_unknown_navigation_exhausts_locating_then_hands_off_only_when_restart_enabled(self):
        for afk in (False, True):
            for restart in (False, True):
                for phase in ('navigate', 'failure', 'retry_after_failure', 'success'):
                    with self.subTest(afk=afk, restart=restart, phase=phase):
                        self.context = Context()
                        settings = self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']
                        settings.update(afk_mode=afk, failure_retry={'enabled': True, 'max_retries': 3})
                        self.assertTrue(self.run_phase('begin', game_management={'restart_on_failure': restart}))
                        if phase == 'retry_after_failure':
                            self.assertTrue(self.run_phase('battle_failure'))
                        expected_attempts = runtime._navigator(self.context, runtime._rounds[100]).routine['locate_attempts']
                        captured_before = self.context.tasker.controller.calls
                        self.context.run_recognition = lambda *_args: SimpleNamespace(hit=False)
                        with patch.object(runtime, 'confirm_white_screen', return_value=False), patch.object(runtime.Navigator, '_settle'), patch.object(self.context, 'run_task', create=True) as inputs:
                            self.assertFalse(self.run_phase(phase))
                        inputs.assert_not_called()
                        result = self.outcome()
                        print('TRACE navigation handoff:', dict(afk=afk, restart=restart, phase=phase,
                              status=result['status'], reason=result['reason'], failure_retries=result['failure_retries']))
                        self.assertEqual(self.context.tasker.controller.calls - captured_before,
                                         0 if phase == 'failure' and not restart else expected_attempts)
                        self.assertEqual(result['failure_retries'], 0, 'An unconfirmed return must not consume a battle retry')
                        self.assertFalse(runtime._rounds)
                        if restart:
                            self.assertEqual((result['status'], result['reason']), ('recoverable', 'task_failed'))
                            self.assertEqual(result['navigation_failure'], 'unknown')
                            self.assertEqual(result['failure_phase'], phase)
                        else:
                            self.assertEqual((result['status'], result['reason']),
                                             ('fatal', 'unrecognized_screen' if phase == 'failure' else 'navigation_unknown'))

    def test_navigation_restart_preserves_blocked_and_ambiguous_guards(self):
        for phase in ('navigate', 'failure', 'retry_after_failure', 'success'):
            for reason in ('blocked', 'ambiguous'):
                with self.subTest(phase=phase, reason=reason):
                    self.context = Context()
                    self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['afk_mode'] = True
                    self.assertTrue(self.run_phase('begin', game_management={'restart_on_failure': True}))
                    if phase == 'retry_after_failure':
                        self.assertTrue(self.run_phase('battle_failure'))
                    with patch.object(runtime, 'confirm_white_screen', return_value=False), patch.object(runtime.Navigator, 'locate', side_effect=runtime.NavigationError('guard', reason=reason)):
                        self.assertFalse(self.run_phase(phase))
                    self.assertEqual((self.outcome()['status'], self.outcome()['reason']), ('fatal', 'navigation_' + reason))

    def test_terminal_task_failure_on_a_known_scene_can_restart(self):
        self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
        with patch.object(runtime, 'confirm_white_screen', return_value=False):
            self.assertFalse(self.run_phase('failure'))
        self.assertEqual((self.outcome()['status'], self.outcome()['reason']), ('recoverable', 'task_failed'))

    def test_stage_that_disappears_after_wait_still_restores_its_viewport(self):
        self.assertTrue(self.run_phase('begin'))
        state = runtime._rounds[100]
        state['parameters']['navigation'] = dict(kind='stage', target_node='stage-target')
        nav = Mock()
        nav.run.side_effect = [runtime.NavigationError('offscreen', reason='unreachable'),
                               runtime.NavigationError('moved again', reason='unreachable'), 'task_destination']
        with patch.object(runtime, '_navigator', return_value=nav), \
             patch.object(runtime, 'wait_stage_ready', return_value=True), \
             patch.object(runtime, 'restore_stage_view', return_value=True) as restore:
            self.assertEqual(runtime.navigate_round(self.context, state), 'task_destination')
        restore.assert_called_once_with(self.context, nav, 'stage-target')
        self.assertEqual(nav.run.call_count, 3)

    def test_blocked_scene_after_wait_never_attempts_a_map_drag(self):
        self.assertTrue(self.run_phase('begin'))
        state = runtime._rounds[100]
        state['parameters']['navigation'] = dict(kind='stage', target_node='stage-target')
        nav = Mock()
        nav.run.side_effect = [runtime.NavigationError('offscreen', reason='unreachable'),
                               runtime.NavigationError('blocked', reason='blocked')]
        with patch.object(runtime, '_navigator', return_value=nav), \
             patch.object(runtime, 'wait_stage_ready', return_value=True), \
             patch.object(runtime, 'restore_stage_view') as restore:
            with self.assertRaises(runtime.NavigationError) as error:
                runtime.navigate_round(self.context, state)
        self.assertEqual(error.exception.reason, 'blocked')
        restore.assert_not_called()

    def test_exhausted_navigation_can_restart_but_blocked_and_ambiguous_scenes_stop(self):
        for reason in ('transition_failed', 'budget', 'unreachable', 'unknown', 'blocked', 'ambiguous'):
            with self.subTest(reason=reason):
                self.assertTrue(self.run_phase('begin',game_management={'restart_on_failure':True}))
                with patch.object(runtime, 'navigate_round', side_effect=runtime.NavigationError('failed',reason=reason)):
                    self.assertFalse(self.run_phase('navigate'))
                expected = 'fatal' if reason in ('blocked', 'ambiguous') else 'recoverable'
                self.assertEqual(self.outcome()['status'], expected)
                if expected == 'recoverable': self.assertEqual(self.outcome()['reason'], 'task_failed')

    def test_missing_and_changed_files_never_trigger_recovery(self):
        self.assertTrue(self.run_phase('begin'))
        self.save_recording('one.json',67)
        with patch.object(runtime,'confirm_white_screen') as white:
            self.assertFalse(self.run_phase('failure'))
        white.assert_not_called()
        self.assertEqual(self.outcome()['reason'],'recording_changed')

    def test_round_preflight_defers_script_aspect_adaptation_to_playback(self):
        params = dict(file='@task:bounty-candle', playback_node='Bounty_Play')
        cases = [((720, 1140, 3), True), ((720, 1145, 3), True),
                 ((720, 1156, 3), True), ((720, 1139, 3), True),
                 ((720, 1157, 3), True), ((719, 1148, 3), False),
                 ((721, 1148, 3), False)]
        for shape, accepted in cases:
            with self.subTest(shape=shape):
                context = Context()
                with patch.object(runtime, 'write_status'), \
                        patch('window_geometry.WindowLease',side_effect=AssertionError('navigation must not lock')):
                    if accepted:
                        state = {}
                        runtime.begin_round(context, params, state, frame=SimpleNamespace(shape=shape))
                        self.assertEqual(state['recording']['base'], [1148, 720])
                        self.assertEqual(context.patches[0]['Bounty_Play']['action']['param']
                                         ['custom_action_param']['expected_base'], [1148, 720])
                    else:
                        with self.assertRaisesRegex(ValueError, '短边720'):
                            runtime.begin_round(context, params, {}, frame=SimpleNamespace(shape=shape))
                        self.assertEqual(context.patches, [])

    def test_round_preflight_returns_fresh_frame_after_script_adaptation(self):
        context=Context(); fresh=Frame(); old=SimpleNamespace(shape=(720,1048,3))
        params=dict(file='@task:bounty-candle',playback_node='Bounty_Play')
        with patch.object(runtime,'prepare_playback_controller',return_value=True,create=True) as prepare, \
                patch.object(runtime,'capture',return_value=fresh) as capture,patch.object(runtime,'write_status'):
            result=runtime.begin_round(context,params,{},frame=old)
        self.assertIs(result,fresh)
        prepare.assert_called_once_with(context.tasker.controller,[1148,720])
        capture.assert_called_once_with(context)

    def test_different_frame_geometry_and_unbalanced_recording_are_fatal(self):
        self.context.tasker.controller.frame=SimpleNamespace(ndim=3,shape=(737,1175,3))
        self.assertFalse(self.run_phase('begin'))
        self.assertEqual(self.outcome()['status'],'fatal')
        self.context.tasker.controller.frame=Frame()
        data=self.save_recording('one.json',65)
        data['actions'].pop()
        (self.recordings/'one.json').write_text(json.dumps(data),encoding='utf8')
        self.ident=str(uuid4())
        self.assertFalse(self.run_phase('begin'))
        self.assertFalse(self.context.patches)

    def test_cancellation_is_never_recoverable(self):
        self.context.tasker.stopping=True
        self.assertFalse(self.run_phase('begin'))
        self.assertEqual(self.outcome()['status'],'cancelled')
        self.assertEqual(self.context.tasker.controller.calls,0)

    def test_unknown_screen_stops_and_only_verified_white_fault_can_recover(self):
        for is_white,expected in ((False,'fatal'),(True,'recoverable')):
            with self.subTest(is_white=is_white):
                self.ident=str(uuid4())
                self.assertTrue(self.run_phase('begin'))
                with patch.object(runtime,'confirm_white_screen',return_value=is_white):
                    self.assertFalse(self.run_phase('failure'))
                self.assertEqual(self.outcome()['status'],expected)
                self.assertFalse(runtime._rounds)

    def test_white_confirmation_uses_three_fresh_frames_and_checks_cancellation(self):
        clock=[0.0]
        def wait(duration): clock[0]+=duration
        with patch.object(runtime.time,'monotonic',side_effect=lambda:clock[0]), patch.object(runtime,'white_frame',return_value=True):
            self.assertTrue(runtime.confirm_white_screen(self.context,wait=wait))
        self.assertEqual(self.context.tasker.controller.calls,3)
        self.assertGreaterEqual(clock[0],.4)
        self.context.tasker.stopping=True
        with self.assertRaises(runtime.Cancelled):
            runtime.confirm_white_screen(self.context,wait=wait)

    def test_status_path_must_match_uuid_in_native_debug_directory(self):
        for parameters in ({'run_id':'not-a-uuid'}, {'run_id':self.ident,'status_path':str(self.root/'escape.json')}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                runtime.status_path(parameters)
        self.assertFalse(self.status.exists())

    def test_rejected_override_cannot_report_success(self):
        self.context.reject=True
        self.assertFalse(self.run_phase('begin'))
        self.assertEqual(self.outcome()['status'],'fatal')
        self.assertFalse(runtime._rounds)

    def test_unwritable_status_never_counts_success_or_escapes_callback(self):
        with patch.object(runtime, 'write_status', side_effect=OSError('disk unavailable')):
            self.assertFalse(self.run_phase('begin'))
        self.assertFalse(runtime._rounds)

    def test_transient_capture_failure_retries_without_restarting(self):
        controller=self.context.tasker.controller
        def capture():
            controller.calls+=1
            succeeded=controller.calls>=2
            return SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=succeeded,get=lambda:controller.frame))
        controller.post_screencap=capture
        with patch.object(runtime.time,'sleep',return_value=None):
            self.assertIs(runtime.capture(self.context),controller.frame)
        self.assertEqual(controller.calls,2)

    def test_transient_controller_lookup_failure_is_retried(self):
        controller=self.context.tasker.controller
        class Tasker:
            stopping=False
            attempts=0
            @property
            def controller(self):
                self.attempts+=1
                if self.attempts==1:raise RuntimeError('controller briefly unavailable')
                return controller
        self.context.tasker=Tasker()
        with patch.object(runtime.time,'sleep',return_value=None):
            self.assertIs(runtime.capture(self.context),controller.frame)
        self.assertEqual(self.context.tasker.attempts,2)
        self.assertEqual(controller.calls,1)

    def test_stopping_during_capture_retry_prevents_another_sample(self):
        controller=self.context.tasker.controller
        def capture():
            controller.calls+=1
            self.context.tasker.stopping=True
            return SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=False))
        controller.post_screencap=capture
        with self.assertRaises(runtime.Cancelled):runtime.capture(self.context)
        self.assertEqual(controller.calls,1)

    def test_persistent_capture_failure_uses_restart_only_when_enabled(self):
        for restart in (False,True):
            self.context=Context();self.ident=str(uuid4())
            self.context.tasker.controller.succeeded=False
            with patch.object(runtime.time,'sleep',return_value=None):
                self.assertFalse(self.run_phase('begin',game_management={'restart_on_failure':restart}))
            self.assertEqual(self.context.tasker.controller.calls,3)
            outcome=self.outcome()
            self.assertEqual(outcome['status'],'recoverable' if restart else 'fatal')
            self.assertEqual(outcome['reason'],'task_failed' if restart else 'capture_unavailable')
            if restart:self.assertEqual(outcome['failure_cause'],'capture_unavailable')

    def test_capture_failure_has_specific_reason_without_claiming_window_loss(self):
        self.context.tasker.controller.succeeded=False
        self.assertFalse(self.run_phase('begin'))
        self.assertEqual(self.outcome()['reason'],'capture_unavailable')
        self.assertEqual(self.outcome()['status'],'fatal')

    def test_automatic_retry_preserves_first_recording_until_next_mfa_round(self):
        self.assertTrue(self.run_phase('begin'))
        first=self.outcome()
        self.assertTrue(self.run_phase('success'))
        self.assertEqual(self.outcome()['recording_sha256'],first['recording_sha256'])
        self.save_recording('one.json',67)
        self.context = Context()
        self.ident=str(uuid4())
        self.context.patches.clear()
        self.context.tasker.controller.calls=0
        self.assertFalse(self.run_phase('begin',expected_recording_file=first['recording_file'],
                                        expected_recording_sha256=first['recording_sha256']))
        self.assertEqual(self.outcome()['reason'],'recording_changed')
        self.assertEqual(self.context.tasker.controller.calls,0)
        self.assertFalse(self.context.patches)
        self.ident=str(uuid4())
        self.assertTrue(self.run_phase('begin'))
        self.assertNotEqual(self.outcome()['recording_sha256'],first['recording_sha256'])

    def test_defeat_returns_then_stops_by_default_without_success(self):
        self.assertTrue(self.run_phase('begin'))
        self.assertTrue(self.run_phase('battle_failure'))
        self.assertFalse(self.run_phase('retry_after_failure'))
        self.assertEqual(self.outcome()['reason'], 'battle_failed')
        self.assertEqual(self.outcome()['failure_retries'], 0)
        self.assertFalse(runtime._rounds)

    def test_defeat_retry_bypasses_quota_and_keeps_one_saved_revision(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['failure_retry'] = {
            'enabled': True, 'max_retries': '1'}
        self.assertTrue(self.run_phase('begin'))
        saved_hash = self.outcome()['recording_sha256']
        self.assertTrue(self.run_phase('battle_failure'))
        self.assertTrue(self.run_phase('retry_after_failure'))
        self.assertEqual(self.context.patches[-1], {'Bounty_FailedMap': {'next': ['Bounty_Navigate']}})
        self.assertEqual(self.outcome()['recording_sha256'], saved_hash)
        self.assertEqual(self.outcome()['failure_retries'], 1)
        self.assertTrue(self.run_phase('battle_failure'))
        self.assertFalse(self.run_phase('retry_after_failure'))
        self.assertEqual(self.outcome()['reason'], 'battle_retry_exhausted')
        self.assertFalse(runtime._rounds)

    def test_zero_retries_disables_reopening_even_when_enabled(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['failure_retry'] = {
            'enabled': True, 'max_retries': '0'}
        self.assertTrue(self.run_phase('begin'))
        self.assertTrue(self.run_phase('battle_failure'))
        patches = len(self.context.patches)
        self.assertFalse(self.run_phase('retry_after_failure'))
        self.assertEqual(self.outcome()['reason'], 'battle_failed')
        self.assertEqual(self.outcome()['failure_retries'], 0)
        self.assertNotIn({'Bounty_FailedMap': {'next': ['Bounty_Navigate']}}, self.context.patches[patches:])
        self.assertFalse(self.outcome()['failure_retry']['enabled'])

    def test_afk_overrides_retry_limit_but_keeps_revision_and_obeys_stop(self):
        settings=self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']
        settings.update(afk_mode=True,failure_retry={'enabled':False,'max_retries':0})
        self.assertTrue(self.run_phase('begin'))
        saved=self.outcome()['recording_sha256']
        for attempt in range(1,26):
            self.assertTrue(self.run_phase('battle_failure'))
            self.assertTrue(self.run_phase('retry_after_failure'))
            self.assertEqual(self.outcome()['failure_retries'],attempt)
            self.assertEqual(self.outcome()['recording_sha256'],saved)
            self.assertEqual(self.context.patches[-1],{'Bounty_FailedMap':{'next':['Bounty_Navigate']}})
        self.context.tasker.stopping=True
        self.assertFalse(self.run_phase('retry_after_failure'))
        self.assertEqual(self.outcome()['status'],'cancelled')

    def test_afk_cannot_retry_unknown_return_or_turn_off_strict_recording_checks(self):
        self.context.nodes['脚本任务设置']['action']['param']['custom_action_param']['afk_mode']=True
        self.assertTrue(self.run_phase('begin'))
        self.assertTrue(self.run_phase('battle_failure'))
        self.context.run_recognition=lambda *args:SimpleNamespace(hit=False)
        self.assertFalse(self.run_phase('retry_after_failure'))
        self.assertEqual(self.outcome()['status'],'fatal')
        self.assertEqual(self.outcome()['reason'], 'navigation_unknown')

    def test_defeat_cannot_be_reported_as_success_or_retried_on_unknown_return(self):
        self.assertTrue(self.run_phase('begin'))
        self.assertTrue(self.run_phase('battle_failure'))
        self.assertFalse(self.run_phase('success'))
        self.assertEqual(self.outcome()['status'], 'fatal')
        self.assertTrue(self.run_phase('begin'))
        self.assertTrue(self.run_phase('battle_failure'))
        self.context.run_recognition = lambda *_args: SimpleNamespace(hit=False)
        with patch.object(runtime.Navigator, '_settle'):
            self.assertFalse(self.run_phase('retry_after_failure'))
        self.assertEqual(self.outcome()['status'], 'fatal')

    def test_editing_during_defeat_retry_cannot_switch_recording(self):
        self.assertTrue(self.run_phase('begin'))
        self.save_recording('one.json', 67)
        self.assertFalse(self.run_phase('navigate'))
        self.assertEqual(self.outcome()['reason'], 'recording_changed')


if __name__=='__main__':
    unittest.main()
