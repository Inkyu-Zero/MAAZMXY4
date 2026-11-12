"""Input-only jobs exercise the same timeline/cleanup as normal native tasks."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_battle_player_studio import player, FakeController
from test_window_geometry import Geometry
from window_geometry import WindowLease
from script_playback import PlaybackJob, InputOnlyController, CompletedInput
from script_studio_service import StudioService
from task_catalog import task_catalog, execution_plan
from script_library import bind_task_recording

ROOT=Path(__file__).resolve().parents[1]


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'recordings').mkdir()
        self.file=self.root/'recordings/test.json'
        self.file.write_text(json.dumps(dict(version=1,name='test',base=[1148,720],duration_ms=70,
            actions=[dict(t=0,op='keydown',key=68),dict(t=30,op='keyup',key=68),
                     dict(t=0,op='down',x=100,y=200),dict(t=30,op='up',x=100,y=200)])),encoding='utf8')
        self.geometry=Geometry();self.ctrl=FakeController()
        self.params=dict(hwnd=42,file='test.json',sha256=hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.modules=patch.dict(sys.modules,{'battle_player':player});self.modules.start();self.addCleanup(self.modules.stop)

    def job(self):
        job=PlaybackJob(self.root,self.params,controller_factory=lambda *args:self.ctrl,
            lease_factory=lambda hwnd,base:WindowLease(hwnd,base,api=self.geometry),
            input_adapter_factory=lambda native,*args:native)
        self.addCleanup(job.stop)
        return job

    def finish(self,job):
        deadline=time.monotonic()+2
        while job.snapshot()['state']=='running' and time.monotonic()<deadline:time.sleep(.01)
        return job.snapshot()

    def test_complete_mixed_input_and_trailing_wait_without_capture(self):
        start=time.monotonic();job=self.job();result=self.finish(job)
        self.assertEqual(result['state'],'succeeded',result)
        self.assertGreaterEqual(time.monotonic()-start,.07)
        self.assertEqual([c[0] for c in self.ctrl.calls],['post_key_down','post_key_up','post_touch_down','post_touch_move','post_touch_up'])
        self.assertFalse(self.geometry.claimed)

    def test_confirmed_double_speed_scales_actual_timeline_including_trailing_wait(self):
        from box_speed import verify_proof
        proof=dict(state='ready',speed=2,hwnd=42,pid=17,started_utc_ticks=123,
                   exe_path=str(self.root/'box.exe'),sha256=self.params['sha256'])
        self.params.update(speed=2,box_speed_proof=proof)
        now=[10.0];deadlines=[];events=[]
        self.ctrl.after_call=lambda name,args,kwargs:events.append((name,now[0]-10.0))
        def wait_until(target,should_stop,clock):
            deadlines.append(target-10.0);now[0]=target
            return not should_stop()
        def confirmed(receipt,hwnd,revision):
            return verify_proof(receipt,hwnd,revision,dict(pid=17,started_utc_ticks=123),proof['exe_path'])
        with patch('box_speed_native.verify_playback_proof',side_effect=confirmed) as verify, \
                patch('window_geometry.WindowLease.clock',lambda lease:now[0]), \
                patch('recording_playback._sleep_until',side_effect=wait_until):
            result=self.finish(self.job())
        self.assertEqual(result['state'],'succeeded',result)
        verify.assert_called_once_with(proof,42,self.params['sha256'])
        self.assertEqual(len(deadlines),5)
        for actual,expected in zip(deadlines,[0,.015,.015,.03,.035]):
            self.assertAlmostEqual(actual,expected)
        self.assertEqual([name for name,_ in events],
                         ['post_key_down','post_key_up','post_touch_down','post_touch_move','post_touch_up'])
        self.assertFalse(self.geometry.claimed)

    def test_unconfirmed_double_speed_never_adapts_window_or_sends_input(self):
        self.params['speed']=2
        result=self.finish(self.job())
        self.assertEqual(result['state'],'failed',result)
        self.assertIn('尚未确认',result['error'])
        self.assertEqual(self.ctrl.calls,[])
        self.assertEqual(self.geometry.changes,[])

    def test_stop_during_gap_releases_input_and_never_counts_success(self):
        data=json.loads(self.file.read_text());data['actions'][1]['t']=10000;data['duration_ms']=10040
        self.file.write_text(json.dumps(data));self.params['sha256']=hashlib.sha256(self.file.read_bytes()).hexdigest()
        job=self.job()
        deadline=time.monotonic()+2
        while not self.ctrl.calls and time.monotonic()<deadline:time.sleep(.01)
        start=time.monotonic();result=job.stop()
        self.assertLess(time.monotonic()-start,.5)
        self.assertEqual(result['state'],'stopped')
        self.assertEqual([c[0] for c in self.ctrl.calls],['post_key_down','post_key_up'])
        self.assertFalse(self.geometry.claimed)

    def test_recording_change_and_unbound_task_do_not_input(self):
        self.file.write_text(self.file.read_text()+' ')
        result=self.finish(self.job());self.assertEqual(result['state'],'failed')
        self.assertEqual(self.ctrl.calls,[]);self.assertFalse(self.geometry.changes)
        tasks=task_catalog(ROOT,bindings={})
        generic=next(t for t in tasks if t['entry']=='GenericScriptPlayback')
        self.assertEqual(generic['modes'],['pipeline']);self.assertTrue(generic['input_only'])
        self.assertIsNone(generic['management_profile'])

    def test_size_failure_mid_play_releases_held_key(self):
        def fail(name,args,kwargs):
            if name=='post_key_down':
                self.geometry.info['width']=900;self.geometry.fail_resize=True
        self.ctrl.after_call=fail
        result=self.finish(self.job())
        self.assertEqual(result['state'],'failed',result)
        self.assertIn('播放期间窗口尺寸',result['error'])
        self.assertEqual([c[0] for c in self.ctrl.calls],['post_key_down','post_key_up'])
        self.assertFalse(self.geometry.claimed)

    def test_worker_close_stops_owned_job(self):
        service=StudioService(self.root)
        job=self.job();service.playback=job
        service.close()
        self.assertNotEqual(job.snapshot()['state'],'running')
        self.assertFalse(self.geometry.claimed)

    def test_task_binding_uses_existing_catalog_and_is_required(self):
        definition=dict(task=[dict(name='通用脚本播放',entry='GenericScriptPlayback')],resource=[])
        (self.root/'interface.json').write_text(json.dumps(definition),encoding='utf8')
        unbound=execution_plan(self.root,'通用脚本播放','GenericScriptPlayback',report_unbound=True)
        self.assertEqual(unbound['stop_reason'],'unbound_script')
        before=self.file.read_bytes()
        bind_task_recording(self.root/'recordings',unbound['id'],'test.json',expected_revision=self.params['sha256'])
        plan=execution_plan(self.root,'通用脚本播放','GenericScriptPlayback')
        self.assertTrue(plan['handled']);self.assertTrue(plan['input_only'])
        self.assertEqual(plan['recording']['sha256'],self.params['sha256'])
        self.assertEqual(plan['recording']['base'],[1148,720])
        self.assertEqual(self.file.read_bytes(),before)


class InputCoordinateTests(unittest.TestCase):
    def test_failed_mouse_dispatch_restores_the_thread_pixel_scope(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        events=[]
        @contextmanager
        def pixels():
            events.append('enter')
            try:yield
            finally:events.append('restore')
        def failed(*args):
            events.append('send')
            return False
        controller=object.__new__(InputOnlyController)
        controller.lease=SimpleNamespace(hwnd=42,require_identity=lambda:None,
            api=SimpleNamespace(windows=SimpleNamespace(_physical_pixels=pixels)))
        controller.mouse_method='PostMessage'
        controller.api=SimpleNamespace(PostMessageW=failed)
        with self.assertRaisesRegex(RuntimeError,'后台鼠标输入失败'):
            controller._message(0x201,1,0)
        self.assertEqual(events,['enter','send','restore'])

    def test_mouse_and_wheel_map_script_coordinates_without_capture(self):
        api=Geometry();api.bits=0;api.info.update(width=1722,height=1080)
        with WindowLease(42,(1148,720),api=api) as lease:
            controller=object.__new__(InputOnlyController)
            controller.lease=lease
            controller.position=(0,0);controller.buttons=set()
            messages=[]
            controller._message=lambda *args:(messages.append(args),CompletedInput())[1]
            controller.post_touch_down(100,200)
            controller.post_touch_up()
            self.assertIn((0x201,1,(300<<16)|150),messages)
            self.assertIn((0x202,0,(300<<16)|150),messages)
            controller.post_touch_move(200,300)
            self.assertEqual(messages[-1],(0x200,0,(450<<16)|300))
            controller.post_scroll(0,120)
            self.assertEqual(messages[-1],(0x20a,120<<16,(500<<16)|400))
            controller.clear_hover()
            self.assertEqual(messages[-1],(0x200,0,(20<<16)|20))
            self.assertFalse(api.changes)


if __name__=='__main__':unittest.main()
