"""Supplied captures + native memory controller; no window or desktop input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = Path(os.environ.get('ARENA_SCREENSHOT_DIR', '__not_configured__'))
FILES = {
    'arena': 'd33f11c6-2661-463e-a5a8-52637ca04b71',
    'arena2': '9f6f56b2-6585-4b2f-8d9d-c0d8db15c4d1',
    'arena3': '806de228-1827-47e2-8f0b-8bb55d0158e3',
    'menu': '084645a1-0ef6-463d-bfb3-b03d74c43b16',
    'menu2': 'fd7534a6-a3ed-4b9a-86c7-efe658e50e2f',
    'stage': '55c34c29-92a2-416a-82b6-71252b2c84d9',
    'success': '335d68ae-c8b7-4047-bc31-b3bcfd0c4cad',
    'success2': 'a61f1a38-49ce-4330-a18f-d6292139c798',
    'world': '87c2625a-dea6-4af2-b1f4-20a33465c198',
    'town6': '787ed47b-341e-4bcd-a77d-662ed4627add',
    'map_current': 'd764c69d-097b-44c5-8339-3c150d3b19be',
    'menu_current': '97f4eb1b-d7bd-4101-9997-657f6a1faf92',
}


def native_case():
    import cv2
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.custom_action import CustomAction
    from maa.resource import Resource
    from maa.tasker import Tasker
    sys.path.insert(0, str(ROOT/'agent'))
    from battle_ui import NoRevivePrompt
    from navigation.catalog import task_routine
    from navigation.runtime import Navigator
    from task_catalog import task_catalog
    sys.path.insert(0, str(ROOT/'tests'))
    from fixture_pixels import read_png_bgr
    frames = {}
    for name, identifier in FILES.items():
        raw = cv2.imdecode(np.fromfile(CAPTURES/f'codex-clipboard-{identifier}.png', dtype=np.uint8), 1)
        viewport = raw if name.endswith('_current') else raw[39:776, 1:1176]
        frames[name] = cv2.resize(viewport, (1147,720), interpolation=cv2.INTER_AREA)
    frames['map_fixture'] = read_png_bgr(ROOT/'tests/fixtures/navigation/map-a.png')
    # The incident frame is already in controller coordinates (1148 x 720).
    # Keep its one-pixel displacement. Resizing here would hide the regression.
    frames['arena_original'] = frames['arena']
    frames['arena'] = read_png_bgr(ROOT/'tests/fixtures/navigation/regressions/arena-lobby.png')
    # Reconstruct only the public result controls, at their captured positions.
    defeat = np.zeros_like(frames['arena'])
    crops = json.loads((ROOT/'agent/data/navigation/arena-defeat-assets.json').read_text(encoding='utf8'))
    for asset in crops['assets']:
        left, top, right, bottom = asset['crop']
        defeat[top:bottom,left:right] = read_png_bgr(ROOT/'assets/resource/base/image'/asset['template'])
    frames['defeat'] = defeat
    task = next(t for t in task_catalog(ROOT, bindings={}) if t['entry']=='Arena')
    params = task['management_profile']['parameters']
    results, errors = [], []

    class Game(CustomController):
        scene = 'arena'
        result_scene = 'success'
        def __init__(self): super().__init__(); self.inputs = []
        def connect(self): return True
        def request_uuid(self): return 'arena-memory-only'
        def screencap(self):
            if self.scene == 'settling':
                self.scene = 'success'
                return frames['stage'].copy()
            return frames[self.scene].copy()
        def touch_down(self, contact, x, y, pressure):
            self.point = (x,y)
            self.inputs.append(('down',self.scene,x,y))
            return True
        def touch_up(self, contact):
            if self.scene in ('map_current', 'map_fixture'):
                assert 845<=self.point[0]<=880 and 35<=self.point[1]<=75, self.point
                self.scene='menu_current'
            elif self.scene in ('menu', 'menu_current'):
                assert 777<=self.point[0]<=850 and 180<=self.point[1]<=235, self.point
                self.scene='arena'
            elif self.scene=='arena': self.scene='stage'
            elif self.scene in ('success','defeat'):
                assert 475 <= self.point[0] < 685 and 540 <= self.point[1] < 640, self.point
                self.scene='arena'
            else: raise AssertionError(('Unexpected exit/input', self.scene, self.point))
            return True
        def key_down(self, key): self.inputs.append(('key_down',key)); return True
        def key_up(self, key):
            self.inputs.append(('key_up',key))
            self.scene=self.result_scene
            return True
        def forbidden(self,*args): raise AssertionError('Unexpected controller input')
        click=swipe=touch_move=input_text=start_app=stop_app=forbidden

    controller = Game()
    class Inspect(CustomAction):
        def run(self, ctx, argv):
            checks = {
                'arena': ['Nav_See_arena','Arena_Target'],
                'arena_original': ['Nav_See_arena','Arena_Target'],
                'arena2': ['Nav_See_arena'], 'arena3': ['Nav_See_arena'],
                'menu': ['Nav_See_daily_menu','Nav_Go_daily_menu_arena'],
                'menu2': ['Nav_See_daily_menu','Nav_Go_daily_menu_arena'],
                'map_current': ['Nav_ClearMap','Nav_Go_map_daily_menu'],
                'map_fixture': ['Nav_ClearMap','Nav_Go_map_daily_menu'],
                'menu_current': ['Nav_See_daily_menu','Nav_Go_daily_menu_arena'],
                'stage': ['Nav_InStage'],
                'success': ['Result_Succeeded','Result_SucceededReturn'],
                'success2': ['Result_Succeeded','Result_SucceededReturn'],
                'defeat': ['Result_Failed','Result_FailedClear','Result_FailedReturn'],
                'world': ['Nav_World_town6'],
                'town6': ['Mainline_063_Target','Mainline_064_Target','Mainline_065_Target'],
            }
            for scene, nodes in checks.items():
                for name in nodes:
                    hit = ctx.run_recognition(name, frames[scene])
                    if not hit or not hit.hit: errors.append((scene,name))
            for missing_rows in (slice(0, 100), slice(100, 200)):
                partial = frames['arena'].copy()
                partial[missing_rows] = 0
                if ctx.run_recognition('Arena_Target', partial).hit:
                    errors.append(('incomplete lobby authorized an opponent', str(missing_rows)))
            if errors:
                return True  # Report failed recognitions before attempting navigation.
            # The menu is still over the map HUD. Its down arrow must never be
            # mistaken for the closed menu's up arrow (which would collapse it).
            assert not ctx.run_recognition('Nav_Go_map_daily_menu',frames['menu_current']).hit
            # A resampled icon shifts its red highlight by a pixel. The colour
            # guard follows that icon, but still rejects a missing highlight.
            current = ctx.run_recognition('Nav_Go_daily_menu_arena',frames['menu_current'])
            if current.hit:
                x,y,w,h=current.box
                missing_red=frames['menu_current'].copy()
                missing_red[y+17:y+27,x+46:x+56]=0
                assert not ctx.run_recognition('Nav_Go_daily_menu_arena',missing_red).hit
            for scene in ('stage','arena','menu','defeat'):
                if ctx.run_recognition('Result_Succeeded',frames[scene]).hit:
                    errors.append(('false_victory',scene))
            for scene in ('success','success2','stage','arena','menu'):
                assert not ctx.run_recognition('Result_Failed',frames[scene]).hit, scene
            for rows in (slice(0,140),slice(540,640)):
                incomplete = defeat.copy()
                incomplete[rows] = 0
                assert not ctx.run_recognition('Result_FailedReturn',incomplete).hit
            for initial, expected_scenes in [('menu',['menu']), ('menu_current',['menu_current']),
                                             ('map_current',['map_current','menu_current']),
                                             ('map_fixture',['map_fixture','menu_current'])]:
                controller.scene=initial;controller.inputs=[];captures=[]
                def capture():
                    captures.append(controller.scene)
                    return controller.screencap()
                nav=Navigator(ctx,task_routine(ctx,params),capture=capture,check=lambda:None)
                try:
                    assert nav.run()=='task_destination'
                    assert [i[1] for i in controller.inputs]==expected_scenes,controller.inputs
                    assert captures==expected_scenes+['arena'],captures
                except Exception as error: errors.append(('menu_navigation',initial,str(error)))
            interface=json.loads((ROOT/'assets/interface.json').read_text(encoding='utf8'))
            for option in interface['option']['竞技场对手']['cases']:
                controller.scene='arena'
                ctx.override_pipeline(option['pipeline_override'])
                nav=Navigator(ctx,task_routine(ctx,params),capture=controller.screencap,check=lambda:None)
                assert nav.perform('Arena_Target',center=True)
                expected=option['pipeline_override']['Arena_Target']['action']['param']['target'][:2]
                if list(controller.point)!=expected: errors.append(('opponent',controller.point,expected))
            return True

    with tempfile.TemporaryDirectory(prefix='arena-memory-',ignore_cleanup_errors=True) as temp:
        directory=Path(temp)
        Tasker.set_log_dir(directory/'logs')
        resource,tasker=Resource(),Tasker()
        resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded
        assert tasker.bind(resource,controller)
        resource.register_custom_action('__arena_inspect',Inspect())
        assert tasker.post_task('__ArenaInspect',{'__ArenaInspect':dict(action=dict(type='Custom',param=dict(custom_action='__arena_inspect')))}).wait().succeeded
        assert not errors, errors
        library=directory/'recordings';library.mkdir()
        recording=library/'fixture.json'
        recording.write_text(json.dumps(dict(version=1,name='memory',base=[1147,720],duration_ms=1,
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
script_task.STATUS_DIRECTORY=Path.cwd()/'status'
battle_player.RECORDING_DIR=str(Path.cwd()/'recordings')
AgentServer.custom_recognition('battle_no_revive')(NoRevivePrompt)
AgentServer.start_up(sys.argv[1]);AgentServer.join();AgentServer.shut_down()
''',encoding='utf8')
        client=AgentClient();client.set_timeout(30000);assert client.bind(resource)
        with (directory/'agent.log').open('wb') as output:
            child=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(server),client.identifier,str(ROOT/'agent')],
                cwd=directory,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert client.connect()
                for scene,expected in [('success','success'),('settling','success'),('defeat','fatal'),
                                       ('stage','failed'),('arena','failed')]:
                    controller.scene='map_current';controller.result_scene=scene;controller.inputs=[]
                    run_id=str(uuid4())
                    configured=dict(params,file=str(recording),run_id=run_id)
                    patch={'Arena':dict(action=dict(param=dict(custom_action_param=configured))),
                           'Arena_Target':dict(action=dict(param=dict(target=[1022,273,1,1]))),
                           'Arena_Play':dict(action=dict(param=dict(custom_action_param=dict(file=str(recording))))),
                           '脚本任务设置':dict(action=dict(param=dict(custom_action_param=dict(afk_mode=False,failure_retry=dict(enabled=False,max_retries=3)))))}
                    tasker.post_task('Arena',patch).wait()
                    status=json.loads((directory/'status'/(run_id+'.json')).read_text(encoding='utf8'))
                    assert status['status']==expected, (status,(directory/'agent.log').read_text(encoding='utf8',errors='replace')[-5000:])
                    downs=[i for i in controller.inputs if i[0]=='down']
                    assert [i[1] for i in downs[:3]]==['map_current','menu_current','arena'],controller.inputs
                    assert downs[2]==('down','arena',1022,273),controller.inputs
                    assert len(downs)==(4 if scene in ('success','settling','defeat') else 3),controller.inputs
                    if scene=='defeat':
                        assert status['reason']=='battle_failed' and not status['victory_observed']
                    results.append(dict(scene=scene,status=status['status'],reason=status['reason']))
            finally: child.terminate();child.wait(timeout=5)
        print('ARENA_NATIVE '+json.dumps(dict(rounds=results,errors=errors)),flush=True)
        os._exit(0)


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1' and CAPTURES.is_dir(),
                     'requires supplied captures and memory-only native Agent tests')
class ArenaNativeTests(unittest.TestCase):
    def test_navigation_opponents_results_and_shared_round_ipc(self):
        run=subprocess.run([sys.executable,'-B','-E','-s','-u','-X','utf8',str(Path(__file__).resolve()),'--native-case'],
            cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=180)
        self.assertEqual(run.returncode,0,run.stdout[-6000:]+run.stderr[-6000:])
        print(next(line for line in run.stdout.splitlines() if line.startswith('ARENA_NATIVE ')),flush=True)


if __name__=='__main__':
    if '--native-case' in sys.argv: native_case()
    else: unittest.main()
