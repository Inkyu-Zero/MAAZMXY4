"""Endpoint reuse and ignored clicks over real Agent IPC, with memory-only input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]


def native_case(name,deadline_probe=False):
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.define import LoggingLevelEnum
    from maa.resource import Resource
    from maa.tasker import Tasker
    from fixture_pixels import read_png_bgr
    profile=json.loads((ROOT/'tools/script_task_profiles.json').read_text(encoding='utf8'))[name]
    recipes=json.loads((ROOT/'agent/data/navigation/stage-scroll.json').read_text(encoding='utf8'))['maps']
    recipe=next((item for item in recipes if item['task']==name),None)
    blood=recipe is not None
    maximum=round((1500-940)*720/590)
    image_root=ROOT/'assets/resource/base/image'
    terrain=read_png_bgr(ROOT/'tests/fixtures/game-management/scroll/town5.png') if blood else None
    ordinary=read_png_bgr(ROOT/'tests/fixtures/navigation/stage-states/s45-f1.png')
    task=read_png_bgr(image_root/'任务.png')
    mode=read_png_bgr(image_root/'navigation/scroll/normal-mode.png')
    scene=np.zeros((720,1147,3),dtype=np.uint8)
    from test_shared_stage_entry import with_stage_controls
    scene=with_stage_controls(scene)
    bounds={'伽岚（万世冥阁）':(1099.8,342.95,1202.8,426.95),
            '无冥（孤魂岭）':(211,66,361,216)}

    class Pixels(CustomController):
        def __init__(self):
            super().__init__()
            self.round=0;self.scene='map';self.ignore=False
            self.offset=maximum if blood and recipe['view']=='left' else 0
            self.captures=[];self.actions=[]
        def connect(self):return True
        def request_uuid(self):return 'stage-entry-memory-only'
        def screencap(self):
            self.captures.append((self.round,self.scene,len(self.actions)))
            if self.scene=='stage':return scene.copy()
            if not blood:return ordinary.copy()
            frame=terrain[:720,self.offset:self.offset+1147].copy()
            frame[620:620+task.shape[0],800:800+task.shape[1]]=task
            region=frame[617:617+mode.shape[0],1036:1036+mode.shape[1]]
            mask=np.any(mode!=(0,255,0),axis=2);region[mask]=mode[mask]
            return frame
        def touch_down(self,contact,x,y,pressure):
            assert self.scene=='map','No input may be sent inside a stage'
            self.start=self.end=(x,y);return True
        def touch_move(self,contact,x,y,pressure):self.end=(x,y);return True
        def touch_up(self,contact):
            if self.start!=self.end:
                assert blood
                for i in range(101):
                    x=self.start[0]+(self.end[0]-self.start[0])*i/100
                    offset=min(maximum,max(0,self.offset+self.start[0]-x))
                    sx,sy=(x+offset)*590/720,self.start[1]*590/720
                    assert not any(a<=sx<=c and b<=sy<=d for a,b,c,d in recipe['excluded_stage_bounds'])
                self.offset=min(maximum,max(0,self.offset+self.start[0]-self.end[0]))
                self.actions.append((self.round,'drag',self.start,self.end,self.offset))
                assert self.offset==(0 if recipe['view']=='left' else maximum)
            else:
                if blood:
                    sx,sy=(self.start[0]+self.offset)*590/720,self.start[1]*590/720
                    a,b,c,d=bounds[name];assert a<=sx<=c and b<=sy<=d,('stale click',sx,sy)
                self.actions.append((self.round,'ignored' if self.ignore else 'entry',self.start,time.monotonic()))
                if not self.ignore:self.scene='stage'
                self.ignore=max(0,self.ignore-1)
            return True
        def key_down(self,key):
            assert key in (250,251,252),'Only test scene-reset signals are allowed'
            self.round+=1;self.scene='map';self.ignore=max(0,key-250)
            if blood and key==251:self.offset=round(maximum/2)
            return True
        def key_up(self,key):assert key in (250,251,252);return True
        def forbidden(self,*args):raise AssertionError('Unexpected physical input')
        click=swipe=input_text=start_app=stop_app=forbidden

    directory=Path(os.environ['STAGE_ENTRY_TEST_LOG'])/name
    directory.mkdir(parents=True,exist_ok=True)
    Tasker.set_log_dir(directory/'native');Tasker.set_stdout_level(LoggingLevelEnum.Off)
    with tempfile.TemporaryDirectory(prefix='stage-entry-ipc-') as temporary:
        working=Path(temporary)
        (working/'recordings').mkdir()
        (working/'recordings/fixture.json').write_text(json.dumps(dict(version=1,name='fixture',
            base=[1147,720],duration_ms=1,actions=[dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])),encoding='utf8')
        server=working/'server.py'
        server.write_text('''import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from battle_ui import NoRevivePrompt
import script_task
script_task.RECORDINGS=Path.cwd()/'recordings'
AgentServer.custom_recognition('battle_no_revive')(NoRevivePrompt)
@AgentServer.custom_action('stage_entry_probe')
class Probe(CustomAction):
    def run(self,context,argv):
        params=json.loads(argv.custom_action_param)
        state=dict(parameters=params,reference='fixture.json',recording=script_task.inspect_recording('fixture.json'))
        results=[]
        class TracedContext:
            def __init__(self):self.calls=[];self.clock=None
            def __getattr__(self,key):return getattr(context,key)
            def run_recognition(self,name,image,*args,**kwargs):
                self.calls.append(name)
                result=context.run_recognition(name,image,*args,**kwargs)
                if self.clock is not None and name=='Nav_BlockedDialog':self.clock[0]+=.31
                return result
        traced=TracedContext()
        for iteration in range(4 if params.get('deadline_probe') else 3):
            if iteration:
                key=249+iteration
                assert context.tasker.controller.post_key_down(key).wait().succeeded
                assert context.tasker.controller.post_key_up(key).wait().succeeded
            traced.calls=[]
            frame=context.tasker.controller.cached_image if iteration==0 else None
            nav=script_task._navigator(traced,state,frame=frame)
            if iteration==3:
                params['round_flow']['load_timeout_ms']=150
                traced.clock=[0.0]
                nav.clock=lambda:traced.clock[0]
                nav.wait=lambda delay:traced.clock.__setitem__(0,traced.clock[0]+delay)
            start=time.monotonic()
            assert script_task.enter_stage(traced,state,nav)
            results.append(dict(elapsed=time.monotonic()-start,calls=traced.calls))
        print('ENTRY_RESULT '+json.dumps(results),flush=True)
        return True
AgentServer.start_up(sys.argv[1]);AgentServer.join();AgentServer.shut_down()
''',encoding='utf8')
        resource,controller,tasker=Resource(),Pixels(),Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded
        assert tasker.bind(resource,controller)
        params=dict(navigation=dict(kind='stage',target_node=profile['target_node']),
                    round_flow=dict(scene_node=profile['scene_node'],load_timeout_ms=15000),
                    deadline_probe=deadline_probe)
        resource.override_pipeline({'__StageEntryProbe':dict(pre_delay=0,post_delay=0,
            action=dict(type='Custom',param=dict(custom_action='stage_entry_probe',custom_action_param=params)))})
        client=AgentClient();client.set_timeout(30000);assert client.bind(resource)
        with (directory/'agent.log').open('wb') as output:
            process=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(server),client.identifier,str(ROOT/'agent')],
                cwd=working,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert client.connect()
                success=tasker.post_task('__StageEntryProbe').wait().succeeded
            finally:process.terminate();process.wait(5)
        content=(directory/'agent.log').read_text(encoding='utf8',errors='replace')
        results=[json.loads(line[13:]) for line in content.splitlines() if line.startswith('ENTRY_RESULT ')]
        assert success and len(results)==1,content[-5000:]
        print('RESULT '+json.dumps(dict(rounds=results[0],captures=controller.captures,actions=controller.actions)))


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1','opt-in memory-only Agent IPC')
class StageEntryTransportTests(unittest.TestCase):
    def test_last_retry_survives_recognition_crossing_deadline_over_real_ipc(self):
        with tempfile.TemporaryDirectory(prefix='stage-deadline-logs-') as logs:
            env=dict(os.environ,STAGE_ENTRY_TEST_LOG=os.environ.get('STAGE_ENTRY_TEST_LOG',logs))
            run=subprocess.run([sys.executable,'-B','-E','-s','-X','utf8',str(Path(__file__).resolve()),
                '--case','摩多（幕山）','--deadline'],capture_output=True,text=True,encoding='utf8',
                errors='replace',timeout=150,env=env)
            self.assertEqual(run.returncode,0,run.stdout[-1500:]+run.stderr[-4000:])
            result=json.loads(next(line[7:] for line in run.stdout.splitlines() if line.startswith('RESULT ')))
            self.assertEqual([a[1] for a in result['actions'] if a[0]==3],['ignored','ignored','entry'])
            print('DEADLINE IPC',json.dumps(result,ensure_ascii=False),flush=True)

    def test_endpoint_reuse_and_ignored_clicks_on_blood_sea_and_ordinary_stage(self):
        for name in ('无冥（孤魂岭）','伽岚（万世冥阁）','摩多（幕山）'):
            with self.subTest(task=name),tempfile.TemporaryDirectory(prefix='stage-entry-logs-') as logs:
                env=dict(os.environ,STAGE_ENTRY_TEST_LOG=os.environ.get('STAGE_ENTRY_TEST_LOG',logs))
                run=subprocess.run([sys.executable,'-B','-E','-s','-X','utf8',str(Path(__file__).resolve()),'--case',name],
                    capture_output=True,text=True,encoding='utf8',errors='replace',timeout=150,env=env)
                self.assertEqual(run.returncode,0,run.stdout[-1500:]+run.stderr[-4000:])
                result=json.loads(next(line[7:] for line in run.stdout.splitlines() if line.startswith('RESULT ')))
                blood=name!='摩多（幕山）'
                self.assertEqual([sum(r==i for r,_,_ in result['captures']) for i in range(3)],
                                 [3,2,4] if blood else [2,2,3],result)
                for i,expected in enumerate([1,0,1] if blood else [0,0,0]):
                    self.assertEqual(sum(a[0]==i and a[1]=='drag' for a in result['actions']),expected,result)
                retries=[a for a in result['actions'] if a[0]==2 and a[1]!='drag']
                self.assertEqual([a[1] for a in retries],['ignored','entry'])
                self.assertLess(retries[1][-1]-retries[0][-1],10,'Must retry before the old 15s timeout')
                self.assertEqual(len(result['rounds'][1]['calls']),4,result['rounds'][1])
                print('STAGE ENTRY IPC',name,json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    if len(sys.argv)>2 and sys.argv[1]=='--case':native_case(sys.argv[2],'--deadline' in sys.argv[3:])
    else:unittest.main()
