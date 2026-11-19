"""Exercise playback and outcome recognition together over real Agent IPC.

The controller uses synthetic pixels and records input in memory: no window or
input device is opened.  Enable MAA_AGENT_NATIVE_TESTS with the packaged Maa
runtime to cover the concurrent reverse calls made by a real recording.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def native_case(mode):
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from fixture_pixels import read_png_bgr

    failed_frame = read_png_bgr(ROOT / 'tests/fixtures/navigation/challenge-failed.png')
    is_revive = mode in ('revive', 'revive_live', 'revive_settling')
    revive_fixture = ('revival-live/vip-p1-countdown10.png' if mode in ('revive_live', 'revive_settling')
                      else 'revival/normal-p1-up.png')
    revive_frame = read_png_bgr(ROOT / 'tests/fixtures/navigation' / revive_fixture) if is_revive else None
    if mode == 'revive_settling':
        unready_frame = revive_frame.copy()
        unready_frame[310:350, 424:852] = (170, 120, 50)
    blocked_frame = read_png_bgr(ROOT / 'tests/fixtures/navigation/profile-sell.png') if mode=='blocked' else None

    class MemoryController(CustomController):
        def __init__(self):
            super().__init__()
            self.frames = 0
            self.inputs = []
            self.input_times = []
            self.declined = False
            self.transient_seen = self.settled_seen = False

        def connect(self): return True
        def request_uuid(self): return 'isolated-playback-transport'
        def screencap(self):
            self.frames += 1
            # Approximate a real FramePool capture to expose overlapping RPCs.
            time.sleep(.08)
            if mode=='blocked' and self.inputs==[('down',68)]:return blocked_frame.copy()
            if mode=='return':return failed_frame.copy()
            if is_revive and any(op=='down' for op,_ in self.inputs):
                if mode == 'revive_settling' and not self.declined:
                    if ('up', 68) not in self.inputs:
                        self.transient_seen = True
                        return unready_frame.copy()
                    self.settled_seen = True
                return failed_frame.copy() if self.declined else revive_frame.copy()
            if mode == 'defeat' and any(op == 'down' for op, _ in self.inputs):
                return failed_frame.copy()
            return np.zeros((720, 1147, 3), dtype=np.uint8)
        def key_down(self, key):
            self.inputs.append(('down', key)); self.input_times.append(time.monotonic()); return True
        def key_up(self, key):
            self.inputs.append(('up', key)); self.input_times.append(time.monotonic()); return True
        def touch_down(self,contact,x,y,pressure):
            if mode=='retry_off':
                assert (x,y)==(500,300),(x,y)
                self.inputs.append(('recorded_down',[x,y]));self.input_times.append(time.monotonic());return True
            if mode=='return':
                assert 500<=x<=1050 and 450<=y<=650 and not self.inputs,(x,y,self.inputs)
                self.inputs.append(('return_down',[x,y]));self.input_times.append(time.monotonic());return True
            assert is_revive and 628<=x<=724 and 429<=y<=470,(x,y)
            assert self.inputs==[('down',68),('up',68)],self.inputs
            self.inputs.append(('cancel_down',[x,y]));self.input_times.append(time.monotonic());return True
        def touch_up(self,contact):
            if mode=='retry_off':
                assert self.inputs[-1][0]=='recorded_down'
                self.inputs.append(('recorded_up',contact));self.input_times.append(time.monotonic());return True
            if mode=='return':
                assert self.inputs[-1][0]=='return_down'
                self.inputs.append(('return_up',contact));self.input_times.append(time.monotonic());return True
            assert is_revive and self.inputs[-1][0]=='cancel_down'
            self.declined=True;self.inputs.append(('cancel_up',contact));self.input_times.append(time.monotonic());return True
        def forbidden(self, *args): raise AssertionError('Unexpected input')
        click = swipe = touch_move = input_text = forbidden

    with tempfile.TemporaryDirectory(prefix='battle-playback-transport-') as temporary:
        directory = Path(temporary).resolve()
        log_dir = Path(os.environ.get('MAAZMXY4_TEST_LOG_DIR', ROOT / '.runtime-cache/native-playback-transport'))
        log_dir.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log_dir)
        (directory / 'pipeline').mkdir()
        import shutil
        definitions = json.loads((ROOT / 'assets/resource/base/pipeline/battle_outcomes.json').read_text(encoding='utf8'))
        pipeline = {name: definitions[name] for name in ('Result_Failed', 'Result_Succeeded',
            'Result_RevivePrompt','Result_ReviveWindow','Result_ReviveLayoutP1','Result_ReviveLayoutP2',
            'Result_DeclineRevive','Result_NoRevivePrompt','Result_FailedReturn','Nav_ModalPresent',
            'Result_ReviveEvidence','Nav_CachedModal','Nav_BlockedDialog')}
        pipeline.update(json.loads((ROOT / 'assets/resource/base/pipeline/navigation_common_ui.json').read_text(encoding='utf8')))
        navigation = json.loads((ROOT / 'assets/resource/base/pipeline/navigation.json').read_text(encoding='utf8'))
        pipeline['Nav_See_profile_sell'] = navigation['Nav_See_profile_sell']
        def templates(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    if key=='template':yield from ([child] if isinstance(child,str) else child)
                    else:yield from templates(child)
            elif isinstance(value,list):
                for child in value:yield from templates(child)
        for path in set(templates(pipeline)):
            target = directory / 'image' / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / 'assets/resource/base/image' / path, target)
        if mode in ('revive_live', 'revive_settling', 'blocked'):
            shutil.copytree(ROOT / 'assets/resource/base/model/ocr', directory / 'model/ocr')
        recording = directory / 'synthetic.json'
        actions = [
                dict(t=453, op='keydown', key=68),
                dict(t=62, op='keyup', key=68),
                dict(t=200, op='keydown', key=75),
                dict(t=62, op='keyup', key=75)]
        if mode in ('defeat','blocked') or is_revive:
            actions = [dict(t=453, op='keydown', key=68),
                       dict(t=1500, op='keyup', key=68),
                       dict(t=100, op='click', x=500, y=300)]
        if mode=='retry_off':
            actions=[dict(t=453,op='keydown',key=68),dict(t=2500,op='keyup',key=68),
                     dict(t=100,op='click',x=500,y=300)]
        recording.write_text(json.dumps(dict(version=1, name='synthetic', base=[1148, 720],
            duration_ms=3600 if mode=='retry_off' else 2200 if mode in ('defeat','blocked') or is_revive else 1400,
            actions=actions)), encoding='utf8')
        policy = dict(failed_next=['Failure'], succeeded_next=['Success'],
                      default_next=['Exit'], interval_ms=400)
        policy['revive_next']=['Decline','ConfirmDefeat'] if is_revive else ['Exit']
        if mode=='blocked':policy['error_next']=['Fatal']
        pipeline['Probe'] = {'action': {'type': 'Custom', 'param': {
            'custom_action': 'observed_playback',
            'custom_action_param': {'file': str(recording), 'result_guard': policy}}}, 'post_delay': 0}
        for name in ('Failure', 'Success', 'Exit', 'Fatal'):
            pipeline[name] = {'action': {'type': 'Custom', 'param': {
                'custom_action': 'capture_branch', 'custom_action_param': name}}, 'post_delay': 0}
        if mode=='return':pipeline['Failure']['next']=['Result_FailedReturn']
        if mode=='retry_off':
            # Exercise the production initializer and native merge: the old
            # monitor and result branch must both be replaced before playback.
            pipeline['Prepare']=dict(action=dict(type='Custom',param=dict(custom_action='script_task_state',
                custom_action_param=dict(phase='begin',file=recording.name,playback_node='Probe',
                    route_node='Route',result_guard=policy,failure_retry=dict(enabled=False,max_retries=3)))),
                next=['Probe'],post_delay=0)
            pipeline['Probe']['next']=['Failure']
            pipeline['Route']=dict(action=dict(type='DoNothing'),next=['Exit'],post_delay=0)
        if is_revive:
            from copy import deepcopy
            pipeline['Decline']=deepcopy(definitions['Result_DeclineRevive'])
            pipeline['Decline'].update(next=['ConfirmDefeat'],timeout=3000)
            pipeline['ConfirmDefeat']=dict(recognition=definitions['Result_Failed']['recognition'],
                action=pipeline['Failure']['action'],post_delay=0)
        (directory / 'pipeline/probe.json').write_text(json.dumps(pipeline), encoding='utf8')
        server = directory / 'server.py'
        server.write_text('''\
import sys
sys.path.insert(0, sys.argv[2])
assert sys.flags.utf8_mode, 'Agent fixture requires explicit UTF-8 when using -E'
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
import battle_player
import script_task
from pathlib import Path
from unittest.mock import patch
if sys.argv[3] == 'guard_failure':
    from contextlib import contextmanager
    @contextmanager
    def failing_guard(_controller):
        yield
        print('GUARD_FAILURE_INJECTED', flush=True)
        raise battle_player.MouseGuardError('synthetic guard cleanup timeout')
    battle_player.hold_background_mouse = failing_guard
script_task.ROOT=Path.cwd()
script_task.RECORDINGS=Path.cwd()
script_task.STATUS_DIRECTORY=Path.cwd()/'debug/task-runs'
battle_player.RECORDING_DIR=str(Path.cwd())
from battle_ui import NoRevivePrompt
AgentServer.custom_recognition('battle_no_revive')(NoRevivePrompt)
@AgentServer.custom_action('observed_playback')
class ObservedPlayback(CustomAction):
    def run(self, context, argv):
        controller_type=type(context.tasker.controller)
        original=controller_type.post_screencap
        def capture(controller,*args,**kwargs):
            print('PLAYBACK_CAPTURE',flush=True)
            return original(controller,*args,**kwargs)
        with patch.object(controller_type,'post_screencap',capture):
            return battle_player.BattlePlay().run(context,argv)
@AgentServer.custom_action('capture_branch')
class Branch(CustomAction):
    def run(self, context, argv):
        print('BRANCH ' + str(argv.custom_action_param), flush=True)
        return True
AgentServer.start_up(sys.argv[1])
AgentServer.join()
AgentServer.shut_down()
''', encoding='utf8')
        controller, resource, tasker = MemoryController(), Resource(), Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(directory).wait().succeeded
        assert tasker.bind(resource, controller)
        client = AgentClient()
        client.set_timeout(6000)
        assert client.bind(resource)
        with (directory / 'server.log').open('wb') as output:
            process = subprocess.Popen([sys.executable, '-B', '-E', '-s', '-u', '-X', 'utf8', str(server),
                client.identifier, str(ROOT / 'agent'), mode], cwd=directory, stdout=output,
                stderr=output, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = {}
            try:
                assert client.connect()
                job = tasker.post_task('Prepare' if mode=='retry_off' else 'Probe')
                def wait():
                    result['succeeded'] = job.wait().succeeded
                waiter = threading.Thread(target=wait, daemon=True)
                waiter.start()
                waiter.join(timeout=8)
                output.flush()
                server_output = (directory / 'server.log').read_text(encoding='utf8', errors='replace')
                branches = [json.loads(line[7:]) for line in server_output.splitlines() if line.startswith('BRANCH ')]
                report = dict(stalled=waiter.is_alive(), succeeded=result.get('succeeded', False),
                              playback_captures=server_output.count('PLAYBACK_CAPTURE'),
                              frames=controller.frames, inputs=controller.inputs, branches=branches,
                              transient_seen=controller.transient_seen, settled_seen=controller.settled_seen,
                              guard_failure_injected='GUARD_FAILURE_INJECTED' in server_output,
                              callback_exception='Exception ignored on calling ctypes callback function' in server_output,
                              input_times_ms=[round((t - controller.input_times[0]) * 1000, 1)
                                              for t in controller.input_times])
                print('RESULT ' + json.dumps(report), flush=True)
                if (report['stalled'] or not report['succeeded'] or report['callback_exception']
                        or mode == 'guard_failure' and not report['guard_failure_injected']):
                    print(server_output[-4000:], flush=True)
            finally:
                process.terminate()
                process.wait(timeout=5)
        # If native IPC stalled, release the isolated process rather than
        # waiting on destructors with a blocked C++ callback.
        if report['stalled']:
            os._exit(0)
    os._exit(0)


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS') == '1', 'opt-in Agent IPC memory test')
class PlaybackTransportTests(unittest.TestCase):
    def run_case(self, mode, *, succeeds=True):
        env = dict(os.environ)
        env.setdefault('MAAFW_BINARY_PATH', str(ROOT / 'runtimes/win-x64/native'))
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-case', mode],
            capture_output=True, text=True, encoding='utf8', errors='replace', env=env, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-1000:])
        lines = [line[7:] for line in result.stdout.splitlines() if line.startswith('RESULT ')]
        self.assertEqual(len(lines), 1, result.stdout[-4000:])
        report = json.loads(lines[0])
        self.assertFalse(report['stalled'], result.stdout[-4000:])
        self.assertEqual(report['succeeded'], succeeds, result.stdout[-4000:])
        return report

    def test_guard_failure_returns_failed_action_over_agent_ipc(self):
        report = self.run_case('guard_failure', succeeds=False)
        self.assertTrue(report['guard_failure_injected'])
        self.assertFalse(report['callback_exception'])
        self.assertEqual(report['branches'], [])
        self.assertEqual(report['inputs'], [['down', 68], ['up', 68], ['down', 75], ['up', 75]])

    def test_playback_and_outcome_monitor_share_agent_without_stalling(self):
        report = self.run_case('normal')
        self.assertEqual(report['inputs'], [['down', 68], ['up', 68], ['down', 75], ['up', 75]])
        self.assertGreaterEqual(report['frames'], 2)
        self.assertGreaterEqual(report['playback_captures'],2)
        self.assertEqual(report['branches'], ['Exit'])
        self.assertLess(report['input_times_ms'][1], 500, report)

    def test_defeat_during_held_key_releases_it_and_skips_recorded_click(self):
        report = self.run_case('defeat')
        self.assertEqual(report['inputs'], [['down', 68], ['up', 68]])
        self.assertEqual(report['branches'], ['Failure'])

    def test_retry_off_removes_stale_monitor_and_plays_keys_and_mouse_without_captures(self):
        report=self.run_case('retry_off')
        self.assertEqual(report['playback_captures'],0)
        self.assertEqual([op for op,_ in report['inputs']],['down','up','recorded_down','recorded_up'])
        self.assertEqual(report['branches'],['Exit'])

    def test_disappearing_confirmation_keeps_terminal_branch_over_agent_ipc(self):
        report=self.run_case('blocked')
        self.assertEqual(report['inputs'],[['down',68],['up',68]])
        self.assertEqual(report['branches'],['Fatal'])

    def test_revive_during_held_key_releases_before_declining_and_confirms_defeat(self):
        report=self.run_case('revive')
        self.assertEqual(report['inputs'][:2],[['down',68],['up',68]])
        self.assertEqual([op for op,_ in report['inputs']],['down','up','cancel_down','cancel_up'])
        self.assertEqual(report['branches'],['Failure'])

    def test_captured_free_revive_uses_template_over_agent_ipc_before_cancel(self):
        report=self.run_case('revive_live')
        self.assertEqual(report['inputs'][:2],[['down',68],['up',68]])
        self.assertEqual([op for op,_ in report['inputs']],['down','up','cancel_down','cancel_up'])
        self.assertEqual(report['branches'],['Failure'])

    def test_unready_revive_releases_input_before_rechecking_over_agent_ipc(self):
        report = self.run_case('revive_settling')
        self.assertTrue(report['transient_seen'], report)
        self.assertTrue(report['settled_seen'], report)
        self.assertEqual([op for op, _ in report['inputs']], ['down', 'up', 'cancel_down', 'cancel_up'])
        self.assertEqual(report['branches'], ['Failure'])

    def test_return_absence_guard_can_reenter_native_matching_over_agent_ipc(self):
        report=self.run_case('return')
        self.assertEqual([op for op,_ in report['inputs']],['return_down','return_up'])
        self.assertEqual(report['branches'],['Failure'])


if __name__ == '__main__':
    if '--native-case' in sys.argv: native_case(sys.argv[-1])
    else: unittest.main()
