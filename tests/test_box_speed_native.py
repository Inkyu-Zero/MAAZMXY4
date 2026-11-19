"""Input-only native key transport; no game, capture, Resource or OCR owner."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1', 'opt-in Maa memory input')
class NativeBoxSpeedTests(unittest.TestCase):
    def test_old_speed_session_can_be_cleared_by_current_instance_parameters(self):
        import numpy as np
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from task_catalog import custom_parameters
        class Pixels(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'speed-proof-compatibility'
            def screencap(self):return np.zeros((1,1,3),dtype=np.uint8)
        errors=[]
        class ClearProof(CustomAction):
            def run(self,context,argv):
                try:
                    assert context.override_pipeline({'__OldSpeed':dict(action=dict(type='Custom',param=dict(
                        custom_action='__clear_speed',custom_action_param=dict(speed=2,box_speed_proof=dict(state='ready',speed=2)))))})
                    assert custom_parameters(context.get_node_data('__OldSpeed'))['box_speed_proof']['speed']==2
                    assert context.override_pipeline({'__OldSpeed':dict(action=dict(param=dict(custom_action_param=dict(speed=1,box_speed_proof=None))))})
                    values=custom_parameters(context.get_node_data('__OldSpeed'))
                    assert values['speed']==1 and values.get('box_speed_proof') is None
                    return True
                except Exception as error:errors.append(str(error));return False
        with tempfile.TemporaryDirectory(prefix='speed-proof-native-',ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(Path(directory)/'logs')
            resource,controller,tasker=Resource(),Pixels(),Tasker()
            self.assertTrue(controller.post_connection().wait().succeeded)
            self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
            self.assertTrue(tasker.bind(resource,controller))
            resource.register_custom_action('__clear_speed',ClearProof())
            self.assertTrue(tasker.post_task('__ClearSpeed',{'__ClearSpeed':dict(action=dict(type='Custom',param=dict(custom_action='__clear_speed')),post_delay=0)}).wait().succeeded,errors)

    def test_both_switches_send_two_presses_with_no_capture_or_resource(self):
        from maa.controller import CustomController
        from maa.tasker import Tasker
        from box_speed_native import NativeSpeedTransport
        from box_speed import SpeedLease
        events=[]
        class InputOnly(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'speed-input-only-memory'
            def key_down(self,key):events.append(('down',key));return True
            def key_up(self,key):events.append(('up',key));return True
            def forbidden(self,*args):raise AssertionError('Speed switching must not capture or use other input')
            screencap=click=swipe=touch_down=touch_up=touch_move=input_text=start_app=stop_app=forbidden
        transport=NativeSpeedTransport.__new__(NativeSpeedTransport)
        transport.hwnd=42;transport.cancelled=threading.Event();transport.input_lock=threading.Lock()
        transport.alive=lambda:True;transport.require_identity=lambda:None
        transport.input_controller=None;transport.key_may_be_down=None;transport.process=None
        with tempfile.TemporaryDirectory(prefix='box-speed-input-',ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(Path(directory)/'logs')
            controller=InputOnly()
            with patch('maa.controller.Win32Controller',return_value=controller) as factory, patch('maa.resource.Resource',side_effect=AssertionError('No speed resource allowed')):
                transport.prepare()
                self.assertEqual(factory.call_args.kwargs['screencap_method'],0)
                with SpeedLease(2,transport.apply) as lease:
                    lease.start()
                    self.assertEqual(events,[('down',50),('up',50)]*2)
                self.assertEqual(events,[('down',50),('up',50)]*2+[('down',49),('up',49)]*2)
                transport.close()
                self.assertIsNone(transport.input_controller)


if __name__=='__main__':unittest.main()
