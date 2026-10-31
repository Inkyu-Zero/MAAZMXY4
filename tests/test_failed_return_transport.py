"""Missed Return Map click through real Maa/Agent IPC; only in-memory pixels/input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '悬赏（守卫太宗）-'


def native_case(mode):
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from fixture_pixels import read_png_bgr
    sys.path.insert(0, str(ROOT/'agent'))
    from task_catalog import custom_parameters

    failed = read_png_bgr(ROOT/'tests/fixtures/navigation/challenge-failed.png')
    ready = read_png_bgr(ROOT/'tests/fixtures/navigation/task-map-real.png')
    modal = read_png_bgr(ROOT/'tests/fixtures/navigation/revival/normal-p1-up.png')
    class Pixels(CustomController):
        def __init__(self):super().__init__();self.clicks=[];self.stage='failed'
        def connect(self):return True
        def request_uuid(self):return 'failed-return-memory-only'
        def screencap(self):
            return {'failed':failed,'map':ready,'unknown':np.zeros_like(failed),'modal':modal}[self.stage].copy()
        def touch_down(self, contact, x, y, pressure):
            assert self.stage=='failed' and 500<=x<=1050 and 450<=y<=650,(self.stage,x,y)
            self.clicks.append([x,y]);return True
        def touch_up(self, contact):
            if mode=='recover' and len(self.clicks)>=2:self.stage='map'
            elif mode in ('unknown','modal'):self.stage=mode
            return True
        def forbidden(self,*args):raise AssertionError('No physical or unrelated input is permitted')
        click=swipe=key_down=key_up=click_key=touch_move=input_text=start_app=stop_app=forbidden

    with tempfile.TemporaryDirectory(prefix='failed-return-ipc-') as temp:
        folder=Path(temp).resolve();run=str(uuid4());status=folder/'debug/task-runs'/(run+'.json')
        (folder/'recordings').mkdir()
        recording=folder/'recordings/synthetic.json'
        recording.write_text(json.dumps(dict(version=1,name='synthetic',base=[1148,720],duration_ms=2,
            actions=[dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])),encoding='utf8')
        canonical=json.loads((ROOT/'assets/resource/base/pipeline/悬赏（守卫太宗）.json').read_bytes())
        params=next(custom_parameters(node) for node in canonical.values()
                    if custom_parameters(node).get('phase')=='begin' and custom_parameters(node).get('navigation'))
        params.update(file=str(recording),run_id=run,instance_id='memory',status_path=str(status),navigation_entry='ReturnProbe_Continue')
        def action(phase):return dict(type='Custom',param=dict(custom_action='script_task_state',custom_action_param=dict(phase=phase)))
        overrides={
            'ReturnProbe_Begin':dict(action=dict(type='Custom',param=dict(custom_action='script_task_state',custom_action_param=params)),
                                     next=['ReturnProbe_Defeat'],post_delay=0),
            'ReturnProbe_Defeat':dict(action=action('battle_failure'),next=[PREFIX+'失败返回地图'],post_delay=0),
            'ReturnProbe_Continue':dict(action=dict(type='Custom',param=dict(custom_action='return_probe_done')),post_delay=0),
            '脚本任务设置':dict(action=dict(param=dict(custom_action_param=dict(afk_mode=True)))),
            params['playback_node']:dict(action=dict(param=dict(custom_action_param=dict(file=str(recording))))),
            PREFIX+'失败返回地图':dict(timeout=200,post_delay=0),
        }
        server=folder/'server.py'
        server.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
import script_task
from battle_ui import NoRevivePrompt
script_task.ROOT=Path.cwd()
script_task.RECORDINGS=Path.cwd()/'recordings'
script_task.STATUS_DIRECTORY=Path.cwd()/'debug/task-runs'
AgentServer.custom_recognition('battle_no_revive')(NoRevivePrompt)
@AgentServer.custom_action('return_probe_done')
class Done(CustomAction):
    def run(self,context,argv):
        print('CONTINUED SAME ROUND',flush=True)
        return True
AgentServer.start_up(sys.argv[1])
AgentServer.join()
AgentServer.shut_down()
''',encoding='utf8')
        logs=ROOT/'.runtime-cache/native-failed-return';logs.mkdir(parents=True,exist_ok=True);Tasker.set_log_dir(logs)
        controller,resource,tasker=Pixels(),Resource(),Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded
        assert tasker.bind(resource,controller)
        client=AgentClient();client.set_timeout(6000);assert client.bind(resource)
        with (folder/'agent.log').open('wb') as output:
            child=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(server),client.identifier,str(ROOT/'agent')],
                cwd=folder,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert client.connect()
                job=tasker.post_task('ReturnProbe_Begin',overrides)
                result={}
                waiter=threading.Thread(target=lambda:result.update(succeeded=job.wait().succeeded),daemon=True)
                waiter.start();waiter.join(timeout=40)
                state=json.loads(status.read_bytes()) if status.exists() else {}
                log=(folder/'agent.log').read_text(encoding='utf8',errors='replace')
                print('RESULT '+json.dumps(dict(stalled=waiter.is_alive(),**result,clicks=controller.clicks,
                    status=state.get('status'),reason=state.get('reason'),retries=state.get('failure_retries'),
                    continued='CONTINUED SAME ROUND' in log)),flush=True)
                print(log[-3000:],flush=True)
            finally:child.terminate();child.wait(timeout=5)
        os._exit(0)


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1','opt-in memory-only Agent integration')
class FailedReturnTransportTests(unittest.TestCase):
    def run_case(self,mode):
        process=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'--native-case',mode],
            capture_output=True,text=True,encoding='utf8',errors='replace',timeout=55)
        self.assertEqual(process.returncode,0,process.stdout[-3500:]+process.stderr[-1500:])
        lines=[line[7:] for line in process.stdout.splitlines() if line.startswith('RESULT ')]
        self.assertEqual(len(lines),1,process.stdout[-3500:])
        result=json.loads(lines[0]);self.assertFalse(result['stalled'],process.stdout[-3500:]);return result

    def test_ignored_initial_click_recovers_without_completing_or_counting_a_round(self):
        result=self.run_case('recover')
        self.assertTrue(result['succeeded'],result)
        self.assertTrue(result['continued'],result)
        self.assertEqual(len(result['clicks']),2,result)
        self.assertEqual((result['status'],result['reason'],result['retries']),('running','battle_retry',1),result)

    def test_unresponsive_return_stops_after_two_additional_clicks(self):
        result=self.run_case('stuck')
        self.assertFalse(result['continued'],result)
        self.assertEqual(len(result['clicks']),3,result)
        self.assertEqual((result['status'],result['retries']),('fatal',0),result)

    def test_unknown_or_modal_after_first_click_receives_no_retry_input(self):
        for mode in ('unknown','modal'):
            with self.subTest(mode=mode):
                result=self.run_case(mode)
                self.assertFalse(result['continued'],result)
                self.assertEqual(len(result['clicks']),1,result)
                self.assertEqual(result['status'],'fatal',result)


if __name__=='__main__':
    if '--native-case' in sys.argv:native_case(sys.argv[-1])
    else:unittest.main()
