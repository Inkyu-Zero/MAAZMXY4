"""Whole compact round across real Agent IPC, using only an in-memory controller."""
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

def native_case(mode):
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from fixture_pixels import read_png_bgr
    from test_bounty_candle_native import quest_frames,panel_title
    sys.path.insert(0,str(ROOT/'agent'))
    from task_catalog import task_catalog
    tasks=task_catalog(ROOT,bindings={})
    name='悬赏（守卫太宗）' if mode=='quota' else '悬赏（守卫宝烛）'
    task=next(t for t in tasks if t['name']==name)
    params=task['management_profile']['parameters'];entry=task['entry']
    frames={name:read_png_bgr(ROOT/'tests/fixtures/bounty-candle'/(name+'.png'))
            for name in ('quest-map','quest-tab','quest-candle','candle-battle-ready')}
    quest_frames(frames)
    frames['quest-candle']=panel_title(frames['quest-candle'],params['navigation']['title'])
    frames['failed']=read_png_bgr(ROOT/'tests/fixtures/navigation/challenge-failed.png')
    frames['dialog']=read_png_bgr(ROOT/'tests/fixtures/navigation/common-dialog/confirm-440-1.png')
    frames['sell']=read_png_bgr(ROOT/'tests/fixtures/navigation/profile-sell.png')
    frames['captcha']=frames['dialog'].copy()
    captcha=read_png_bgr(ROOT/'tests/fixtures/game-management/box-captcha.png')
    frames['captcha'][300:340,440:540]=captcha[409:449,382:482]
    class Game(CustomController):
        scene='quest-map';plays=0;returns=0
        def __init__(self):super().__init__();self.inputs=[];self.captures=[]
        def connect(self):return True
        def request_uuid(self):return 'compact-round-memory-only'
        def screencap(self):
            self.captures.append((self.scene,len(self.inputs)))
            if self.scene=='capture':return None
            return frames[self.scene].copy()
        def touch_down(self,contact,x,y,pressure):
            self.inputs.append(('down',self.scene,x,y));return True
        def touch_up(self,contact):
            self.inputs.append(('up',self.scene))
            if self.scene=='failed':
                self.returns+=1
                if self.returns>=2:self.scene='quest-map'
            else:self.scene={'quest-map':'quest-main','quest-main':'quest-candle','quest-candle':'candle-battle-ready'}[self.scene]
            return True
        def key_down(self,keycode):
            self.inputs.append(('key_down',keycode))
            if mode in ('dialog','captcha','sell','capture'):self.scene=mode
            return True
        def key_up(self,keycode):
            self.inputs.append(('key_up',keycode));self.plays+=1
            if mode!='capture':self.scene='failed' if mode=='retry' and self.plays==1 else 'quest-map'
            return True
        def forbidden(self,*args):raise AssertionError('No physical or unrelated input')
        click=swipe=touch_move=input_text=start_app=stop_app=forbidden
    with tempfile.TemporaryDirectory(prefix='compact-round-ipc-') as temp:
        directory=Path(temp);library=directory/'recordings';library.mkdir()
        Tasker.set_log_dir(directory/'logs')
        recording=library/'fixture.json'
        hold=4500 if mode in ('dialog','captcha','sell','capture') else 1
        recording.write_text(json.dumps(dict(version=1,name='IPC fixture',base=[1148,720],duration_ms=hold,
            actions=[dict(t=0,op='keydown',key=65),dict(t=hold,op='keyup',key=65)])),encoding='utf8')
        run=str(uuid4());status=directory/'debug/task-runs'/(run+'.json')
        params.update(file=str(recording),run_id=run,exit_mode='recorded_return',
                      game_management=dict(restart_on_failure=mode in ('dialog','captcha','sell','capture')))
        patch={entry:dict(action=dict(param=dict(custom_action_param=params)),max_hit=2 if mode=='quota' else 1),
               'Bounty_Play':dict(action=dict(param=dict(custom_action_param=dict(file=str(recording))))),
               '脚本任务设置':dict(action=dict(param=dict(custom_action_param=dict(afk_mode=mode in ('retry','dialog','captcha','sell','capture'),exit_mode='recorded_return'))))}
        server=directory/'server.py'
        server.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from maa.agent.agent_server import AgentServer
import battle_player
import script_task
from battle_ui import NoRevivePrompt
script_task.ROOT=Path.cwd()
script_task.RECORDINGS=Path.cwd()/'recordings'
script_task.STATUS_DIRECTORY=Path.cwd()/'debug/task-runs'
battle_player.RECORDING_DIR=str(Path.cwd()/'recordings')
AgentServer.custom_recognition('battle_no_revive')(NoRevivePrompt)
AgentServer.start_up(sys.argv[1])
AgentServer.join()
AgentServer.shut_down()
''',encoding='utf8')
        resource,controller,tasker=Resource(),Game(),Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded
        assert tasker.bind(resource,controller)
        client=AgentClient();client.set_timeout(10000);assert client.bind(resource)
        with (directory/'agent.log').open('wb') as output:
            child=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(server),client.identifier,str(ROOT/'agent')],
                cwd=directory,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert client.connect()
                job=tasker.post_task(entry,patch);result={}
                waiter=threading.Thread(target=lambda:result.update(succeeded=job.wait().succeeded),daemon=True)
                waiter.start();waiter.join(timeout=100)
                state=json.loads(status.read_text(encoding='utf8')) if status.exists() else {}
                agent_output=(directory/'agent.log').read_text(encoding='utf8',errors='replace')
                print('RESULT '+json.dumps(dict(stalled=waiter.is_alive(),**result,plays=controller.plays,
                    returns=controller.returns,captures=controller.captures,inputs=controller.inputs,
                    status=state.get('status'),reason=state.get('reason'),retries=state.get('failure_retries'),
                    navigation_failure=state.get('navigation_failure'),failure_phase=state.get('failure_phase'),
                    failure_cause=state.get('failure_cause'),
                    known_blocker_logged='检测到验证码、登录或阻断提示' in agent_output,
                    known_sell_logged='检测到出售确认浮窗' in agent_output)),flush=True)
                print(agent_output,flush=True)
            finally:child.terminate();child.wait(timeout=5)
        os._exit(0)

@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1','opt-in memory-only real Agent IPC')
class CompactRoundTransportTests(unittest.TestCase):
    def run_case(self,mode,expected='success'):
        result=subprocess.run([sys.executable,'-B','-E','-s','-X','utf8',str(Path(__file__).resolve()),'--native-case',mode],
            cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
        line=next((s[7:] for s in result.stdout.splitlines() if s.startswith('RESULT ')),None)
        self.assertIsNotNone(line,result.stdout[-4000:]+result.stderr[-3000:])
        evidence=json.loads(line);print('COMPACT IPC',mode,line,flush=True)
        self.assertFalse(evidence['stalled']);self.assertEqual(evidence['status'],expected,result.stdout[-5000:])
        return evidence
    def test_success_uses_five_frames_with_full_bounty_tab_and_title_checks(self):
        data=self.run_case('success');self.assertTrue(data['succeeded'])
        self.assertEqual(data['plays'],1);self.assertEqual(len(data['captures']),5)
    def test_ignored_return_click_recovers_and_retries_the_same_round(self):
        data=self.run_case('retry');self.assertTrue(data['succeeded'])
        self.assertEqual(data['plays'],2);self.assertEqual(data['returns'],2);self.assertEqual(data['retries'],1)
    def test_unknown_modal_hands_off_over_agent_ipc_after_releasing_held_input(self):
        data=self.run_case('dialog',expected='recoverable')
        self.assertFalse(data['succeeded'])
        self.assertEqual(data['reason'],'task_failed')
        self.assertEqual(data['navigation_failure'],'unrecognized_dialog')
        self.assertEqual(data['failure_phase'],'battle_monitor')
        self.assertEqual(data['retries'],0)
        self.assertTrue(any(scene=='dialog' for scene,_ in data['captures']))
        self.assertEqual([item[0] for item in data['inputs'] if item[0].startswith('key_')],['key_down','key_up'])
        self.assertFalse(any(item[0]=='down' and item[1]=='dialog' for item in data['inputs']))

    def test_known_captcha_dialog_remains_fatal_over_agent_ipc(self):
        data=self.run_case('captcha',expected='fatal')
        self.assertFalse(data['succeeded'])
        self.assertEqual(data['reason'],'blocked_dialog')
        self.assertTrue(data['known_blocker_logged'])
        self.assertTrue(any(scene=='captcha' for scene,_ in data['captures']))
        self.assertFalse(any(item[0]=='down' and item[1]=='captcha' for item in data['inputs']))

    def test_known_sell_confirmation_remains_fatal_over_agent_ipc(self):
        data=self.run_case('sell',expected='fatal')
        self.assertFalse(data['succeeded'])
        self.assertEqual(data['reason'],'blocked_dialog')
        self.assertTrue(data['known_sell_logged'])
        self.assertTrue(any(scene=='sell' for scene,_ in data['captures']))
        self.assertFalse(any(item[0]=='down' and item[1]=='sell' for item in data['inputs']))

    def test_persistent_capture_failure_hands_off_after_releasing_input(self):
        data=self.run_case('capture',expected='recoverable')
        self.assertFalse(data['succeeded'])
        self.assertEqual(data['reason'],'task_failed')
        self.assertEqual(data['failure_cause'],'capture_unavailable')
        self.assertEqual(data['retries'],0)
        self.assertGreaterEqual(sum(scene=='capture' for scene,_ in data['captures']),3)
        self.assertEqual([item[0] for item in data['inputs'] if item[0].startswith('key_')],['key_down','key_up'])

    def test_native_quota_stops_after_two_confirmed_rounds(self):
        data=self.run_case('quota');self.assertEqual(data['plays'],2);self.assertEqual(data['retries'],0)

if __name__=='__main__':
    if '--native-case' in sys.argv:native_case(sys.argv[-1])
    else:unittest.main()
