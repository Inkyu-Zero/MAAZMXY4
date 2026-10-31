"""Real Maa/Agent transport with synthetic pixels only; never creates a Win32 controller."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]


def native_case(kind):
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from fixture_pixels import read_png_bgr
    paths=[ROOT/'tests/fixtures/game-management/box-selected.png',ROOT/'tests/fixtures/bounty-candle/login.png',
           ROOT/'tests/fixtures/game-management/save-role.png',ROOT/'tests/fixtures/navigation/task-map-real.png']
    live=kind.startswith('live')
    if live:
        paths=[ROOT/'tests/fixtures/game-management/live-20261030'/(n+'.png') for n in ('box-selected','loading','title','save-role')]
        paths.append(ROOT/'tests/fixtures/navigation/task-map-real.png')
    if kind=='captcha':paths=[ROOT/'tests/fixtures/game-management/box-captcha.png']
    frames=[read_png_bgr(p) for p in paths]
    if kind=='live-slot8':
        # Keep the real eight-slot layout; synthesize an occupied final tile.
        frames[3][504:609,655:905]=frames[3][150:255,328:578].copy()
    class Pixels(CustomController):
        stage=0
        def __init__(self):super().__init__();self.clicks=[];self.loading_frames=0;self.role_transition_frames=0
        def connect(self):return True
        def request_uuid(self):return 'managed-login-memory-only'
        def screencap(self):
            if kind=='live-role-transition' and self.stage==4:
                self.role_transition_frames+=1
                if self.role_transition_frames<=3:return frames[2].copy()
            if live and self.stage==1:
                self.loading_frames+=1
                if self.loading_frames>=3:self.stage=2
            return frames[min(self.stage,len(frames)-1)].copy()
        def touch_down(self,contact,x,y,pressure):
            self.clicks.append([self.stage,x,y]);return True
        def touch_up(self,contact):self.stage+=1;return True
        def forbidden(self,*args):raise AssertionError('No physical input or unexpected action is permitted')
        click=swipe=key_down=key_up=touch_move=input_text=start_app=stop_app=forbidden
    with tempfile.TemporaryDirectory(prefix='managed-login-') as temp:
        directory=Path(temp).resolve();run=str(uuid4());status=directory/'debug/task-runs'/(run+'.json')
        server=directory/'server.py'
        server.write_text('''import sys
sys.path.insert(0,sys.argv[2])
from pathlib import Path
from maa.agent.agent_server import AgentServer
import script_task
script_task.ROOT=Path.cwd()
script_task.STATUS_DIRECTORY=Path.cwd()/'debug/task-runs'
import game_management_login
AgentServer.start_up(sys.argv[1])
AgentServer.join()
AgentServer.shut_down()
''',encoding='utf8')
        logs=ROOT/'.runtime-cache/native-managed-login';logs.mkdir(parents=True,exist_ok=True);Tasker.set_log_dir(logs)
        resource,controller,tasker=Resource(),Pixels(),Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded
        assert tasker.bind(resource,controller)
        agent=AgentClient();agent.set_timeout(6000);assert agent.bind(resource)
        with (directory/'agent.log').open('wb') as output:
            child=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(server),agent.identifier,str(ROOT/'agent')],
                cwd=directory,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert agent.connect()
                params=dict(run_id=run,instance_id='memory',status_path=str(status),account_name='历史账号甲',
                    role_name='角色甲' if kind not in ('wrong-role','live-wrong-role') else '错误角色',
                    save_slot=2 if kind=='live-empty-slot' else 8 if kind=='live-slot8' else 1)
                job=tasker.post_task('GameManagement_Resume',{'GameManagement_Resume':{'action':{'param':{'custom_action_param':params}}}})
                completed={}
                waiter=threading.Thread(target=lambda:completed.update(succeeded=job.wait().succeeded),daemon=True)
                waiter.start();waiter.join(timeout=25)
                state=json.loads(status.read_text(encoding='utf8')) if status.exists() else {}
                agent_log=(directory/'agent.log').read_text(encoding='utf8',errors='replace')
                print('RESULT '+json.dumps(dict(stalled=waiter.is_alive(),**completed,status=state.get('status'),
                    reason=state.get('reason'),clicks=controller.clicks,loading_observed='正在加载游戏' in agent_log)),flush=True)
                if waiter.is_alive():print((directory/'agent.log').read_text(encoding='utf8',errors='replace')[-1500:],flush=True)
            finally:
                child.terminate();child.wait(timeout=5)
        # Native callbacks may still await the now-closed isolated server on a
        # failed case. Exit only this test child, never the user's app/process.
        os._exit(0)


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1','opt-in memory-only Maa Agent integration')
class ManagedLoginTransportTests(unittest.TestCase):
    def test_role_transition_does_not_reclick_title_after_verified_save(self):
        result=self.run_case('live-role-transition')
        self.assertTrue(result['succeeded'],result)
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual([c[0] for c in result['clicks']],[0,2,3],result)

    def test_current_restart_screens_wait_through_loading_and_verify_combined_role_level(self):
        result=self.run_case('live')
        self.assertTrue(result['succeeded'],result)
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual([c[0] for c in result['clicks']],[0,2,3],result)
        self.assertTrue(result['loading_observed'],result)

    def test_current_restart_screens_reject_wrong_role_and_empty_slot(self):
        for kind in ('live-wrong-role','live-empty-slot'):
            with self.subTest(kind=kind):
                result=self.run_case(kind)
                self.assertEqual(result['status'],'fatal',result)
                self.assertEqual([c[0] for c in result['clicks']],[0,2],result)

    def test_eighth_slot_requires_its_own_verified_role(self):
        result=self.run_case('live-slot8')
        self.assertTrue(result['succeeded'],result)
        self.assertEqual(result['status'],'ready',result)
        self.assertTrue(655<=result['clicks'][-1][1]<=905 and 504<=result['clicks'][-1][2]<=609,result)

    def run_case(self,kind):
        result=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'--native-case',kind],
            capture_output=True,text=True,encoding='utf8',errors='replace',timeout=40)
        self.assertEqual(result.returncode,0,result.stdout[-2000:]+result.stderr[-1000:])
        lines=[line[7:] for line in result.stdout.splitlines() if line.startswith('RESULT ')]
        self.assertEqual(len(lines),1,result.stdout[-2500:]);report=json.loads(lines[0])
        self.assertFalse(report['stalled'],result.stdout[-2500:]);return report

    def test_saved_account_game_start_role_confirmation_and_map_ready(self):
        result=self.run_case('success')
        self.assertTrue(result['succeeded'],result);self.assertEqual(result['status'],'ready',result)
        self.assertEqual([c[0] for c in result['clicks']],[0,1,2],result)
        for (_,x,y),(left,top,right,bottom) in zip(result['clicks'],[(100,540,420,625),(450,590,710,690),(325,195,575,255)]):
            self.assertTrue(left<=x<=right and top<=y<=bottom,result)

    def test_captcha_never_clicks_any_login_control(self):
        result=self.run_case('captcha');self.assertEqual(result['clicks'],[],result)
        self.assertEqual(result['status'],'fatal',result)

    def test_wrong_role_never_selects_a_save_or_claims_ready(self):
        result=self.run_case('wrong-role');self.assertEqual([c[0] for c in result['clicks']],[0,1],result)
        self.assertEqual(result['status'],'fatal',result)


if __name__=='__main__':
    if '--native-case' in sys.argv:native_case(sys.argv[-1])
    else:unittest.main()
