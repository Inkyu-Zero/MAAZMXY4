"""All mainline profiles across real Agent IPC with a memory-only controller."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]


def native_case(buzhou_only=False,shared_only=False):
    from test_shared_stage_entry import with_stage_controls
    import cv2
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from test_mainline_native import map_frame,SCENES
    sys.path.insert(0,str(ROOT/'agent'))
    from task_catalog import task_catalog
    stages=json.loads((ROOT/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf8'))['stages']
    cases=[(stage,False,False,None) for stage in stages]
    cases.extend([(stages[0],True,False,None),(stages[1],False,True,None)])
    if buzhou_only:
        stage=next(stage for stage in stages if stage['id']==46)
        cases=[(stage,False,False,ROOT/f'tests/fixtures/navigation/buzhou-live/s046-phase-{phase}.png') for phase in (1,2)]
        cases.append((stage,False,True,None))
    if shared_only:
        cases=[(stages[33],False,False,None),(stages[0],True,False,None),(stages[45],False,True,None)]
    catalog={t['entry']:t for t in task_catalog(ROOT,bindings={})}
    class Game(CustomController):
        scene='map';plays=0;ignored=False;wrong=False
        def __init__(self):super().__init__();self.inputs=[];self.captures=[]
        def connect(self):return True
        def request_uuid(self):return 'mainline-round-memory-only'
        def screencap(self):
            self.captures.append((self.scene,len(self.inputs)))
            return self.frames[self.scene].copy()
        def touch_down(self,contact,x,y,pressure):
            assert self.scene=='map','Must not click an unrecognized scene'
            self.inputs.append(('down',x,y));return True
        def touch_up(self,contact):
            self.inputs.append(('up',))
            if self.ignored:self.ignored=False
            else:self.scene='wrong' if self.wrong else 'stage'
            return True
        def key_down(self,key):self.inputs.append(('key_down',key));return True
        def key_up(self,key):
            self.inputs.append(('key_up',key))
            if key==65:self.plays+=1;self.scene='map'
            return True
        def forbidden(self,*args):raise AssertionError('No desktop input or application launch')
        click=swipe=touch_move=input_text=start_app=stop_app=forbidden
    with tempfile.TemporaryDirectory(prefix='mainline-round-ipc-',ignore_cleanup_errors=True) as temp:
        directory=Path(temp);library=directory/'recordings';library.mkdir()
        Tasker.set_log_dir(directory/'logs')
        recording=library/'fixture.json'
        recording.write_text(json.dumps(dict(version=1,name='Memory fixture',base=[1147,720],duration_ms=1,
            actions=[dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])),encoding='utf8')
        server=directory/'server.py'
        server.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from maa.agent.agent_server import AgentServer
import battle_player,script_task
from battle_ui import NoRevivePrompt
script_task.ROOT=Path.cwd()
script_task.RECORDINGS=Path.cwd()/'recordings'
script_task.STATUS_DIRECTORY=Path.cwd()/'debug/task-runs'
battle_player.RECORDING_DIR=str(Path.cwd()/'recordings')
AgentServer.custom_recognition('battle_no_revive')(NoRevivePrompt)
AgentServer.start_up(sys.argv[1]);AgentServer.join();AgentServer.shut_down()
''',encoding='utf8')
        resource,controller,tasker=Resource(),Game(),Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded
        assert tasker.bind(resource,controller)
        client=AgentClient();client.set_timeout(30000);assert client.bind(resource)
        results=[]
        with (directory/'agent.log').open('wb') as output:
            child=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(server),client.identifier,str(ROOT/'agent')],
                cwd=directory,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert client.connect()
                for stage,ignored,wrong,scene_path in cases:
                    entry=f'Mainline_{stage["id"]:03d}';task=catalog[entry]
                    params=task['management_profile']['parameters'].copy();run=str(uuid4())
                    params.update(file=str(recording),run_id=run,exit_mode='recorded_return')
                    params['round_flow']=dict(params['round_flow'],load_timeout_ms=1000 if wrong else 15000)
                    controller.scene='map';controller.plays=0;controller.inputs=[];controller.captures=[]
                    controller.ignored=ignored;controller.wrong=wrong
                    controller.frames=dict(map=map_frame(stage),
                        stage=cv2.resize(cv2.imread(str(SCENES/f's{stage["id"]:03d}.png')),(1147,720)))
                    if scene_path is not None:
                        from fixture_pixels import read_png_bgr
                        controller.frames['stage']=read_png_bgr(scene_path)
                    if wrong:controller.frames['wrong']=cv2.resize(cv2.imread(str(SCENES/'s047.png')),(1147,720))
                    for key in ('stage','wrong'):
                        if key in controller.frames:controller.frames[key]=with_stage_controls(controller.frames[key])
                    patch={entry:dict(action=dict(param=dict(custom_action_param=params)),max_hit=1),
                        entry+'_Play':dict(action=dict(param=dict(custom_action_param=dict(file=str(recording))))),
                        '脚本任务设置':dict(action=dict(param=dict(custom_action_param=dict(exit_mode='recorded_return',afk_mode=False,
                            failure_retry=dict(enabled=False,max_retries=3)))))}
                    success=tasker.post_task(entry,patch).wait().succeeded
                    path=directory/'debug/task-runs'/(run+'.json')
                    status=json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
                    results.append(dict(stage=stage['id'],success=success,plays=controller.plays,
                        status=status.get('status'),reason=status.get('reason'),captures=len(controller.captures),
                        entry_presses=sum(event[0]=='down' for event in controller.inputs),case='wrong' if wrong else 'retry' if ignored else 'normal'))
                    print('MAINLINE_ROUND '+json.dumps(results[-1]),flush=True)
                    assert controller.plays==1, (results[-1],(directory/'agent.log').read_text(encoding='utf8',errors='replace')[-3000:])
                    assert success,results[-1]
            finally:child.terminate();child.wait(timeout=5)
        print('RESULT '+json.dumps(results),flush=True)
        os._exit(0)


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1' and os.environ.get('MAINLINE_ASSET_SOURCE') and os.environ.get('MAINLINE_SCENE_FIXTURES'),
                     'requires offline fixtures and explicitly enabled memory-only Agent IPC')
class MainlineTransportTests(unittest.TestCase):
    def test_huanjing_retry_and_different_stage_use_shared_controls(self):
        run=subprocess.run([sys.executable,'-B','-E','-s','-u','-X','utf8',str(Path(__file__).resolve()),'--shared-case'],
            cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=90)
        self.assertEqual(run.returncode,0,run.stdout[-4000:]+run.stderr[-4000:])
        evidence=json.loads(next(line[7:] for line in run.stdout.splitlines() if line.startswith('RESULT ')))
        self.assertEqual([row['stage'] for row in evidence],[34,1,46])
        self.assertTrue(all(row['status']=='success' and row['plays']==1 for row in evidence),evidence)
        self.assertEqual(evidence[1]['entry_presses'],2)
        print('SHARED_STAGE_IPC '+json.dumps(evidence),flush=True)

    def test_buzhou_backgrounds_with_shared_hud_and_other_stage_complete_round(self):
        run=subprocess.run([sys.executable,'-B','-E','-s','-u','-X','utf8',str(Path(__file__).resolve()),'--buzhou-case'],
            cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=90)
        self.assertEqual(run.returncode,0,run.stdout[-4000:]+run.stderr[-4000:])
        line=next((line[7:] for line in run.stdout.splitlines() if line.startswith('RESULT ')),None)
        self.assertIsNotNone(line,run.stdout[-4000:])
        evidence=json.loads(line)
        self.assertEqual(len(evidence),3)
        self.assertTrue(all(row['stage']==46 for row in evidence))
        self.assertTrue(all(row['status']=='success' and row['plays']==1 and row['entry_presses']==1 for row in evidence[:2]),evidence)
        self.assertEqual(evidence[-1]['plays'],1)
        self.assertEqual(evidence[-1]['status'],'success')
        print('BUZHOU_IPC '+line,flush=True)

    def test_all_profiles_complete_round_without_scene_identity_requirement(self):
        run=subprocess.run([sys.executable,'-B','-E','-s','-u','-X','utf8',str(Path(__file__).resolve()),'--native-case'],
            cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=600)
        self.assertEqual(run.returncode,0,run.stdout[-4000:]+run.stderr[-4000:])
        line=next((line[7:] for line in run.stdout.splitlines() if line.startswith('RESULT ')),None)
        self.assertIsNotNone(line,run.stdout[-4000:])
        evidence=json.loads(line)
        self.assertEqual(len(evidence),64)
        self.assertTrue(all(row['status']=='success' for row in evidence[:63]))
        self.assertEqual(evidence[-2]['entry_presses'],2)
        self.assertEqual(evidence[-1]['plays'],1)
        print('MAINLINE_IPC '+line,flush=True)


if __name__=='__main__':
    if '--shared-case' in sys.argv:native_case(shared_only=True)
    elif '--buzhou-case' in sys.argv:native_case(buzhou_only=True)
    elif '--native-case' in sys.argv:native_case()
    else:unittest.main()
