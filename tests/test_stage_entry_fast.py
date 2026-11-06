"""Ignored entry clicks retry on a fresh map frame, not a loading timeout."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from test_bounty_candle import load_runtime, Context
from navigation.runtime import Navigator

runtime = load_runtime()


class FastEntryTests(unittest.TestCase):
    def enter(self, *, ignored=0, loading=0, cancel=False):
        ctx = Context(); ctx.tasker.controller.info = {}
        clock = [0.0]; calls = []; images = []; clicks = []
        state = dict(parameters=dict(navigation=dict(kind='stage',target_node='Target'),
                                     round_flow=dict(scene_node='Scene',load_timeout_ms=15000)))
        current = ['map']; remaining = [loading]
        def capture():
            if current[0] == 'loading':
                if remaining[0] == 0: current[0] = 'stage'
                else: remaining[0] -= 1
            image = (len(images)+1, current[0]); images.append(image); return image
        def recognize(name, image):
            calls.append((name, image))
            if cancel and name == 'Target' and clicks: ctx.tasker.stopping = True
            return SimpleNamespace(hit=(name == 'Scene' and image[1] == 'stage') or
                (name == 'Target' and image[1] == 'map'), box=(image[0]*10,100,10,10))
        def action(name, box):
            self.assertEqual(name,'Target'); clicks.append((clock[0],box))
            if len(clicks)>ignored: current[0]='loading' if loading else 'stage'
            clock[0] += .5  # existing game transition delay; no extra hover wait
            return SimpleNamespace(success=True)
        ctx.run_recognition=recognize;ctx.run_action=action
        nav=Navigator(ctx,{},capture=capture,check=lambda:runtime._check(ctx),log=lambda _:None,
                      clock=lambda:clock[0],wait=lambda t:clock.__setitem__(0,clock[0]+t))
        def navigate(*_):
            self.assertTrue(nav.recognize('Target').hit)
        with patch.object(runtime,'navigate_round',side_effect=navigate), \
                patch.object(Navigator,'_modal_state',return_value=None):
            try: result=runtime.enter_stage(ctx,state,nav)
            except runtime.Cancelled: result='cancelled'
        return result,clock[0],clicks,calls,images

    def test_ignored_click_retries_early_with_a_fresh_match(self):
        result,elapsed,clicks,_,_=self.enter(ignored=1)
        self.assertTrue(result)
        self.assertEqual(len(clicks),2)
        self.assertLess(clicks[1][0],2)
        self.assertNotEqual(clicks[0][1],clicks[1][1])
        self.assertLess(elapsed,3)

    def test_loading_frames_are_never_clicked(self):
        result,_,clicks,_,images=self.enter(loading=3)
        self.assertTrue(result);self.assertEqual(len(clicks),1)
        self.assertTrue(any(scene=='loading' for _,scene in images))

    def test_cancel_before_retry_does_not_click_again(self):
        result,_,clicks,_,_=self.enter(ignored=1,cancel=True)
        self.assertEqual(result,'cancelled');self.assertEqual(len(clicks),1)

    def test_normal_scene_skips_failure_probes_and_uses_one_post_click_image(self):
        result,_,clicks,calls,images=self.enter()
        self.assertTrue(result);self.assertEqual(len(images),2)
        self.assertEqual(len(clicks),1)
        self.assertNotIn('Result_FailedClear',[n for n,_ in calls])
        self.assertNotIn('Nav_BlockedDialog',[n for n,_ in calls])


if __name__=='__main__':unittest.main()
