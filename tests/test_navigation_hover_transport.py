"""One-shot hover recovery over real Agent IPC; all pixels/input stay in memory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


def native_case(mode):
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.define import LoggingLevelEnum
    with tempfile.TemporaryDirectory(prefix='navigation-hover-') as temporary:
        directory=Path(temporary)
        log=Path(os.environ['NAVIGATION_HOVER_TEST_LOG'])/mode
        log.mkdir(parents=True,exist_ok=True)
        Tasker.set_log_dir(log);Tasker.set_stdout_level(LoggingLevelEnum.Off)
        class Pixels(CustomController):
            captures=0;moves=0;clicks=[]
            def connect(self):return True
            def request_uuid(self):return 'hover-recovery-memory'
            def screencap(self):
                self.captures+=1
                frame=np.zeros((720,1148,3),dtype=np.uint8)
                if mode!='unknown':frame[30:40,30:40]=(0,255,0)
                if mode=='ready' or mode in ('cleared','bounty') and self.moves:
                    frame[100:110,150:160]=255
                if mode=='bounty' and self.clicks:frame[300:310,300:310]=255
                if mode=='blocked' or mode=='modal_after_move' and self.moves:
                    frame[60:70,60:70]=255
                return frame
            def key_down(self,key):
                assert key==254,'Only the simulated hover message is allowed'
                self.moves+=1;return True
            def key_up(self,key):assert key==254;return True
            def touch_down(self,contact,x,y,pressure):
                assert mode in ('cleared','bounty') and self.moves==1,(mode,x,y)
                assert 150<=x<160 and 100<=y<110,(x,y)
                self.clicks.append((x,y));return True
            def touch_up(self,contact):return True
            def forbidden(self,*args):raise AssertionError('Unexpected input')
            click=swipe=touch_move=input_text=start_app=stop_app=forbidden
        def color(roi,lower,count=90):
            return dict(recognition=dict(type='ColorMatch',param=dict(roi=roi,lower=lower,upper=lower,count=count)),post_delay=0)
        pipeline={'__Probe':dict(action=dict(type='Custom',param=dict(custom_action='hover_probe',custom_action_param=mode)),post_delay=0),
            '__Map':color([30,30,10,10],[0,255,0]),'__Entry':color([100,100,100,10],[255,255,255]),
            'Nav_Province_town4':color([30,30,10,10],[0,255,0]),
            'Nav_ModalPresent':color([60,60,10,10],[255,255,255]),
            'Nav_BlockedDialog':color([60,60,10,10],[255,255,255]),
            'Result_RevivePrompt':color([80,80,10,10],[255,255,255]),
            'Nav_See_profile_sell':color([80,80,10,10],[255,255,255])}
        pipeline['__Entry']['action']=dict(type='Click',param=dict(target=True))
        pipeline['Nav_ChooseBounty']=pipeline['__Entry']
        pipeline['__Goal']=color([300,300,10,10],[255,255,255])
        pipeline['Nav_CachedModal']=pipeline['Nav_ModalPresent']
        (directory/'pipeline').mkdir()
        (directory/'pipeline/test.json').write_text(json.dumps(pipeline),encoding='utf8')
        server=directory/'server.py'
        server.write_text('''import sys,json
from types import SimpleNamespace
sys.path.insert(0,sys.argv[2])
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
import navigation.runtime as runtime
from navigation import NavigationError
import cursor_action
import mouse_input
@AgentServer.custom_action('hover_probe')
class Probe(CustomAction):
    def run(self,context,argv):
        mode=json.loads(argv.custom_action_param)
        ctrl=context.tasker.controller
        messages=[]
        def message(hwnd,x,y):
            assert (hwnd,x,y)==(42,20,20),(hwnd,x,y)
            messages.append((hwnd,x,y))
            assert context.tasker.controller.post_key_down(254).wait().succeeded
            assert context.tasker.controller.post_key_up(254).wait().succeeded
            return True,'simulated WM_MOUSEMOVE via memory controller'
        class MemoryWindow:
            def __init__(self,native):self.native=native
            @property
            def info(self):
                value=dict(self.native.info) # Exercise the real reverse info request.
                value['hwnd']=42      # A memory controller has no OS HWND.
                return value
        original=runtime._move_cursor_off_target
        cursor_action._post_move=message
        mouse_input.post_move=message
        runtime._move_cursor_off_target=lambda native:original(MemoryWindow(native))
        routine=dict(kind='stage',province='town4',targets=['task_destination'],destination='map',
            max_seconds=5,max_steps=5,locate_attempts=1,settle_ms=0,actions={},routes={},fallback=[],
            screens={'task_destination':dict(detector='__Entry',label='entry'),
                     'map':dict(detector='__Map',label='map')})
        if mode=='bounty':
            routine.update(kind='bounty',fallback=['accepted_bounties'],actions={
                'Nav_ChooseBounty':dict(label='bounty row',verify=['task_destination'])})
            routine['screens']={'task_destination':dict(detector='__Goal',label='goal'),
                                'accepted_bounties':dict(detector='__Map',label='bounties')}
        def capture():
            current=context.tasker.controller
            assert current.post_screencap().wait().succeeded
            return current.cached_image
        nav=runtime.Navigator(context,routine,capture=capture,check=lambda:None,frame=ctrl.cached_image)
        reasons=[]
        for _ in range(2 if mode=='absent' else 1):
            try:
                reasons.append(nav.run())
                if mode=='cleared':assert nav.perform('__Entry')
            except NavigationError as error:reasons.append(error.reason)
        print('HOVER_RESULT '+json.dumps(dict(reasons=reasons,messages=messages)),flush=True)
        return True
AgentServer.start_up(sys.argv[1]);AgentServer.join();AgentServer.shut_down()
''',encoding='utf8')
        controller,resource,tasker=Pixels(),Resource(),Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(directory).wait().succeeded
        assert tasker.bind(resource,controller)
        client=AgentClient();client.set_timeout(5000);assert client.bind(resource)
        with (log/'agent.log').open('wb') as output:
            process=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(server),client.identifier,str(ROOT/'agent')],
                cwd=directory,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert client.connect()
                success=tasker.post_task('__Probe').wait().succeeded
            finally:
                process.terminate();process.wait(5)
        content=(log/'agent.log').read_text(encoding='utf8',errors='replace')
        results=[json.loads(line[13:]) for line in content.splitlines() if line.startswith('HOVER_RESULT ')]
        assert success and len(results)==1,content[-5000:]
        print('RESULT '+json.dumps(dict(results[0],captures=controller.captures,moves=controller.moves,clicks=controller.clicks)))


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1','opt-in memory-only Agent IPC')
class HoverTransportTests(unittest.TestCase):
    def test_current_frame_one_shot_and_blocker_behavior_over_real_agent(self):
        expected={'ready':(['task_destination'],1,0,0),'cleared':(['task_destination'],2,2,1),
                  'absent':(['unreachable','unreachable'],2,1,0),
                  'blocked':(['blocked'],1,0,0),'modal_after_move':(['blocked'],2,1,0),
                  'unknown':(['unknown'],1,0,0),'bounty':(['task_destination'],3,2,1)}
        for mode,(reasons,frames,moves,clicks) in expected.items():
            with self.subTest(mode=mode):
                # Native logging remains open until process exit. The parent,
                # not the native child, therefore owns temporary log cleanup.
                with tempfile.TemporaryDirectory(prefix='hover-logs-') as logs:
                    env=dict(os.environ, NAVIGATION_HOVER_TEST_LOG=os.environ.get('NAVIGATION_HOVER_TEST_LOG',logs))
                    run=subprocess.run([sys.executable,'-B','-E','-s','-X','utf8',str(Path(__file__).resolve()),'--case',mode],
                        capture_output=True,text=True,encoding='utf8',errors='replace',timeout=20,env=env)
                self.assertEqual(run.returncode,0,run.stdout[-2000:]+run.stderr[-3000:])
                result=json.loads(next(line[7:] for line in run.stdout.splitlines() if line.startswith('RESULT ')))
                self.assertEqual(result['reasons'],reasons,result)
                self.assertEqual((result['captures'],result['moves'],len(result['clicks'])),(frames,moves,clicks),result)
                self.assertEqual(len(result['messages']),moves,result)


if __name__=='__main__':
    if len(sys.argv)>2 and sys.argv[1]=='--case':native_case(sys.argv[2])
    else:unittest.main()
