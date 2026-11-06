"""Verify native press/release/hover ordering without a desktop controller."""
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1','opt-in memory-only input timing')
class EntryPressNativeTests(unittest.TestCase):
    def test_centered_entry_holds_300ms_and_dwells_after_release_before_parking(self):
        import numpy as np
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        sys.path.insert(0,str(ROOT/'agent'))
        from navigation.runtime import Navigator
        events=[];errors=[];target='无冥（孤魂岭）-关卡入口'
        frame=np.zeros((720,1148,3),dtype=np.uint8)
        class Pixels(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'entry-press-memory'
            def screencap(self):return frame.copy()
            def touch_down(self,contact,x,y,pressure):events.append(('down',time.monotonic(),x,y));return True
            def touch_up(self,contact):events.append(('up',time.monotonic()));return True
            def forbidden(self,*args):raise AssertionError('Unexpected input')
            click=swipe=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        controller=Pixels()
        class Check(CustomAction):
            def run(self,context,argv):
                try:
                    nav=Navigator(context,{},capture=controller.screencap,check=lambda:None)
                    def parked(_):events.append(('park',time.monotonic()));return True,'memory only'
                    with patch('navigation.runtime._move_cursor_off_target',side_effect=parked):
                        return nav.perform(target,center=True)
                except Exception as error:errors.append(repr(error));return False
        resource,tasker=Resource(),Tasker()
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        resource.override_pipeline({target:dict(recognition=dict(type='DirectHit',param=dict(roi=[100,100,40,30]))),
            '__PressProbe':dict(action=dict(type='Custom',param=dict(custom_action='entry_press_probe')),pre_delay=0,post_delay=0)})
        resource.register_custom_action('entry_press_probe',Check())
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__PressProbe').wait().succeeded,errors)
        self.assertEqual([event[0] for event in events],['down','up','park'])
        self.assertEqual(events[0][2:],(120,115))
        self.assertGreaterEqual(events[1][1]-events[0][1],.28)
        self.assertGreaterEqual(events[2][1]-events[1][1],.95)
