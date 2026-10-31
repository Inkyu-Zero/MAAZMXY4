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
    is_revive = mode in ('revive', 'revive_live')
    revive_fixture = ('revival-live/vip-p1-countdown10.png' if mode=='revive_live'
                      else 'revival/normal-p1-up.png')
    revive_frame = read_png_bgr(ROOT / 'tests/fixtures/navigation' / revive_fixture) if is_revive else None

    class MemoryController(CustomController):
        def __init__(self):
            super().__init__()
            self.frames = 0
            self.inputs = []
            self.input_times = []
            self.declined = False

        def connect(self): return True
        def request_uuid(self): return 'isolated-playback-transport'
        def screencap(self):
            self.frames += 1
            # Approximate a real FramePool capture to expose overlapping RPCs.
            time.sleep(.08)
            if mode=='return':return failed_frame.copy()
            if is_revive and any(op=='down' for op,_ in self.inputs):
                return failed_frame.copy() if self.declined else revive_frame.copy()
            if mode == 'defeat' and any(op == 'down' for op, _ in self.inputs):
                return failed_frame.copy()
            return np.zeros((720, 1147, 3), dtype=np.uint8)
        def key_down(self, key):
            self.inputs.append(('down', key)); self.input_times.append(time.monotonic()); return True
        def key_up(self, key):
            self.inputs.append(('up', key)); self.input_times.append(time.monotonic()); return True
        def touch_down(self,contact,x,y,pressure):
            if mode=='return':
                assert 500<=x<=1050 and 450<=y<=650 and not self.inputs,(x,y,self.inputs)
                self.inputs.append(('return_down',[x,y]));self.input_times.append(time.monotonic());return True
            assert is_revive and 628<=x<=724 and 429<=y<=470,(x,y)
            assert self.inputs==[('down',68),('up',68)],self.inputs
            self.inputs.append(('cancel_down',[x,y]));self.input_times.append(time.monotonic());return True
        def touch_up(self,contact):
            if mode=='return':
                assert self.inputs[-1][0]=='return_down'
                self.inputs.append(('return_up',contact));self.input_times.append(time.monotonic());return True
            assert is_revive and self.inputs[-1][0]=='cancel_down'
            self.declined=True;self.inputs.append(('cancel_up',contact));self.input_times.append(time.monotonic());return True
        def forbidden(self, *args): raise AssertionError('Unexpected input')
        click = swipe = touch_move = input_text = forbidden

    with tempfile.TemporaryDirectory(prefix='battle-playback-transport-') as temporary:
        directory = Path(temporary).resolve()
        log_dir = ROOT / '.runtime-cache/native-playback-transport'
        log_dir.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log_dir)
        (directory / 'pipeline').mkdir()
        import shutil
        definitions = json.loads((ROOT / 'assets/resource/base/pipeline/battle_outcomes.json').read_text(encoding='utf8'))
        pipeline = {name: definitions[name] for name in ('Result_Failed', 'Result_Succeeded',
            'Result_RevivePrompt','Result_ReviveWindow','Result_ReviveLayoutP1','Result_ReviveLayoutP2',
            'Result_DeclineRevive','Result_NoRevivePrompt','Result_FailedReturn')}
        pipeline.update(json.loads((ROOT / 'assets/resource/base/pipeline/navigation_common_ui.json').read_text(encoding='utf8')))
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
        if mode=='revive_live':
            shutil.copytree(ROOT / 'assets/resource/base/model/ocr', directory / 'model/ocr')
        recording = directory / 'synthetic.json'
        actions = [
                dict(t=453, op='keydown', key=68),
                dict(t=62, op='keyup', key=68),
                dict(t=200, op='keydown', key=75),
                dict(t=62, op='keyup', key=75)]
        if mode=='defeat' or is_revive:
            actions = [dict(t=453, op='keydown', key=68),
                       dict(t=1500, op='keyup', key=68),
                       dict(t=100, op='click', x=500, y=300)]
        recording.write_text(json.dumps(dict(version=1, name='synthetic', base=[1148, 720],
            duration_ms=2200 if mode=='defeat' or is_revive else 1400, actions=actions)), encoding='utf8')
        policy = dict(failed_next=['Failure'], succeeded_next=['Success'],
                      default_next=['Exit'], interval_ms=400)
        policy['revive_next']=['Decline','ConfirmDefeat'] if is_revive else ['Exit']
        pipeline['Probe'] = {'action': {'type': 'Custom', 'param': {
            'custom_action': 'battle_play',
            'custom_action_param': {'file': str(recording), 'result_guard': policy}}}, 'post_delay': 0}
        for name in ('Failure', 'Success', 'Exit'):
            pipeline[name] = {'action': {'type': 'Custom', 'param': {
                'custom_action': 'capture_branch', 'custom_action_param': name}}, 'post_delay': 0}
        if mode=='return':pipeline['Failure']['next']=['Result_FailedReturn']
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
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
import battle_player
from battle_ui import NoRevivePrompt
AgentServer.custom_recognition('battle_no_revive')(NoRevivePrompt)
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
            process = subprocess.Popen([sys.executable, '-B', '-E', '-s', '-u', str(server),
                client.identifier, str(ROOT / 'agent')], cwd=directory, stdout=output,
                stderr=output, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = {}
            try:
                assert client.connect()
                job = tasker.post_task('Probe')
                def wait():
                    result['succeeded'] = job.wait().succeeded
                waiter = threading.Thread(target=wait, daemon=True)
                waiter.start()
                waiter.join(timeout=8)
                output.flush()
                server_output = (directory / 'server.log').read_text(encoding='utf8', errors='replace')
                branches = [json.loads(line[7:]) for line in server_output.splitlines() if line.startswith('BRANCH ')]
                report = dict(stalled=waiter.is_alive(), succeeded=result.get('succeeded', False),
                              frames=controller.frames, inputs=controller.inputs, branches=branches,
                              input_times_ms=[round((t - controller.input_times[0]) * 1000, 1)
                                              for t in controller.input_times])
                print('RESULT ' + json.dumps(report), flush=True)
                if report['stalled'] or not report['succeeded']:
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
    def run_case(self, mode):
        env = dict(os.environ)
        env.setdefault('MAAFW_BINARY_PATH', str(ROOT / 'runtimes/win-x64/native'))
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-case', mode],
            capture_output=True, text=True, encoding='utf8', errors='replace', env=env, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-1000:])
        lines = [line[7:] for line in result.stdout.splitlines() if line.startswith('RESULT ')]
        self.assertEqual(len(lines), 1, result.stdout[-4000:])
        report = json.loads(lines[0])
        self.assertFalse(report['stalled'], result.stdout[-4000:])
        self.assertTrue(report['succeeded'], result.stdout[-4000:])
        return report

    def test_playback_and_outcome_monitor_share_agent_without_stalling(self):
        report = self.run_case('normal')
        self.assertEqual(report['inputs'], [['down', 68], ['up', 68], ['down', 75], ['up', 75]])
        self.assertGreaterEqual(report['frames'], 2)
        self.assertEqual(report['branches'], ['Exit'])
        self.assertLess(report['input_times_ms'][1], 500, report)

    def test_defeat_during_held_key_releases_it_and_skips_recorded_click(self):
        report = self.run_case('defeat')
        self.assertEqual(report['inputs'], [['down', 68], ['up', 68]])
        self.assertEqual(report['branches'], ['Failure'])

    def test_revive_during_held_key_releases_before_declining_and_confirms_defeat(self):
        report=self.run_case('revive')
        self.assertEqual(report['inputs'][:2],[['down',68],['up',68]])
        self.assertEqual([op for op,_ in report['inputs']],['down','up','cancel_down','cancel_up'])
        self.assertEqual(report['branches'],['Failure'])

    def test_captured_free_revive_uses_ocr_over_agent_ipc_before_safe_cancel(self):
        report=self.run_case('revive_live')
        self.assertEqual(report['inputs'][:2],[['down',68],['up',68]])
        self.assertEqual([op for op,_ in report['inputs']],['down','up','cancel_down','cancel_up'])
        self.assertEqual(report['branches'],['Failure'])

    def test_return_absence_guard_can_reenter_native_matching_over_agent_ipc(self):
        report=self.run_case('return')
        self.assertEqual([op for op,_ in report['inputs']],['return_down','return_up'])
        self.assertEqual(report['branches'],['Failure'])


if __name__ == '__main__':
    if '--native-case' in sys.argv: native_case(sys.argv[-1])
    else: unittest.main()
