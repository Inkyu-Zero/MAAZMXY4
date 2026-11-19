"""Shared warning skin recognition, including unrelated-screen negatives."""
import json
import os
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('MAA_RESULT_UI_TESTS')=='1','opt-in memory-only UI recognition')
class CommonDialogNativeTests(unittest.TestCase):
    def test_confirm_dialog_and_unrelated_screens(self):
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        folder=ROOT/'tests/fixtures/navigation/common-dialog'
        positives=[folder/c['file'] for c in json.loads((folder/'cases.json').read_text(encoding='utf8'))['cases']]
        # The sell-confirmation fixture uses this exact shared WarnBar skin.
        positives.append(ROOT/'tests/fixtures/navigation/profile-sell.png')
        # This retained log image is a paid revival panel, not a clear screen.
        positives.append(ROOT/'tests/fixtures/navigation/revival-threshold-20261103.png')
        # Resurrection uses the same three corners. Its precise caption still
        # selects the known decline route; an unreadable caption remains blocked.
        positives += [p for p in (ROOT/'tests/fixtures/navigation/revival').glob('*.png')
                      if not p.name.startswith('return-')]
        negatives=[p for p in (ROOT/'tests/fixtures/navigation').glob('*.png') if p not in positives]
        negatives+=list((ROOT/'tests/fixtures/navigation/revival').glob('return-*.png'))
        errors=[];checked=[]
        class Pixels(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'common-dialog-memory-only'
            def screencap(self):return read_png_bgr(positives[0])
            def forbidden(self,*args):raise AssertionError('No desktop input allowed')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        class Inspect(CustomAction):
            def run(self,context,argv):
                for path,expected in [(p,True) for p in positives]+[(p,False) for p in negatives]:
                    result=context.run_recognition('Nav_CommonConfirm',read_png_bgr(path))
                    if result is None or bool(result.hit)!=expected:errors.append((path.name,expected,None if result is None else result.hit))
                    checked.append(path)
                return True
        log=Path(os.environ.get('MAA_RESULT_TEST_LOG',str(ROOT/'.runtime-cache/common-dialog-native')))
        log.mkdir(parents=True,exist_ok=True);Tasker.set_log_dir(log)
        resource,controller,tasker=Resource(),Pixels(),Tasker()
        self.assertTrue(controller.post_connection().wait().succeeded)
        controller.set_screenshot_use_raw_size(True)
        self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
        resource.register_custom_action('__inspect_common_dialog',Inspect())
        resource.override_pipeline({'__CommonDialog':{'action':{'type':'Custom','param':{'custom_action':'__inspect_common_dialog'}}}})
        self.assertTrue(tasker.bind(resource,controller))
        self.assertTrue(tasker.post_task('__CommonDialog').wait().succeeded)
        self.assertEqual(len(checked),len(positives)+len(negatives))
        self.assertEqual(errors,[])


if __name__=='__main__':unittest.main()
