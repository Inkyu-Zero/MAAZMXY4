"""A shared paid-item decision must not retain debug images or cross frames."""
from pathlib import Path
from types import SimpleNamespace
import sys
import os
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from navigation.recognition import FrameRecognition, _pixels, _probe, clear_cache, modal_present


class Frame:
    shape=(720,1147,3)
    def __init__(self,value=b'frame'):self.value=value
    def tobytes(self):return self.value


class SharedRecognitionTests(unittest.TestCase):
    def setUp(self):
        clear_cache();self.calls=[]
        def match(name,frame):
            self.calls.append(name)
            return SimpleNamespace(hit=False,box=None,raw_image=object(),draw_images=[object()])
        self.context=SimpleNamespace(tasker=SimpleNamespace(_handle=1),run_recognition=match)
    def test_normal_frame_has_one_additional_modal_probe_across_all_callers(self):
        for _ in range(20):self.assertFalse(modal_present(self.context,Frame()))
        self.assertEqual(self.calls,['Nav_ModalPresent'])
    def test_changed_pixels_and_controller_scope_require_a_fresh_decision(self):
        modal_present(self.context,Frame())
        modal_present(self.context,Frame(b'changed'))
        self.context.tasker._handle=2
        modal_present(self.context,Frame(b'changed'))
        self.assertEqual(len(self.calls),3)
    def test_cache_retains_no_screenshot_or_debug_buffer(self):
        result=_probe(self.context,Frame(),'Nav_ModalPresent')
        self.assertEqual(set(vars(result)),{'hit','box'})
    def test_unavailable_guard_is_not_cached_as_clear(self):
        self.context.run_recognition=lambda *_:None
        with self.assertRaises(ValueError):modal_present(self.context,Frame())
    def test_resource_registration_invalidates_previous_results(self):
        modal_present(self.context,Frame());clear_cache();modal_present(self.context,Frame())
        self.assertEqual(len(self.calls),2)

    def test_executed_compound_children_reuse_decisions_without_fetching_images(self):
        def match(name, frame):
            self.calls.append(name)
            return SimpleNamespace(hit=False, box=None, raw_detail=[
                dict(name='Nav_Probe_hub_world_entry', algorithm='TemplateMatch', box=None),
                dict(name='Nav_ModalPresent', algorithm='Or', box=None, detail=[
                    dict(name='Result_ReviveEvidence', algorithm='TemplateMatch', box=None)]),
                dict(name='unrelated', algorithm='TemplateMatch', box=[1,2,3,4])])
        self.context.run_recognition = match
        observation = FrameRecognition(self.context, Frame())
        self.assertFalse(observation.any_of('Nav_StageOverlay', ('Nav_Probe_hub_world_entry', 'Nav_ModalPresent')))
        self.assertFalse(observation.probe('Nav_ModalPresent').hit)
        self.assertFalse(observation.probe('Result_ReviveEvidence').hit)
        self.assertEqual(self.calls, ['Nav_StageOverlay'])
        observation.probe('unrelated')
        self.assertEqual(self.calls[-1], 'unrelated')

    def test_short_circuit_does_not_invent_unexecuted_child_results(self):
        self.context.run_recognition = lambda name, frame: (
            self.calls.append(name) or SimpleNamespace(hit=True, box=[1,2,3,4], raw_detail=[
                dict(name='Nav_CommonConfirm', algorithm='And', box=[1,2,3,4])]))
        observation = FrameRecognition(self.context, Frame())
        observation.probe('Nav_ModalPresent')
        observation.probe('Result_ReviveEvidence')
        self.assertEqual(self.calls, ['Nav_ModalPresent', 'Result_ReviveEvidence'])

    def test_contiguous_pixels_hash_without_allocating_a_bytes_copy(self):
        import numpy as np
        frame = np.arange(36, dtype=np.uint8).reshape(3,4,3)
        self.assertIsInstance(_pixels(frame), memoryview)
        self.assertEqual(bytes(_pixels(frame)), frame.tobytes())
        self.assertEqual(bytes(_pixels(frame[:,::2])), frame[:,::2].tobytes())


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1','opt-in native same-frame accounting')
class NativeSharedRecognitionTests(unittest.TestCase):
    def test_native_decline_wait_and_map_candidates_share_one_modal_probe(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from battle_ui import NoRevivePrompt
        from fixture_pixels import read_png_bgr
        root=Path(__file__).resolve().parents[1]
        frames=[read_png_bgr(root/'tests/fixtures/navigation/map-a.png'),
                read_png_bgr(root/'tests/fixtures/navigation/revival/normal-p1-up.png')]
        frames[1][385:550,270:900]=0  # Caption remains, cancel frame is unavailable.
        calls=[];errors=[]
        class Proxy:
            def __init__(self,c):self.context=c
            def __getattr__(self,n):return getattr(self.context,n)
            def run_recognition(self,name,frame):
                if name in ('Nav_ModalPresent','Result_ReviveEvidence'):calls.append(name)
                return self.context.run_recognition(name,frame)
        class Guard(NoRevivePrompt):
            def analyze(self,context,argv):return super().analyze(Proxy(context),argv)
        class Pixels(CustomController):
            index=0
            def connect(self):return True
            def request_uuid(self):return 'one-modal-check-per-frame'
            def screencap(self):return frames[self.index].copy()
            def forbidden(self,*args):raise AssertionError('No cancel button is clickable in these frames')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        controller=Pixels()
        class Inspect(CustomAction):
            def run(self,context,argv):
                try:
                    for index in (0,1):
                        controller.index=index;clear_cache();calls.clear()
                        result=context.run_task('__GuardFanout',{
                            '__GuardFanout':dict(action=dict(type='DoNothing'),post_delay=0,timeout=1000,
                                next=['Result_DeclineRevive','Result_RevivePrompt','Nav_CachedModal','Nav_ClearMap']),
                        })
                        assert result and result.status.succeeded
                        assert calls.count('Nav_ModalPresent')==1,(index,calls)
                        # With its lower frame erased, the modal is identified
                        # by its caption; that executed child is already cached.
                        assert calls.count('Result_ReviveEvidence')==0,(index,calls)
                    return True
                except Exception:
                    import traceback
                    errors.append(traceback.format_exc());return False
        Tasker.set_log_dir(Path(os.environ.get('MAA_MODAL_TEST_LOG', str(root/'.runtime-cache/native-modal-cache'))))
        resource,tasker=Resource(),Tasker()
        resource.register_custom_recognition('battle_no_revive',Guard())
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(root/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__guard_count',Inspect())
        resource.override_pipeline({'__GuardCount':dict(action=dict(type='Custom',param=dict(custom_action='__guard_count')))})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__GuardCount').wait().succeeded,'\n'.join(errors))


if __name__=='__main__':unittest.main()
