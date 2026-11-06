"""Opt-in Maa tests on recorded pixels and a memory-only input controller."""
import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS') == '1', 'opt-in Maa memory-controller test')
class NativeNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(ROOT/'agent'))
        os.environ.setdefault('MAAFW_BINARY_PATH', str(ROOT/'runtimes/win-x64/native'))
        from maa.tasker import Tasker
        from battle_ui import NoRevivePrompt
        cls.no_revive = NoRevivePrompt
        cls.log = ROOT/'.runtime-cache/native-navigation'
        cls.log.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(cls.log)

    def test_native_shortcuts_cross_map_hubs_and_post_click_verification(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        from navigation.catalog import task_routine
        from navigation.runtime import Navigator
        frames={name:read_png_bgr(ROOT/'tests/fixtures/navigation'/(name+'.png'))
                for name in ['profile','quest-taizong-real','map-a','map-b','world','settings-menu']}
        frames['nanzhan']=read_png_bgr(ROOT/'tests/fixtures/navigation/hubs/nanzhan-source.png')
        icon=read_png_bgr(ROOT/'assets/resource/base/image/任务.png')
        frames['nanzhan'][630:630+icon.shape[0],400:400+icon.shape[1]]=icon
        events=[];errors=[]
        class Pixels(CustomController):
            frame='profile';route={}
            def connect(self):return True
            def request_uuid(self):return 'native-candidate-navigation-memory'
            def screencap(self):return frames[self.frame].copy()
            def touch_down(self,contact,x,y,pressure):
                if self.frame=='settings-menu':
                    bx,by,bw,bh=self.return_box
                    assert bx<=x<=bx+bw and by<=y<=by+bh,('continued instead of returning',x,y,self.return_box)
                events.append((self.frame,x,y));return True
            def touch_up(self,contact):
                self.frame=self.route[self.frame];return True
            def touch_move(self,*args):return True
            def forbidden(self,*args):raise AssertionError('Unexpected device input')
            click=swipe=key_down=key_up=input_text=start_app=stop_app=forbidden
        controller=Pixels()
        class Navigate(CustomAction):
            def run(self,context,argv):
                try:
                    if controller.frame=='settings-menu':
                        check=context.run_recognition('Nav_SettingsReturn',frames['settings-menu'])
                        assert check and check.hit
                        controller.return_box=list(check.box)
                    if controller.case=='bounty':
                        assert context.run_recognition('Nav_Nanzhan',frames['nanzhan']).hit
                        assert not context.run_recognition('Nav_Nanzhan',frames['map-a']).hit
                        params=dict(navigation=dict(kind='bounty',title='守卫太宗',target_node='Bounty_Target'))
                    else:
                        params=dict(navigation=dict(kind='stage',target_node='朱电（琉璃宫）-关卡入口'))
                    actual=Navigator(context,task_routine(context,params),capture=controller.screencap,check=lambda:None).run()
                    assert actual=='task_destination'
                    return True
                except Exception:
                    import traceback
                    errors.append(traceback.format_exc());return False
        resource,tasker=Resource(),Tasker()
        resource.register_custom_recognition('battle_no_revive',self.no_revive())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__candidate_navigation',Navigate())
        resource.override_pipeline({'__CandidateNavigation':dict(action=dict(type='Custom',param=dict(custom_action='__candidate_navigation')))})
        self.assertTrue(tasker.bind(resource,controller))
        cases=[('profile','stage',{'profile':'map-a'},1),
               ('settings-menu','stage',{'settings-menu':'map-a'},1),
               ('quest-taizong-real','stage',{'quest-taizong-real':'map-a'},1),
               ('map-b','stage',{'map-b':'world','world':'map-a'},2),
               ('world','bounty',{'world':'nanzhan','nanzhan':'quest-taizong-real'},2)]
        for frame,case,route,count in cases:
            with self.subTest(frame=frame,goal=case):
                controller.frame,controller.case,controller.route=frame,case,route
                events.clear();errors.clear()
                self.assertTrue(tasker.post_task('__CandidateNavigation').wait().succeeded,'\n'.join(errors))
                self.assertEqual(len(events),count)
                if frame=='world':
                    # Source town0 st1 is the bottom island, not the central
                    # Sumeru portal. The memory transition is explicitly simulated.
                    self.assertTrue(397<=events[0][1]<=727 and 460<=events[0][2]<=662,events)
                elif frame=='map-b':
                    self.assertTrue(1053<=events[0][1]<=1143 and 29<=events[0][2]<=121,events)
                    self.assertTrue(865<=events[1][1]<=1112 and 188<=events[1][2]<=454,events)

    def test_revival_linger_and_missing_cancel_wait_for_clear_defeat(self):
        import time
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        from navigation.catalog import task_routine
        from navigation.runtime import Navigator
        prompt=read_png_bgr(ROOT/'tests/fixtures/navigation/revival/normal-p1-up.png')
        missing=prompt.copy();missing[385:550,270:900]=0
        failed=read_png_bgr(ROOT/'tests/fixtures/navigation/challenge-failed.png')
        ready=read_png_bgr(ROOT/'tests/fixtures/navigation/map-a.png')
        events=[];errors=[];durations=[]
        class Pixels(CustomController):
            mode='linger';started=0;cancelled=None;returned=False
            def connect(self):return True
            def request_uuid(self):return 'navigation-revival-wait-memory'
            def screencap(self):
                if self.returned:return ready.copy()
                expired=(time.monotonic()-self.started>=10.1 if self.mode=='missing'
                         else self.cancelled is not None and time.monotonic()-self.cancelled>=.8)
                return failed.copy() if expired else (missing if self.mode=='missing' else prompt).copy()
            def touch_down(self,contact,x,y,pressure):
                if self.mode=='linger' and self.cancelled is None:
                    assert 628<=x<=724 and 429<=y<=470,(x,y)
                    events.append('cancel')
                else:
                    assert 500<=x<=1050 and 450<=y<=650,(x,y)
                    events.append('return')
                return True
            def touch_up(self,*args):
                if events[-1]=='cancel':self.cancelled=time.monotonic()
                else:self.returned=True
                return True
            def touch_move(self,*args):return True
            def forbidden(self,*args):raise AssertionError('Unexpected input')
            click=swipe=key_down=key_up=input_text=start_app=stop_app=forbidden
        controller=Pixels()
        class Navigate(CustomAction):
            def run(self,context,argv):
                try:
                    controller.started=time.monotonic()
                    nav=Navigator(context,task_routine(context,{}),capture=controller.screencap,check=lambda:None)
                    assert nav.run()=='map'
                    durations.append(time.monotonic()-controller.started)
                    return True
                except Exception:
                    import traceback
                    errors.append(traceback.format_exc());return False
        resource,tasker=Resource(),Tasker()
        resource.register_custom_recognition('battle_no_revive',self.no_revive())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__revival_wait',Navigate())
        resource.override_pipeline({'__RevivalWait':dict(action=dict(type='Custom',param=dict(custom_action='__revival_wait')))})
        self.assertTrue(tasker.bind(resource,controller))
        for mode,expected in [('linger',['cancel','return']),('missing',['return'])]:
            controller.mode=mode;controller.cancelled=None;controller.returned=False;events.clear()
            self.assertTrue(tasker.post_task('__RevivalWait').wait().succeeded,'\n'.join(errors))
            self.assertEqual(events,expected)
        self.assertGreaterEqual(durations[-1],10.1)

    def test_world_portals_click_only_the_reviewed_opaque_interiors(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        world=read_png_bgr(ROOT/'tests/fixtures/navigation/world.png')
        portals=json.loads((ROOT/'agent/data/navigation/hubs.json').read_text(encoding='utf8'))['portals']
        events=[];errors=[]
        class Pixels(CustomController):
            region=None
            def connect(self):return True
            def request_uuid(self):return 'world-portal-interior-memory'
            def screencap(self):return world.copy()
            def touch_down(self,contact,x,y,pressure):
                bx,by,bw,bh=self.region
                assert bx<=x<bx+bw and by<=y<by+bh,('outside opaque artwork',x,y,self.region)
                events.append((x,y));return True
            def touch_up(self,*args):return True
            def touch_move(self,*args):return True
            def forbidden(self,*args):raise AssertionError('Unexpected input')
            click=swipe=key_down=key_up=input_text=start_app=stop_app=forbidden
        controller=Pixels()
        class Inspect(CustomAction):
            def run(self,context,argv):
                try:
                    for portal in portals:
                        controller.region=portal['click_region']
                        name='Nav_WorldNanzhan' if portal['town']=='town1' else 'Nav_World_'+portal['town']
                        result=context.run_task('__PortalClick',{
                            '__PortalClick':dict(action=dict(type='DoNothing'),next=[name],post_delay=0,timeout=1000),
                            name:dict(next=[],post_delay=0),
                        })
                        assert result and result.status.succeeded,portal['town']
                    return True
                except Exception:
                    import traceback
                    errors.append(traceback.format_exc());return False
        resource,tasker=Resource(),Tasker()
        resource.register_custom_recognition('battle_no_revive',self.no_revive())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__portals',Inspect())
        resource.override_pipeline({'__Portals':dict(action=dict(type='Custom',param=dict(custom_action='__portals')))})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__Portals').wait().succeeded,'\n'.join(errors))
        self.assertEqual(len(events),6)

    def test_sumeru_hud_behind_daily_menu_is_not_premature_arrival(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        from navigation.catalog import task_routine
        from navigation.runtime import Navigator
        frames=[read_png_bgr(ROOT/'tests/fixtures/navigation/daily-expanded.png')]
        events=[];errors=[]
        class Pixels(CustomController):
            stage=0
            def connect(self):return True
            def request_uuid(self):return 'sumeru-overlay-memory-only'
            def screencap(self):return frames[self.stage].copy()
            def touch_down(self,contact,x,y,pressure):events.append((self.stage,x,y));return True
            def touch_up(self,contact):self.stage+=1;return True
            def touch_move(self,*args):return True
            def forbidden(self,*args):raise AssertionError('Unexpected input')
            click=swipe=key_down=key_up=input_text=start_app=stop_app=forbidden
        controller=Pixels()
        class Navigate(CustomAction):
            def run(self,context,argv):
                try:
                    caption=context.run_recognition('Nav_Probe_paused_overlay',frames[0])
                    assert caption and caption.hit
                    assert context.run_recognition('Nav_HubSurface',frames[0]).hit
                    assert not context.run_recognition('Nav_See_sumeru',frames[0]).hit
                    # Explicit memory composites remove the pause caption and
                    # then the menu; the source HUD pixels are retained.
                    x,y,w,h=caption.box
                    resumed=frames[0].copy();resumed[y:y+h,x:x+w]=0
                    assert context.run_recognition('Nav_See_daily_menu',resumed).hit
                    closed=resumed.copy();closed[280:560,270:640]=0
                    assert context.run_recognition('Nav_See_sumeru',closed).hit
                    frames.extend([resumed,closed])
                    routine=task_routine(context,dict(navigation=dict(kind='screen',screen='sumeru')))
                    assert Navigator(context,routine,capture=controller.screencap,check=lambda:None).run()=='sumeru'
                    return True
                except Exception:
                    import traceback
                    errors.append(traceback.format_exc());return False
        resource,tasker=Resource(),Tasker()
        resource.register_custom_recognition('battle_no_revive',self.no_revive())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__sumeru',Navigate())
        resource.override_pipeline({'__Sumeru':dict(action=dict(type='Custom',param=dict(custom_action='__sumeru')))})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__Sumeru').wait().succeeded,'\n'.join(errors))
        self.assertEqual([e[0] for e in events],[0,1])

    def test_known_paused_map_resumes_before_opening_the_quest_panel(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        from navigation.catalog import task_routine
        from navigation.runtime import Navigator
        normal=read_png_bgr(ROOT/'tests/fixtures/navigation/map-b.png')
        paused=normal.copy()
        label=read_png_bgr(ROOT/'assets/resource/base/image/bounty-candle/pause.png')
        h,w=label.shape[:2]
        paused[342:342+h,381:381+w]=label
        detail=read_png_bgr(ROOT/'tests/fixtures/navigation/quest-taizong-real.png')
        events=[]
        class Pixels(CustomController):
            stage=0
            def connect(self):return True
            def request_uuid(self):return 'paused-map-memory'
            def screencap(self):return [paused,normal,detail][self.stage].copy()
            def touch_down(self,contact,x,y,pressure):
                events.append((self.stage,x,y))
                return True
            def touch_up(self,contact):
                self.stage+=1
                return True
            def touch_move(self,*args):return True
            def forbidden(self,*args):raise AssertionError('Unexpected input')
            click=swipe=key_down=key_up=input_text=start_app=stop_app=forbidden
        controller=Pixels()
        class Navigate(CustomAction):
            def run(self,context,argv):
                graph=task_routine(context,dict(navigation=dict(kind='bounty',title='守卫太宗',target_node='Bounty_Target')))
                nav=Navigator(context,graph,capture=lambda:controller.screencap(),check=lambda:None)
                return nav.run()=='task_destination'
        resource,tasker=Resource(),Tasker()
        resource.register_custom_recognition('battle_no_revive',self.no_revive())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__paused_nav',Navigate())
        resource.override_pipeline({'__PausedNav':{'action':{'type':'Custom','param':{'custom_action':'__paused_nav'}}}})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__PausedNav').wait().succeeded)
        self.assertEqual([event[0] for event in events],[0,1])
        self.assertEqual(controller.stage,2)

    def test_real_templates_and_navigation_identify_one_screen_without_input(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        from navigation.catalog import task_routine
        from navigation.runtime import Navigator

        frames = {path.stem:read_png_bgr(path) for path in (ROOT/'tests/fixtures/navigation').glob('*.png')}
        checks = []
        class PixelsOnly(CustomController):
            def connect(self): return True
            def request_uuid(self): return 'navigation-memory-readonly'
            def screencap(self): return frames['task-map-real'].copy()
            def forbidden(self, *args): raise AssertionError('Desktop input is forbidden')
            touch_down = touch_move = touch_up = click = swipe = start_app = stop_app = forbidden
            key_down = key_up = click_key = input_text = forbidden

        class Inspect(CustomAction):
            def run(self, context, argv):
                for frame_name in ('challenge-failed', 'challenge-succeeded', 'task-map-real', 'quest-taizong-real'):
                    for suffix in ('Failed', 'Succeeded', 'FailedReturn', 'SucceededReturn'):
                        result = context.run_recognition('Result_'+suffix, frames[frame_name])
                        expected = frame_name == ('challenge-failed' if suffix.startswith('Failed') else 'challenge-succeeded')
                        checks.append(dict(check=frame_name+':'+suffix, actual=bool(result and result.hit), expected=expected))
                # The button alone must never authorize a result-screen click.
                for kind, suffix in [('failed','Failed'),('succeeded','Succeeded')]:
                    pixels = frames['challenge-'+kind].copy()
                    pixels[120:500,200:980] = 0
                    result = context.run_recognition('Result_'+suffix+'Return', pixels)
                    checks.append(dict(check=kind+':button without title', actual=bool(result and result.hit), expected=False))
                graph = task_routine(context, {})
                for name, expected in [('task-map-real','map'), ('map-a','map'), ('map-b','map'),
                    ('world','world'), ('activity','activity'), ('profile','profile'), ('profile-sell','profile_sell'),
                    ('warehouse','warehouse'), ('treasure','treasure'), ('settings-menu','settings_menu'),
                    ('alliance','alliance'), ('alliance-contribution','alliance_contribution'),
                    ('survival-week','survival_week'), ('survival-rewards','survival_rewards'),
                    ('bounty','bounty_board')]:
                    try:
                        actual = Navigator(context, graph, capture=lambda:frames[name], check=lambda:None,
                            wait=lambda _seconds:None, log=lambda _line:None).locate()
                    except Exception as error:
                        actual = str(error)
                    checks.append(dict(check='locate:'+name, actual=actual, expected=expected))
                graph = task_routine(context, dict(navigation=dict(kind='bounty', title='守卫太宗',
                    target_node='Bounty_Target')))
                actual = Navigator(context,graph,capture=lambda:frames['quest-taizong-real'],
                    check=lambda:None,wait=lambda _seconds:None,log=lambda _line:None).locate()
                checks.append(dict(check='accepted real Taizong detail', actual=actual, expected='task_destination'))
                choose=context.run_recognition('Nav_ChooseBounty',frames['quest-taizong-real'])
                checks.append(dict(check='accepted row remains selectable',actual=bool(choose and choose.hit),expected=True))
                return True

        controller, resource, tasker = PixelsOnly(), Resource(), Tasker()
        resource.register_custom_recognition('battle_no_revive',self.no_revive())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__inspect_navigation', Inspect())
        resource.override_pipeline({'__InspectNavigation':{'action':{'type':'Custom','param':{'custom_action':'__inspect_navigation'}}}})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__InspectNavigation').wait().succeeded)
        (self.log/'recognition-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf8')
        self.assertEqual([item for item in checks if item['actual'] != item['expected']], [])

    def test_failed_attempt_returns_retries_and_does_not_consume_two_round_quota(self):
        self.run_failure_quota(late_revive=False)

    def test_late_revive_after_map_match_recovers_the_same_round(self):
        self.run_failure_quota(late_revive=True)

    def run_failure_quota(self, *, late_revive):
        import importlib.util
        import sys
        import tempfile
        from types import ModuleType
        from unittest.mock import patch
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr

        registration = ModuleType('maa.agent.agent_server')
        registration.AgentServer = type('RegistrationOnly', (), {'custom_action':staticmethod(lambda _name:lambda cls:cls)})
        def load(name, filename):
            spec=importlib.util.spec_from_file_location(name, ROOT/'agent'/filename)
            module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        with patch.dict(sys.modules, {'maa.agent':ModuleType('maa.agent'),'maa.agent.agent_server':registration}):
            state = load('navigation_native_state', 'script_task.py')
            player = load('navigation_native_player', 'battle_player.py')
        player_patch=patch.dict(sys.modules,{'battle_player':player})
        player_patch.start();self.addCleanup(player_patch.stop)
        names = dict(map='task-map-real', detail='quest-taizong-real', failed='challenge-failed', won='challenge-succeeded')
        frames = {key:read_png_bgr(ROOT/'tests/fixtures/navigation'/(value+'.png')) for key,value in names.items()}
        frames['battle'] = read_png_bgr(ROOT/'tests/fixtures/bounty-candle/candle-battle-ready.png')[:,:1147].copy()
        frames['revive'] = read_png_bgr(ROOT/'tests/fixtures/navigation/revival/normal-p1-up.png')
        events, quotas = [], []
        class Game(CustomController):
            stage, attempts, successes, failed_returns = 'detail', 0, 0, 0
            injected = False
            def connect(self): return True
            def request_uuid(self): return 'navigation-failure-quota-memory'
            def screencap(self): return frames[self.stage].copy()
            def touch_down(self, contact,x,y,pressure):
                events.append(('down',self.stage,x,y))
                self.last_down=(x,y)
                return True
            def touch_move(self, *args): return True
            def touch_up(self, contact):
                events.append(('up',self.stage))
                if self.stage == 'map': self.stage='detail'
                elif self.stage == 'detail':
                    self.stage='battle'
                    self.attempts+=1
                elif self.stage == 'revive':
                    x,y=self.last_down
                    if not (628<=x<=724 and 429<=y<=470):
                        raise AssertionError('Only the cancel button may be clicked')
                    self.stage='failed'
                elif self.stage in ('failed','won'):
                    x,y=self.last_down
                    if self.stage=='failed' and 600<=x<=780 and 490<=y<=550:
                        self.failed_returns+=1
                        self.stage='map'
                        if late_revive and not self.injected:
                            self.stage='revive';self.injected=True
                            events.append(('injected_late_revive',))
                    elif self.stage=='won' and 810<=x<=1020 and 530<=y<=595:
                        self.successes+=1
                        self.stage='map'
                elif self.stage!='battle': raise AssertionError('Unexpected mouse input')
                return True
            def key_down(self, key):
                events.append(('key_down',self.stage,key))
                if self.attempts == 1: self.stage='failed'
                return True
            def key_up(self, key):
                events.append(('key_up',self.stage,key))
                if self.stage == 'battle': self.stage='won'
                return True
            def forbidden(self,*args): raise AssertionError('Unexpected controller API')
            click = swipe = click_key = input_text = start_app = stop_app = forbidden
        class Count(CustomAction):
            def run(self,context,argv):
                quotas.append(len(events))
                return True

        class TrackedState(CustomAction):
            def run(self,context,argv):
                params=argv.custom_action_param
                if isinstance(params,str):params=json.loads(params)
                if params.get('phase')=='begin':quotas.append(len(events))
                return state.ScriptTaskState().run(context,argv)

        with tempfile.TemporaryDirectory(prefix='nav-memory-') as temporary:
            directory=Path(temporary).resolve()
            library=directory/'recordings'
            library.mkdir()
            (library/'memory.json').write_text(json.dumps(dict(version=1,name='Memory only',base=[1147,720],duration_ms=1600,
                actions=[dict(t=0,op='down',button='left',x=650,y=520),dict(t=50,op='keydown',key=65),
                         dict(t=800,op='keyup',key=65),dict(t=0,op='up',button='left',x=650,y=520)])),encoding='utf8')
            from script_library import bind_task_recording
            from task_catalog import task_key
            task=json.loads((ROOT/'assets/interface.json').read_text(encoding='utf8'))['task']
            task=next(t for t in task if t['name']=='悬赏（守卫太宗）')
            import hashlib
            bind_task_recording(library, task_key(task['name'],task['entry']), 'memory.json',
                expected_revision=hashlib.sha256((library/'memory.json').read_bytes()).hexdigest())
            state.ROOT, state.RECORDINGS, state.STATUS_DIRECTORY=directory,library,directory/'debug/task-runs'
            player.RECORDING_DIR=str(library)
            player._toast=lambda *_args:None
            controller, resource, tasker=Game(),Resource(),Tasker()
            resource.register_custom_recognition('battle_no_revive',self.no_revive())
            self.assertTrue(controller.post_connection().wait().succeeded)
            controller.set_screenshot_use_raw_size(True)
            self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
            resource.register_custom_action('script_task_state',TrackedState())
            resource.register_custom_action('battle_play',player.BattlePlay())
            resource.register_custom_action('loop_count',Count())
            self.assertTrue(tasker.bind(resource,controller))
            prefix=task['name']+'-'
            pipeline=json.loads((ROOT/'assets/resource/base/pipeline'/(task['name']+'.json')).read_text(encoding='utf8'))
            # The existing scene-specific guard has its own fixtures. This test
            # covers real quest/result recognition and the new shared flow.
            overrides={prefix+'刷取次数':{'max_hit':2},
                'Bounty_Play':{'timeout':2000,'recognition':{'type':'DirectHit'}},
                '脚本任务设置':{'action':{'param':{'custom_action_param':{'failure_retry':{'enabled':True,'max_retries':2}}}}}}
            result=tasker.post_task(task['entry'],overrides).wait()
            checkpoints=[json.loads(p.read_text(encoding='utf8')) for p in state.STATUS_DIRECTORY.glob('*.json')]
            report='late-revive-quota-result.json' if late_revive else 'quota-result.json'
            (self.log/report).write_text(json.dumps(dict(succeeded=result.succeeded,attempts=controller.attempts,
                successes=controller.successes,quota_hits=len(quotas),checkpoints=checkpoints,events=events),ensure_ascii=False,indent=2),encoding='utf8')
            self.assertTrue(result.succeeded)
            self.assertEqual((controller.attempts, controller.successes, len(quotas)),(3,2,2))
            self.assertEqual(controller.failed_returns,2 if late_revive else 1)
            self.assertEqual(controller.injected,late_revive)
            self.assertEqual([p['status'] for p in checkpoints],['success','success'])
            self.assertEqual(sorted(p['failure_retries'] for p in checkpoints),[0,1])
            self.assertFalse(state._rounds)


if __name__ == '__main__': unittest.main()
