"""Opt-in recognition on packaged pixels; every input callback is forbidden."""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '悬赏（守卫宝烛）-'


def panel_title(frame, title):
    """Synthetic description text from game config; not live game evidence."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    image=Image.fromarray(frame[:,:,::-1].copy())
    draw=ImageDraw.Draw(image);font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',24)
    draw.rectangle((500,175,930,235),fill=(31,43,52))
    draw.text((665,185),title,fill='white',font=font)
    descriptions=json.loads((ROOT/'agent/data/navigation/bounty-descriptions.json').read_text(encoding='utf8'))
    draw.rectangle((510,228,940,322),fill=(230,193,135))
    for i,phrase in enumerate(descriptions['tasks'][title]):
        draw.text((520,235+i*29),phrase,fill='black',font=font)
    return np.asarray(image)[:,:,::-1].copy()


def quest_frames(frames):
    """Keep real tabs and badge; main-quest text reproduces the logged collision."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from fixture_pixels import read_png_bgr
    header=read_png_bgr(ROOT/'tests/fixtures/navigation/quest-taizong-real.png')
    frames['quest-map']=read_png_bgr(ROOT/'tests/fixtures/navigation/map-a.png')
    frames['quest-tab'][55:175,175:980]=header[55:175,175:980]
    panel=frames['quest-candle']
    panel[55:175,175:980]=header[55:175,175:980]
    panel[175:215,180:500]=header[175:215,180:500]
    panel[215:265,410:520]=panel[300,400]
    panel[220:260,420:486]=read_png_bgr(ROOT/'tests/fixtures/bounty-candle/accepted-badge.png')
    image=Image.fromarray(header[:,:,::-1].copy())
    draw=ImageDraw.Draw(image);font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',22)
    draw.rectangle((200,178,495,215),fill=(230,193,135))
    draw.text((208,182),'完成主线任务，获得高额奖励',fill='black',font=font)
    draw.rectangle((215,220,410,258),fill=(255,172,42))
    draw.text((221,222),'日月星河',fill='black',font=font)
    draw.rectangle((640,182,820,220),fill=(230,193,135))
    draw.text((665,185),'日月星河',fill='black',font=font)
    frames['quest-main']=np.asarray(image)[:,:,::-1].copy()
    frames['quest-candle']=panel_title(frames['quest-candle'],'守卫宝烛')
    return frames


@unittest.skipUnless(os.environ.get('CANDLE_NATIVE_TESTS') == '1', 'opt-in Maa recognition test')
class NativeCandleRecognitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(ROOT/'agent'))

    def test_current_ocr_and_native_pipeline_on_packaged_fixtures(self):
        os.environ.setdefault('MAAFW_BINARY_PATH', str(ROOT / 'runtimes/win-x64/native'))
        import numpy as np
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr

        directory = ROOT / 'tests/fixtures/bounty-candle'
        frames = {path.stem: read_png_bgr(path) for path in directory.glob('*.png')}
        quest_frames(frames)
        pipeline = json.loads((ROOT / 'assets/resource/base/pipeline/悬赏（守卫宝烛）.json').read_text(encoding='utf8'))
        results = []

        class PixelsOnly(CustomController):
            def connect(self): return True
            def request_uuid(self): return 'native-candle-offline-fixtures'
            def screencap(self): return frames['world'].copy()
            def forbidden(self, *args): raise AssertionError('Real input is forbidden')
            start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = forbidden
            click_key = input_text = key_down = key_up = forbidden

        class Inspect(CustomAction):
            def run(self, context, argv):
                from navigation.catalog import task_routine
                task_routine(context,dict(navigation=dict(kind='bounty',title='守卫宝烛',target_node='Bounty_Target')))
                checks=[('Bounty_Target','quest-candle',True),('Bounty_Target','quest-tab',False),
                        ('Bounty_Target','quest-main',False),('Nav_QuestPanel','quest-main',True),
                        ('Nav_AcceptedBounties','quest-main',False),('Nav_SelectBountyTab','quest-main',True),
                        ('Bounty_Target','world',False),('Bounty_Target','saves',False),
                        ('Bounty_Play','candle-battle-ready',True),('Bounty_Play','world',False),
                        ('Bounty_StageSettings','candle-battle-settings',True),
                        ('Bounty_StageSettings','candle-battle-ready',False),
                        ('Nav_ClearMap','quest-map',True),('Nav_ClearMap','world',False),
                        ('Bounty_ResumeStage','candle-battle-paused',True),
                        ('Bounty_StagePause','candle-battle-paused',True)]
                for name,frame,expected in checks:
                    result=context.run_recognition(name,frames[frame])
                    actual=bool(result and result.hit)
                    results.append(dict(check=name+':'+frame,actual=actual,expected=expected,ok=actual==expected))
                for title in ('守卫太宗','追捕飞贼','遁地大盗'):
                    wrapper=json.loads((ROOT/'assets/resource/base/pipeline'/('悬赏（'+title+'）.json')).read_text(encoding='utf8'))
                    params=next(n['action']['param']['custom_action_param'] for n in wrapper.values()
                        if n.get('action',{}).get('param',{}).get('custom_action_param',{}).get('phase')=='begin')
                    task_routine(context,params)
                    for label,pixels,expected in [('wrong candle',frames['quest-candle'],False),
                        ('own detail',panel_title(frames['quest-candle'],title),True)]:
                        result=context.run_recognition('Bounty_Target',pixels)
                        actual=bool(result and result.hit)
                        results.append(dict(check=title+':'+label,actual=actual,expected=expected,ok=actual==expected))
                # Real user screenshot: title/body overlap must not reject Taizong.
                from fixture_pixels import read_png_bgr
                task_routine(context,dict(navigation=dict(kind='bounty',title='守卫太宗',target_node='Bounty_Target')))
                result=context.run_recognition('Bounty_Target',read_png_bgr(ROOT/'tests/fixtures/navigation/quest-taizong-real.png'))
                results.append(dict(check='real taizong description',actual=bool(result and result.hit),expected=True,ok=bool(result and result.hit)))
                return True

        log = Path(os.environ.get('MAAZMXY4_TEST_LOG_DIR', ROOT / '.runtime-cache/native-candle-recognition'))
        log.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(log)
        controller, resource, tasker = PixelsOnly(), Resource(), Tasker()
        sys.path.insert(0,str(ROOT/'agent'))
        from battle_ui import NoRevivePrompt
        resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
        self.assertTrue(controller.post_connection().wait().succeeded)
        self.assertTrue(controller.set_screenshot_use_raw_size(True))
        self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
        self.assertTrue(resource.override_pipeline({'__NativeCandleInspect': {
            'action': {'type': 'Custom', 'param': {'custom_action': '__inspect_candle'}}}}))
        self.assertTrue(resource.register_custom_action('__inspect_candle', Inspect()))
        self.assertTrue(tasker.bind(resource, controller))
        self.assertTrue(tasker.post_task('__NativeCandleInspect').wait().succeeded)
        (log / 'checks.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf8')
        self.assertGreaterEqual(len(results), 12)
        self.assertEqual([item for item in results if not item['ok']], [])

    def test_native_round_executes_saved_library_file_and_confirms_return(self):
        """Run the real engine and actions against a simulated screen/controller."""
        import importlib.util
        import sys
        import tempfile
        from types import ModuleType
        from unittest.mock import patch
        from uuid import uuid4

        os.environ.setdefault('MAAFW_BINARY_PATH', str(ROOT / 'runtimes/win-x64/native'))
        from maa.controller import CustomController
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr

        registration = ModuleType('maa.agent.agent_server')
        registration.AgentServer = type('RegistrationOnly', (), {
            'custom_action': staticmethod(lambda _name: lambda implementation: implementation)})
        def load(name, filename):
            spec = importlib.util.spec_from_file_location(name, ROOT / 'agent' / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        with patch.dict(sys.modules, {'maa.agent': ModuleType('maa.agent'), 'maa.agent.agent_server': registration}), \
                patch.object(sys, 'path', [str(ROOT / 'agent'), *sys.path]):
            state_actions = load('native_fixture_candle_actions', 'script_task.py')
            playback = load('native_fixture_battle_player', 'battle_player.py')
            from script_library import bind_task_recording

        module_patch = patch.dict(sys.modules, {'battle_player': playback})
        module_patch.start(); self.addCleanup(module_patch.stop)
        frames = {name: read_png_bgr(ROOT / 'tests/fixtures/bounty-candle' / (name + '.png'))
                  for name in ('quest-map', 'quest-tab', 'quest-candle', 'candle-battle-ready', 'candle-battle-settings',
                               'login', 'saves-slot4', 'world')}
        quest_frames(frames)
        candle_panel=frames['quest-candle'].copy()
        events = []
        clicked_screens=[]
        captures=[]

        class SimulatedGame(CustomController):
            stage = 'quest-map'
            open_settings_on_entry = False
            return_stage = 'quest-map'
            def connect(self): return True
            def request_uuid(self): return 'native-candle-round-simulation'
            def screencap(self):
                captures.append(dict(stage=self.stage,after_inputs=len(events)))
                return frames[self.stage].copy()
            def touch_down(self, contact, x, y, pressure):
                events.append(('mouse_down', contact, x, y))
                clicked_screens.append(self.stage)
                if self.stage=='quest-main' and not (310<x<430 and 120<y<180):
                    raise AssertionError('Must click the bounty tab, never the main-quest stage button')
                self.last_down = (x, y)
                return True
            def touch_move(self, contact, x, y, pressure): return True
            def touch_up(self, contact):
                events.append(('mouse_up', contact))
                if self.stage.startswith('map:'): self.stage = 'stage:' + self.stage[4:]
                elif self.stage == 'quest-map': self.stage = 'quest-main'
                elif self.stage == 'quest-main': self.stage = 'quest-candle'
                elif self.stage == 'quest-tab': self.stage = 'quest-candle'
                elif self.stage == 'quest-candle':
                    self.stage = 'candle-battle-settings' if self.open_settings_on_entry else 'candle-battle-ready'
                elif self.stage == 'login': self.stage = 'saves-slot4'
                elif self.stage == 'saves-slot4':
                    x, y = self.last_down
                    if not (560 < x < 900 and 260 < y < 390):
                        raise AssertionError('Recovery clicked a different save slot')
                    self.stage = 'quest-map'
                return True
            def key_down(self, keycode):
                events.append(('key_down', keycode))
                return True
            def key_up(self, keycode):
                events.append(('key_up', keycode))
                self.stage = self.return_stage
                return True
            def forbidden(self, *args): raise AssertionError('Unexpected simulated device action')
            start_app = stop_app = click = swipe = click_key = input_text = forbidden

        with tempfile.TemporaryDirectory(prefix='native-candle-round-') as temporary:
            directory = Path(temporary)
            library, checkpoints = directory / 'recordings', directory / 'debug/task-runs'
            library.mkdir()
            checkpoints.mkdir(parents=True)
            filename = 'native-round.json'
            script = dict(version=1, name='Memory controller round', base=[1148,720], duration_ms=3,
                          actions=[dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])
            raw = json.dumps(script).encode('utf8')
            (library / filename).write_bytes(raw)
            import hashlib
            bind_task_recording(library, 'bounty-candle', filename, expected_revision=hashlib.sha256(raw).hexdigest())
            state_actions.ROOT = directory
            state_actions.RECORDINGS, state_actions.STATUS_DIRECTORY = library, checkpoints
            playback.RECORDING_DIR = str(library)
            playback._toast = lambda *_args: None
            run_id = str(uuid4())
            parameters = dict(phase='begin', file='@task:bounty-candle', exit_mode='recorded_return',
                              run_id=run_id, instance_id='memory-test', status_path=str(checkpoints / (run_id + '.json')))
            canonical = json.loads((ROOT/'assets/resource/base/pipeline/悬赏（守卫宝烛）.json').read_text(encoding='utf8'))
            parameters = {**canonical[PREFIX+'初始化']['action']['param']['custom_action_param'], **parameters}
            controller, resource, tasker = SimulatedGame(), Resource(), Tasker()
            from battle_ui import NoRevivePrompt
            resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
            self.assertTrue(controller.post_connection().wait().succeeded)
            self.assertTrue(controller.set_screenshot_use_raw_size(True))
            self.assertTrue(resource.post_bundle(ROOT / 'assets/resource/base').wait().succeeded)
            self.assertTrue(resource.register_custom_action('script_task_state', state_actions.ScriptTaskState()))
            self.assertTrue(resource.register_custom_action('battle_play', playback.BattlePlay()))
            self.assertTrue(tasker.bind(resource, controller))
            overrides = {PREFIX + '初始化': {'action': {'param': {'custom_action_param': parameters}}}}
            self.assertTrue(tasker.post_task(PREFIX + '初始化', overrides).wait().succeeded)
            checkpoint = json.loads((checkpoints / (run_id + '.json')).read_text(encoding='utf8'))
            self.assertEqual(checkpoint['status'], 'success')
            self.assertEqual(checkpoint['recording_file'], filename)
            self.assertEqual([event for event in events if event[0].startswith('key_')], [('key_down',65),('key_up',65)])
            self.assertEqual(len([event for event in events if event[0]=='mouse_down']), 3)
            self.assertEqual(clicked_screens,['quest-map','quest-main','quest-candle'])
            if os.environ.get('MAA_FRAME_BUDGET_REPORT'):
                Path(os.environ['MAA_FRAME_BUDGET_REPORT']).write_text(json.dumps(
                    dict(captures=captures,total_captures=len(captures),inputs=events),ensure_ascii=False,indent=2),encoding='utf8')
            # Save an edit to the same file, without rebinding or creating a copy.
            script['actions'] = [dict(t=0,op='keydown',key=66),dict(t=1,op='keyup',key=66)]
            (library / filename).write_text(json.dumps(script), encoding='utf8')
            events.clear()
            controller.stage = 'quest-map'
            run_id = str(uuid4())
            parameters.update(run_id=run_id, status_path=str(checkpoints / (run_id + '.json')))
            self.assertTrue(tasker.post_task(PREFIX + '初始化', overrides).wait().succeeded)
            checkpoint = json.loads((checkpoints / (run_id + '.json')).read_text(encoding='utf8'))
            self.assertEqual(checkpoint['status'], 'success')
            self.assertEqual([event for event in events if event[0].startswith('key_')], [('key_down',66),('key_up',66)])
            # Four bindings share the lifecycle, but each requires its own detail title.
            from task_catalog import task_catalog
            profiles=[t for t in task_catalog(ROOT,bindings={}) if t['name'].startswith('悬赏（')]
            for index,task in enumerate(profiles):
                key=70+index
                path=library/('profile-'+str(index)+'.json')
                data=dict(version=1,name='Shared profile',base=[1148,720],duration_ms=3,
                          actions=[dict(t=0,op='keydown',key=key),dict(t=1,op='keyup',key=key)])
                raw=json.dumps(data).encode('utf8');path.write_bytes(raw)
                bind_task_recording(library,task['id'],path.name,expected_revision=hashlib.sha256(raw).hexdigest())
                profile=task['management_profile'];params=dict(profile['parameters'])
                frames['quest-candle']=panel_title(candle_panel,params['navigation']['title'])
                ident=str(uuid4());params.update(run_id=ident,status_path=str(checkpoints/(ident+'.json')),exit_mode='recorded_return')
                events.clear();controller.stage='quest-map'
                patch={profile['entry']:{'action':{'param':{'custom_action_param':params}}}}
                patch.setdefault(profile['success_node'],{})['next']=[]
                self.assertTrue(tasker.post_task(profile['entry'],patch).wait().succeeded,task['name'])
                self.assertEqual([e[1] for e in events if e[0]=='key_down'],[key],task['name'])
                status=json.loads((checkpoints/(ident+'.json')).read_text(encoding='utf8'))
                self.assertEqual(status['status'],'success');self.assertEqual(status['recording_file'],path.name)

            # Seven ordinary stages use the same coordinator and their own
            # real map fixtures and a shared HUD on synthetic blank canvases.
            import numpy as np
            cases=json.loads((ROOT/'tests/fixtures/navigation/stage-states/cases.json').read_text(encoding='utf8'))
            stages=[t for t in task_catalog(ROOT,bindings={}) if t['management_profile']
                    and t['management_profile']['parameters']['navigation']['kind']=='stage'
                    and t['name'] in {case['task'] for case in cases}]
            self.assertEqual(len(stages),7)  # New 62 mainline stages have their own fixture suite.
            stage_reports=[]
            for index,task in enumerate(stages):
                profile=task['management_profile'];params=dict(profile['parameters'])
                case=next(c for c in cases if c['task']==task['name'])
                map_name='map:'+str(index);stage_name='stage:'+str(index)
                from fixture_pixels import read_stage_state
                frames[map_name]=read_stage_state(ROOT/'tests/fixtures/navigation/stage-states',case)
                from test_shared_stage_entry import with_stage_controls
                frames[stage_name]=with_stage_controls(np.zeros_like(frames[map_name]))
                path=library/('stage-'+str(index)+'.json')
                raw=json.dumps(dict(version=1,name='Stage profile',base=[1148,720],duration_ms=3,
                    actions=[dict(t=0,op='keydown',key=80+index),dict(t=1,op='keyup',key=80+index)])).encode('utf8')
                path.write_bytes(raw);bind_task_recording(library,task['id'],path.name,expected_revision=hashlib.sha256(raw).hexdigest())
                ident=str(uuid4());params.update(run_id=ident,status_path=str(checkpoints/(ident+'.json')),exit_mode='recorded_return')
                events.clear();captures.clear();controller.stage=map_name;controller.return_stage=map_name
                patch={profile['entry']:{'action':{'param':{'custom_action_param':params}},'next':[]}}
                self.assertTrue(tasker.post_task(profile['entry'],patch).wait().succeeded,task['name'])
                self.assertEqual([e[1] for e in events if e[0]=='key_down'],[80+index])
                self.assertEqual(len(captures),3,(task['name'],captures))
                stage_reports.append(dict(task=task['name'],captures=list(captures)))
            if os.environ.get('MAA_FRAME_BUDGET_REPORT'):
                Path(os.environ['MAA_FRAME_BUDGET_REPORT']+'.stages.json').write_text(json.dumps(stage_reports,ensure_ascii=False,indent=2),encoding='utf8')
            controller.return_stage='quest-map'

            # The scene template is still visible behind the settings menu.
            # Even so, the real pipeline must stop without replaying a key.
            events.clear()
            frames['quest-candle']=candle_panel
            controller.stage = 'quest-map'
            controller.open_settings_on_entry = True
            run_id = str(uuid4())
            blocked_parameters = dict(canonical[PREFIX+'初始化']['action']['param']['custom_action_param'])
            blocked_parameters.update(phase='begin', file='@task:bounty-candle', exit_mode='recorded_return',
                                      run_id=run_id, instance_id='memory-test',
                                      status_path=str(checkpoints / (run_id + '.json')))
            settings_overrides = {PREFIX + '初始化': {'action': {'param': {'custom_action_param': blocked_parameters}}}}
            blocked_job = tasker.post_task(PREFIX + '初始化', settings_overrides).wait()
            checkpoint = json.loads((checkpoints / (run_id + '.json')).read_text(encoding='utf8'))
            self.assertFalse(blocked_job.succeeded)
            self.assertEqual(checkpoint['status'], 'fatal')
            self.assertEqual(checkpoint['reason'],'stage_settings_open')
            self.assertEqual(controller.stage, 'candle-battle-settings')
            self.assertEqual([event for event in events if event[0].startswith('key_')], [])


if __name__ == '__main__':
    unittest.main()
