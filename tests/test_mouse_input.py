"""Mouse cleanup boundaries: zero capture, no mid-script moves, released input first."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
import mouse_input
import recording_playback
from test_battle_player_studio import FakeController
import test_upstream_242_merge as upstream_tests


class MouseInputTests(unittest.TestCase):
    def test_uses_only_bound_window_and_reads_identity_once(self):
        class Controller:
            reads=0
            @property
            def info(self):self.reads+=1;return {'hwnd':hex(2**40+1)}
        controller=Controller()
        with patch.object(mouse_input,'post_move',return_value=(True,'ok')) as move:
            mouse_input.finish_mouse_operation(controller)
            move.assert_called_once_with(2**40+1,20,20)
        self.assertEqual(controller.reads,1)

    def test_invalid_window_never_falls_back_to_enumeration(self):
        for hwnd in (0,-1,True,'invalid'):
            with self.subTest(hwnd=hwnd),patch.object(mouse_input,'post_move') as move:
                with self.assertRaises(ValueError):mouse_input.finish_mouse_operation(SimpleNamespace(info={'hwnd':hwnd}))
                move.assert_not_called()

    def test_complete_and_cancelled_recordings_park_once_after_releases(self):
        for cancel in (False,True):
            with self.subTest(cancel=cancel):
                controller=FakeController();events=[];stopped=False
                def sent(name,args,kwargs):
                    nonlocal stopped
                    events.append(name)
                    if cancel and name=='post_touch_down':stopped=True
                controller.after_call=sent
                rec=dict(name='test',duration_ms=0,actions=[dict(t=0,op='keydown',key=65),
                    dict(t=0,op='down',x=200,y=300),dict(t=0,op='move',x=210,y=300),
                    dict(t=0,op='up',x=210,y=300),dict(t=0,op='keyup',key=65)])
                completed=recording_playback.play_actions(rec,controller,lambda:stopped,
                    after_playback=lambda:events.append('park'),log=lambda _:None)
                self.assertEqual(completed,not cancel)
                self.assertEqual(events[-1],'park');self.assertEqual(events.count('park'),1)
                self.assertIn('post_key_up',events[:-1]);self.assertIn('post_touch_up',events[:-1])

    def test_failed_release_never_parks_a_still_held_mouse(self):
        controller=FakeController();controller.fail_status={'post_touch_up'}
        rec=dict(name='test',actions=[dict(t=0,op='down',x=200,y=300)])
        with patch('mouse_input.post_move') as parked:
            with self.assertRaisesRegex(RuntimeError,'输入清理'):
                recording_playback.play_actions(rec,controller,lambda:False,log=lambda _:None,
                    after_playback=lambda:parked())
            parked.assert_not_called()

    def test_pipeline_click_preserves_verified_box_then_parks(self):
        cursor=upstream_tests.UpstreamMergeTests().cursor_module();events=[]
        controller=FakeController();controller.info={'hwnd':42}
        controller.after_call=lambda name,args,kwargs:events.append((name,args))
        context=SimpleNamespace(tasker=SimpleNamespace(stopping=False,controller=controller))
        with patch.object(cursor,'_post_move',side_effect=lambda *args:events.append(('park',args)) or (True,'ok')):
            self.assertTrue(cursor.ClickAndClearHover().run(context,SimpleNamespace(box=(100,200,50,60))))
        self.assertEqual(events,[('post_click',(125,230)),('park',(42,20,20))])


if __name__=='__main__':unittest.main()
