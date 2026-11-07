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
    def test_base_entry_holds_100ms_and_dwells_after_release_before_parking(self):
        self.run_press_probe(100)

    def test_double_speed_entry_holds_50ms_and_keeps_release_dwell(self):
        self.run_press_probe(50,override=True)

    def test_map_double_press_at_1x_parks_only_after_both_releases(self):
        self.run_press_probe(100,override=True,repeat=2)

    def test_map_double_press_at_2x_parks_only_after_both_releases(self):
        self.run_press_probe(50,override=True,repeat=2)

    def run_press_probe(self,duration,override=False,repeat=1):
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
                    if override:
                        self_outer.assertTrue(context.override_pipeline({target:{'action':{'param':{'duration':duration}},
                            'repeat':repeat,'repeat_delay':50 if repeat==2 else 0}}))
                    def parked(_):events.append(('park',time.monotonic()));return True,'memory only'
                    with patch('navigation.runtime._move_cursor_off_target',side_effect=parked):
                        return nav.perform(target,center=True)
                except Exception as error:errors.append(repr(error));return False
        resource,tasker=Resource(),Tasker()
        self_outer=self
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        resource.override_pipeline({target:dict(recognition=dict(type='DirectHit',param=dict(roi=[100,100,40,30]))),
            '__PressProbe':dict(action=dict(type='Custom',param=dict(custom_action='entry_press_probe')),pre_delay=0,post_delay=0)})
        resource.register_custom_action('entry_press_probe',Check())
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__PressProbe').wait().succeeded,errors)
        self.assertEqual([event[0] for event in events],['down','up']*repeat+['park'])
        for index in range(repeat):
            down,up=events[index*2:index*2+2]
            self.assertEqual(down[2:],(120,115))
            held=up[1]-down[1]
            self.assertGreaterEqual(held,(duration-10)/1000)
            self.assertLess(held,(duration+40)/1000)
            print(f'entry requested_ms={duration} press={index+1}/{repeat} held_ms={held*1000:.1f}',flush=True)
        if repeat==2:
            gap=events[2][1]-events[1][1]
            self.assertGreaterEqual(gap,.04)
            self.assertLess(gap,.15)
            print(f'entry repeat_gap_ms={gap*1000:.1f}',flush=True)
        self.assertGreaterEqual(events[-1][1]-events[-2][1],.95)
