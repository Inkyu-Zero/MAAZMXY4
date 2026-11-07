"""Native OCR and key dispatch on synthetic box toasts, without OS windows."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1' and os.name=='nt',
                     'opt-in memory-only OCR, using the box font installed on Windows')
class NativeBoxSpeedTests(unittest.TestCase):
    def test_confirmation_reset_and_stale_toast_rejection(self):
        import numpy as np
        from PIL import Image,ImageDraw,ImageFont
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from box_speed_native import NativeSpeedTransport
        from box_speed import SpeedLease
        font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',24)
        def frame(toasts):
            result=Image.new('RGB',(1147,720),(45,65,80))
            draw=ImageDraw.Draw(result)
            for text,shift in toasts:
                bounds=draw.textbbox((0,0),text,font=font);width=bounds[2]-bounds[0];height=bounds[3]-bounds[1]
                x=(1147-width)//2;y=(720-height)//2+shift
                draw.rounded_rectangle((x-8,y-6,x+width+8,y+height+6),radius=5,fill=(72,78,84),outline=(210,210,210),width=2)
                draw.text((x-bounds[0],y-bounds[1]),text,font=font,fill='white',stroke_width=1,stroke_fill=(15,15,15))
            return np.asarray(result)[:,:,::-1].copy()
        class Pixels(CustomController):
            image=frame([]);captures=0
            def connect(self):return True
            def request_uuid(self):return 'native-speed-memory-only'
            def screencap(self):self.captures+=1;return self.image.copy()
            def forbidden(self,*args):raise AssertionError('No OS or game input is permitted')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        class Done:
            succeeded=True
            def wait(self):return self
        events=[]
        class Keys:
            def post_key_down(self,key):
                events.append(('down',key));controller.image=frame([(f'设置{key-48}倍速',0)]);return Done()
            def post_key_up(self,key):events.append(('up',key));return Done()
        transport=NativeSpeedTransport.__new__(NativeSpeedTransport)
        transport.cancelled=threading.Event();transport.input_lock=threading.Lock()
        transport.alive=lambda:True;transport.require_identity=lambda:None
        transport.input_controller=Keys();transport.key_may_be_down=None
        class Probe(CustomAction):
            def run(self,context,argv):
                try:return transport._verify(context)
                except Exception as error:
                    transport.probe_error=str(error);transport.probe_exception=error;return False
        cleared=[]
        class ClearProof(CustomAction):
            def run(self,context,argv):
                from task_catalog import custom_parameters
                assert context.override_pipeline({'__OldSpeed':dict(action=dict(type='Custom',param=dict(
                    custom_action='__box_speed_probe',custom_action_param=dict(speed=2,box_speed_proof=dict(state='ready',speed=2)))))})
                assert custom_parameters(context.get_node_data('__OldSpeed'))['box_speed_proof']['speed']==2
                assert context.override_pipeline({'__OldSpeed':dict(action=dict(param=dict(
                    custom_action_param=dict(speed=1,box_speed_proof=None))))})
                values=custom_parameters(context.get_node_data('__OldSpeed'))
                assert values['speed']==1 and values.get('box_speed_proof') is None
                cleared.append(True);return True
        with tempfile.TemporaryDirectory(prefix='box-speed-native-',ignore_cleanup_errors=True) as temporary:
            Tasker.set_log_dir(Path(temporary)/'logs')
            resource,controller,tasker=Resource(),Pixels(),Tasker()
            transport.resource,transport.controller,transport.tasker=resource,controller,tasker
            def prepare_memory_capture():
                transport.controller=controller;transport.tasker=tasker
            transport.prepare_capture=prepare_memory_capture
            self.assertTrue(controller.post_connection().wait().succeeded)
            controller.set_screenshot_use_raw_size(True)
            self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
            resource.register_custom_action('__box_speed_probe',Probe())
            resource.register_custom_action('__clear_speed_proof',ClearProof())
            resource.override_pipeline({'__BoxSpeedProbe':dict(pre_delay=0,post_delay=0,next=[],
                action=dict(type='Custom',param=dict(custom_action='__box_speed_probe'))),
                '__ClearSpeedProof':dict(pre_delay=0,post_delay=0,next=[],action=dict(type='Custom',param=dict(custom_action='__clear_speed_proof')))})
            self.assertTrue(tasker.bind(resource,controller))
            self.assertTrue(tasker.post_task('__ClearSpeedProof').wait().succeeded)
            self.assertEqual(cleared,[True])
            with SpeedLease(2,transport.apply) as lease:
                self.assertTrue(lease.ready)
                self.assertIsNone(transport.controller,'FramePool must not stay active during playback')
                self.assertEqual(events,[('down',50),('up',50)])
                captures=controller.captures
                lease.start()
                self.assertEqual(controller.captures,captures,'Keeping speed across rounds must not capture')
            self.assertEqual(events,[('down',50),('up',50),('down',49),('up',49),('down',49),('up',49)])
            prepare_memory_capture()
            cases=[(2,[('设置1倍速',0)],False),(1,[('设置2倍速',0)],False),
                   (1,[('设置1倍速',-49),('设置2倍速',0)],False),
                   (2,[('设置2倍速',-49),('设置1倍速',0)],False),
                   (2,[('60帧不支持倍速调整',0)],False),
                   (2,[],False),(2,[('设置2倍速',0)],True)]
            for expected,toasts,hit in cases:
                with self.subTest(expected=expected,toasts=toasts):
                    transport.requested=expected;transport.probe_error='';controller.image=frame(toasts)
                    self.assertTrue(controller.post_screencap().wait().succeeded)
                    self.assertEqual(tasker.post_task('__BoxSpeedProbe').wait().succeeded,hit,transport.probe_error)
            print('BOX_SPEED_NATIVE: 2x/1x acknowledged; stale/opposite/high-frame-rate/missing hints rejected; held lease has no capture.',flush=True)


if __name__=='__main__':unittest.main()
