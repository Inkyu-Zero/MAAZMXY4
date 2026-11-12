"""Fake-controller playback checks: never load Maa native libraries or send input."""
from contextlib import redirect_stdout, contextmanager
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_player():
    modules = {name: ModuleType(name) for name in
               ("maa", "maa.agent", "maa.agent.agent_server", "maa.custom_action", "toast")}
    class AgentServer:
        @staticmethod
        def custom_action(_name):
            return lambda implementation: implementation
    modules["maa.agent.agent_server"].AgentServer = AgentServer
    modules["maa.custom_action"].CustomAction = object
    modules["toast"].toast = lambda *args, **kwargs: None
    spec = importlib.util.spec_from_file_location("isolated_battle_player", ROOT/"agent/battle_player.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT/"agent"))
    try:
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(ROOT/"agent"))
    return module


player = load_player()


class FakeJob:
    def __init__(self, succeeded=True):
        self.succeeded = succeeded
    def wait(self):
        return self


class FakeController:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.fail_status = set()
        self.after_call = None
    def __getattr__(self, name):
        if not name.startswith("post_"):
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            self.calls.append((name,args,kwargs))
            if self.after_call:
                self.after_call(name,args,kwargs)
            if name in self.fail:
                raise RuntimeError("fake failure")
            return FakeJob(name not in self.fail_status)
        return invoke


class BattlePlayerStudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.controller = FakeController()
        self.tasker = SimpleNamespace(controller=self.controller,stopping=False)
        self.context = SimpleNamespace(tasker=self.tasker)
        self.logs, self.waits, self.notifications = [], [], []
        self.clock = 100.0
        self.patches = [
            patch.object(player,"RECORDING_DIR",str(self.directory)),
            patch.object(player,"_toast",lambda *args,**kwargs: self.notifications.append(args)),
            patch.object(player.BattlePlay,"_log",lambda _self,message: self.logs.append(message)),
            patch.object(player.time,"monotonic",lambda: self.clock),
            patch.object(player,"_sleep_until",self.wait_until)]
        for mocked in self.patches: mocked.start()
    def tearDown(self):
        for mocked in reversed(self.patches): mocked.stop()
        self.temp.cleanup()
    def wait_until(self, target, stopping=None, clock=None):
        self.waits.append(target)
        if stopping and stopping(): return False
        self.clock = target
        return True
    def write(self, actions, *, name="战斗", filename="record.json", **extra):
        data=dict(version=1,name=name,base=[1147,720],actions=actions)
        data.update(extra)
        (self.directory/filename).write_text(json.dumps(data,ensure_ascii=False),encoding="utf8")
        return data
    def run_recording(self, file="record.json", **params):
        return player.BattlePlay().run(self.context,SimpleNamespace(node_name='Playback', custom_action_param=dict(file=file,**params)))

    def guarded_monitor(self, outcome):
        events = []
        monitor = SimpleNamespace(branch=None, error=None, interrupted=False, rpc_lock=threading.RLock(),
            start=lambda:None, probe=lambda:None, request_stop=lambda:None, close=lambda:events.append('closed'),
            recheck_unknown_dialog=lambda:events.append('rechecked'))
        def after(name, *_args):
            events.append(name)
            if name == 'post_key_down':
                monitor.branch = [outcome]
                monitor.interrupted = True
        self.controller.after_call = after
        def override(name, targets):
            events.append(('next', list(targets)))
            return True
        self.context.override_next = override
        return monitor, events

    def test_outcome_stops_tail_and_releases_key_before_branching(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=10000,op='keyup',key=65)])
        for outcome in ('Failed', 'Won'):
            with self.subTest(outcome=outcome):
                monitor, events = self.guarded_monitor(outcome)
                with patch.object(player, 'BattleOutcomeMonitor', return_value=monitor):
                    self.assertTrue(self.run_recording(result_guard={'default_next':['Exit']}))
                self.assertLess(events.index('post_key_up'), events.index(('next',[outcome])))
                self.assertIn('closed', events)

    def test_input_failure_cannot_be_hidden_by_concurrent_result(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=10,op='keyup',key=65)])
        monitor, events = self.guarded_monitor('Failed')
        self.controller.fail.add('post_key_down')
        with patch.object(player, 'BattleOutcomeMonitor', return_value=monitor):
            self.assertFalse(self.run_recording(result_guard={'default_next':['Exit']}))
        self.assertNotIn(('next',['Failed']), events)
        self.assertIn('post_key_up', events)

    def test_guard_cleanup_failure_is_not_downgraded_to_playback_failure(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])
        @contextmanager
        def guard(_):
            yield
            raise player.MouseGuardError('guard cleanup timeout')
        with patch.object(player,'hold_background_mouse',guard), self.assertRaises(player.MouseGuardError):
            player.BattlePlay().run_managed(self.context, SimpleNamespace(
                node_name='Playback', custom_action_param={'file':'record.json'}))
        self.assertEqual([c[0] for c in self.controller.calls],['post_key_down','post_key_up'])

    def test_native_callback_returns_failure_for_guard_cleanup_error(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])
        @contextmanager
        def guard(_):
            yield
            raise player.MouseGuardError('guard cleanup timeout')
        with patch.object(player, 'hold_background_mouse', guard):
            self.assertFalse(self.run_recording())
        self.assertTrue(any('guard cleanup timeout' in message for message in self.logs))

    def test_cancelled_task_never_follows_a_concurrent_result(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=10,op='keyup',key=65)])
        monitor, events = self.guarded_monitor('Won')
        callback = self.controller.after_call
        def after(*args):
            callback(*args)
            self.tasker.stopping = True
        self.controller.after_call = after
        with patch.object(player, 'BattleOutcomeMonitor', return_value=monitor):
            self.assertFalse(self.run_recording(result_guard={'default_next':['Exit']}))
        self.assertNotIn(('next',['Won']), events)

    def test_monitor_error_is_terminal_even_if_the_dialog_disappears_after_release(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=10000,op='keyup',key=65)])
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                monitor, events = self.guarded_monitor('Unused')
                def after(name, *_args):
                    events.append(name)
                    if name == 'post_key_down':
                        monitor.error = ValueError('检测到确认浮窗')
                        monitor.interrupted = True
                    elif name == 'post_key_up':
                        self.tasker.stopping = cancelled
                self.controller.after_call = after
                self.tasker.stopping = False
                with patch.object(player, 'BattleOutcomeMonitor', return_value=monitor):
                    self.assertEqual(self.run_recording(result_guard={
                        'default_next':['Exit'], 'error_next':['Fatal']}), not cancelled)
                if cancelled:
                    self.assertNotIn(('next',['Fatal']),events)
                else:
                    self.assertLess(events.index('post_key_up'),events.index(('next',['Fatal'])))
                    self.assertLess(events.index('closed'),events.index(('next',['Fatal'])))

    def test_unknown_dialog_releases_inputs_before_specific_failure_branch(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=10000,op='keyup',key=65)])
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                monitor, events = self.guarded_monitor('Unused')
                def after(name, *_args):
                    events.append(name)
                    if name == 'post_key_down':
                        monitor.error = player.UnrecognizedDialog('unknown dialog')
                        monitor.interrupted = True
                    elif name == 'post_key_up':
                        self.tasker.stopping = cancelled
                self.controller.after_call = after
                self.tasker.stopping = False
                with patch.object(player, 'BattleOutcomeMonitor', return_value=monitor):
                    self.assertEqual(self.run_recording(result_guard={
                        'default_next':['Exit'], 'error_next':['Fatal']}), not cancelled)
                target=('next',['Result_MonitorUnrecognizedDialog'])
                if cancelled:
                    self.assertNotIn(target,events)
                else:
                    self.assertLess(events.index('post_key_up'),events.index(target))
                    self.assertLess(events.index('closed'),events.index(target))
                    self.assertLess(events.index('closed'),events.index('rechecked'))
                    self.assertLess(events.index('rechecked'),events.index(target))
                    self.assertNotIn(('next',['Fatal']),events)

    def test_capture_loss_keeps_the_normal_failure_path_for_verified_window_recovery(self):
        self.write([dict(t=0,op='keydown',key=65),dict(t=10000,op='keyup',key=65)])
        monitor, events = self.guarded_monitor('Unused')
        monitor.error = RuntimeError('战斗结果截图失败')
        monitor.interrupted = True
        with patch.object(player, 'BattleOutcomeMonitor', return_value=monitor):
            self.assertFalse(self.run_recording(result_guard={'default_next':['Exit'], 'error_next':['Fatal']}))
        self.assertNotIn(('next',['Fatal']),events)

    def test_legacy_actions_keep_order_leading_wait_and_absolute_schedule(self):
        self.write([dict(t=100,op="key",key=74),dict(t=20,op="click",x=10,y=20),
                    dict(t=30,op="text",text="中文"),dict(t=40,op="scroll",dx=-1,dy=2)])
        self.assertTrue(self.run_recording(speed=2))
        self.assertEqual([c[0] for c in self.controller.calls],
                         ["post_click_key","post_click","post_input_text","post_scroll"])
        self.assertAlmostEqual(self.waits[0],100.05)
        self.assertAlmostEqual(self.waits[-1],100.095)

    def test_right_middle_buttons_and_moves_use_contact_numbers(self):
        self.write([dict(t=0,op="down",button="right",x=10,y=20),
                    dict(t=10,op="down",button="middle",x=10,y=20),
                    dict(t=10,op="move",x=20,y=30),dict(t=10,op="up",button="right"),
                    dict(t=10,op="up",button="middle")])
        self.assertTrue(self.run_recording())
        self.assertEqual([c[2]["contact"] for c in self.controller.calls],[1,2,1,2,1,2])

    def test_mouse_release_retains_new_capture_final_drag_endpoint(self):
        self.write([dict(t=0,op="down",button="right",x=10,y=20),
                    dict(t=10,op="up",button="right",x=30,y=40)])
        self.assertTrue(self.run_recording())
        self.assertEqual(self.controller.calls[1],("post_touch_move",(30,40),{"contact":1}))
        self.assertEqual(self.controller.calls[2],("post_touch_up",(),{"contact":1}))

    def test_legacy_standalone_move_clears_implicit_left_button_flag_on_exit(self):
        self.write([dict(t=0,op="move",x=10,y=20)])
        self.assertTrue(self.run_recording())
        self.assertEqual(self.controller.calls[-1],("post_touch_up",(),{"contact":0}))

    def test_duration_preserves_trailing_empty_interval_and_speed(self):
        self.write([dict(t=100,op="key",key=74)],duration_ms=1000)
        self.assertTrue(self.run_recording(speed=2))
        self.assertAlmostEqual(self.waits[-1],100.5)
        self.assertEqual(len(self.controller.calls),1)

    def test_stop_after_keydown_releases_held_key_without_later_action(self):
        self.write([dict(t=0,op="keydown",key=74),dict(t=100000,op="keyup",key=74)])
        self.controller.after_call=lambda *_: setattr(self.tasker,"stopping",True)
        self.assertFalse(self.run_recording())
        self.assertEqual([c[0] for c in self.controller.calls],["post_key_down","post_key_up"])

    def test_stop_during_wait_does_not_dispatch_due_action(self):
        self.write([dict(t=100000,op="key",key=74)])
        def interrupted_wait(target, stopping=None):
            self.tasker.stopping=True
            return True  # Cancellation arriving after a wait also needs checking.
        with patch.object(player,"_sleep_until",interrupted_wait):
            self.assertFalse(self.run_recording())
        self.assertEqual(self.controller.calls,[])

    def test_normal_completion_of_legacy_missing_releases_warns_and_cleans(self):
        self.write([dict(t=10,op="keydown",key=74),dict(t=10,op="down",button="middle",x=5,y=6)])
        with redirect_stdout(io.StringIO()):
            self.assertTrue(self.run_recording())
        self.assertTrue(any("配对" in message for args in self.notifications for message in args))
        self.assertEqual(self.controller.calls[-2:],[("post_key_up",(74,),{}),
                                                    ("post_touch_up",(),{"contact":2})])

    def test_failed_press_still_releases_other_keys_and_buttons(self):
        self.write([dict(t=0,op="keydown",key=74),dict(t=1,op="down",button="right",x=1,y=2),
                    dict(t=1,op="keyup",key=74),dict(t=1,op="up",button="right")])
        self.controller.fail.add("post_touch_down")
        self.assertFalse(self.run_recording())
        self.assertEqual(self.controller.calls[-2:],[("post_key_up",(74,),{}),
                                                    ("post_touch_up",(),{"contact":1})])

    def test_failed_release_does_not_skip_other_cleanup(self):
        self.write([dict(t=0,op="keydown",key=74),dict(t=1,op="down",x=1,y=2)])
        self.controller.fail.add("post_key_up")
        with redirect_stdout(io.StringIO()):
            self.assertFalse(self.run_recording())
        self.assertEqual(self.controller.calls[-1],("post_touch_up",(),{"contact":0}))
        self.assertTrue(any("清理未完成" in message for message in self.logs))

    def test_failed_job_status_is_reported_and_cleanup_attempted(self):
        self.write([dict(t=0,op="keydown",key=74),dict(t=1,op="keyup",key=74)])
        self.controller.fail_status.add("post_key_down")
        self.assertFalse(self.run_recording())
        self.assertEqual(self.controller.calls[-1],("post_key_up",(74,),{}))

    def test_invalid_numbers_and_coordinate_scroll_fail_before_any_input(self):
        cases=[dict(actions=[dict(t=-1,op="key",key=74)]),
               dict(actions=[dict(t=0,op="key",key=True)]),
               dict(actions=[dict(t=0,op="click",x=-1,y=0)]),
               dict(actions=[dict(t=0,op="key",key=74)],duration_ms=float("nan")),
               dict(actions=[dict(t=10,op="key",key=74)],duration_ms=5),
               dict(actions=[dict(t=0,op="scroll",dx=0,dy=1,x=1,y=2)]),
               dict(actions=[dict(t=0,op="未释放")])]
        for case in cases:
            with self.subTest(case=case):
                self.write(**case)
                self.assertFalse(self.run_recording())
                self.assertEqual(self.controller.calls,[])

    def test_stop_just_before_dispatch_does_not_send_a_provisional_press_or_release(self):
        self.write([dict(t=0,op="keydown",key=74),dict(t=10,op="keyup",key=74)])
        class StoppingTasker:
            controller=self.controller
            reads=0
            @property
            def stopping(self):
                self.reads+=1
                return self.reads>=4
        self.context.tasker=StoppingTasker()
        self.assertFalse(self.run_recording())
        self.assertEqual(self.controller.calls,[])

    def test_invalid_speed_fails_without_input(self):
        self.write([dict(t=0,op="key",key=74)])
        for speed in (float("nan"),float("inf"),-1,0,True,"invalid",{},None):
            with self.subTest(speed=speed):
                self.assertFalse(self.run_recording(speed=speed))
                self.assertEqual(self.controller.calls,[])

    def test_live_selected_resolution_does_not_require_client_restart(self):
        self.write([dict(t=0,op="key",key=74)],filename="one.json")
        self.write([dict(t=0,op="key",key=75)],filename="two.json")
        selection=self.directory/".selected.json"
        for filename,key in (("one.json",74),("two.json",75)):
            selection.write_text(json.dumps(dict(file=filename)),encoding="utf8")
            self.assertTrue(self.run_recording(file="@selected"))
            self.assertEqual(self.controller.calls[-1],("post_click_key",(key,),{}))

    def test_bound_library_edit_is_used_by_next_playback_without_reimport(self):
        with patch.object(sys, "path", [str(ROOT/"agent"), *sys.path]):
            from script_studio_service import StudioService
        root = self.directory / "project"
        recordings = root / "recordings"
        recordings.mkdir(parents=True)
        (root / 'interface.json').write_text(json.dumps(dict(task=[dict(name='战斗任务',entry='Fight')], resource=[dict(path=['resource/base'])])), encoding='utf8')
        pipeline = root / 'resource/base/pipeline'
        pipeline.mkdir(parents=True)
        (pipeline/'fight.json').write_text(json.dumps({'Fight': {'action': {'type':'Custom','param': {'custom_action':'battle_play','custom_action_param': {'execution': {'binding_id':'bounty-candle'}}}}}}), encoding='utf8')
        saved = recordings / "战斗.json"
        saved.write_text(json.dumps(dict(version=1,name="战斗",base=[1148,720],
            actions=[dict(t=0,op="key",key=74)])), encoding="utf8")
        service = StudioService(root)
        try:
            service.dispatch("load",dict(file="战斗.json"))
            service.dispatch("bind_task",dict(task_id="bounty-candle"))
            with patch.object(player,"RECORDING_DIR",str(recordings)):
                self.assertTrue(self.run_recording("@task:bounty-candle",strict_task=True))
                operation=service.snapshot()["operations"][0]
                service.dispatch("edit",dict(id=operation["id"],changes=dict(key=75)))
                service.dispatch("save")
                self.assertTrue(self.run_recording("@task:bounty-candle",strict_task=True))
            self.assertEqual([call[1][0] for call in self.controller.calls], [74,75])
            self.assertCountEqual([path.name for path in recordings.glob("*.json")], ["战斗.json", ".bindings.json"])
            self.assertFalse((root/".local-migrations").exists())
        finally:
            service.close()

    def test_task_binding_never_falls_back_to_latest_or_manual_selection(self):
        self.write([dict(t=0,op="key",key=74)])
        with patch.object(player,"ask_recording_file",side_effect=AssertionError("must not ask")):
            self.assertFalse(self.run_recording("@task:bounty-candle"))
        self.assertEqual(self.controller.calls,[])

    def test_strict_task_rejects_unselected_external_raw_and_unbalanced_files(self):
        self.write([dict(t=0,op="key",key=74)])
        self.assertFalse(self.run_recording("@selected",strict_task=True))
        self.assertFalse(self.run_recording("@latest",strict_task=True))
        with tempfile.TemporaryDirectory() as other:
            path=Path(other)/"external.json"
            path.write_text(json.dumps(dict(version=1,name="外部",base=[1148,720],actions=[dict(t=0,op="key",key=74)])),encoding="utf8")
            self.assertFalse(self.run_recording(str(path),strict_task=True))
        self.write([dict(t=0,op="key",key=74)],base=[1175,737])
        self.assertFalse(self.run_recording(strict_task=True))
        self.write([dict(t=0,op="keydown",key=74)])
        self.assertFalse(self.run_recording(strict_task=True))
        self.assertEqual(self.controller.calls,[])

    def test_round_preflight_hash_rejects_mid_round_edit_and_next_round_reads_new(self):
        self.write([dict(t=0,op="key",key=74)])
        path=self.directory/"record.json"
        old_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        self.write([dict(t=0,op="key",key=75)])
        self.assertFalse(self.run_recording(strict_task=True,expected_sha256=old_hash))
        self.assertEqual(self.controller.calls,[])
        new_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertTrue(self.run_recording(strict_task=True,expected_sha256=new_hash))
        self.assertEqual(self.controller.calls[-1],("post_click_key",(75,),{}))

    def test_invalid_round_contract_fails_before_input(self):
        self.write([dict(t=0,op="key",key=74)])
        for options in (dict(expected_sha256="bad"),dict(expected_base=[1148,720]),
                        dict(expected_short_side=737),dict(strict_task="true")):
            with self.subTest(options=options):
                self.assertFalse(self.run_recording(**options))
                self.assertEqual(self.controller.calls,[])

    def test_unselected_falls_back_to_latest_but_stale_selection_never_does(self):
        self.write([dict(t=0,op="key",key=74)])
        self.assertTrue(self.run_recording(file="@selected"))
        self.controller.calls.clear()
        (self.directory/".selected.json").write_text(json.dumps(dict(file="missing.json")),encoding="utf8")
        self.assertFalse(self.run_recording(file="@selected"))
        self.assertEqual(self.controller.calls,[])
        self.assertTrue(any("已不存在" in message for message in self.logs))

    def test_malformed_and_traversal_selection_explicitly_fail(self):
        self.write([dict(t=0,op="key",key=74)])
        for selected in ("broken",json.dumps([]),json.dumps(dict(file="../outside.json")),
                         json.dumps(dict(file=5))):
            with self.subTest(selected=selected):
                (self.directory/".selected.json").write_text(selected,encoding="utf8")
                self.assertFalse(self.run_recording(file="@selected"))
                self.assertEqual(self.controller.calls,[])

    def test_hidden_bookkeeping_files_never_appear_as_recordings(self):
        self.write([dict(t=0,op="key",key=74)])
        (self.directory/".selected.json").write_text('{}',encoding="utf8")
        self.assertEqual(player.list_recordings(),["record"])
        self.assertEqual(player.newest_recording(),"record")

    def test_tasks_use_library_bindings_instead_of_competing_file_selectors(self):
        interface=json.loads((ROOT/"assets/interface.json").read_text(encoding="utf8"))
        self.assertNotIn('回放文件',interface['option'])
        self.assertNotIn('任务脚本来源',interface['option'])
        for option in ('录制内容','战斗录制内容'):
            self.assertEqual(interface['option'][option]['label'],'录制内容是否包含返回地图')
            self.assertEqual([case['name'] for case in interface['option'][option]['cases']],['是','否'])
        count=0
        for path in (ROOT/'assets/resource/base/pipeline').glob('*.json'):
            for node in json.loads(path.read_text(encoding='utf8')).values():
                action=node.get('action',{})
                if isinstance(action,dict) and action.get('param',{}).get('custom_action')=='script_task_state' and action['param']['custom_action_param'].get('phase')=='begin':
                    self.assertTrue(action['param']['custom_action_param']['file'].startswith('@task'))
                    count+=1
        self.assertEqual(count,11+62)

    def test_timestamp_wait_checks_stop_instead_of_sleeping_entire_gap(self):
        # Unpatch only the timing helper; all OS timer and clock access stays fake.
        elapsed=[0.0]; sleeps=[]
        def fake_sleep(seconds):
            sleeps.append(seconds); elapsed[0]+=seconds
        def monotonic():
            elapsed[0]+=.0001
            return elapsed[0]
        original_wait = next(p for p in self.patches if p.attribute=="_sleep_until").temp_original
        with patch.object(player.playback,"_boost_timer",lambda:None), \
             patch.object(player.time,"monotonic",monotonic),patch.object(player.time,"sleep",fake_sleep):
            self.assertFalse(original_wait(999,lambda:elapsed[0]>=.05))
        self.assertTrue(sleeps)
        self.assertLessEqual(max(sleeps),.02)
        self.assertLess(elapsed[0],.08)


if __name__ == "__main__": unittest.main()
