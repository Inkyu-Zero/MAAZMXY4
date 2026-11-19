"""Scene landmarks must survive animation, without a desktop/game connection."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT/'tests/fixtures/navigation/buzhou-live'
sys.path.insert(0, str(ROOT/'tools/research'))


class SceneExtractionTests(unittest.TestCase):
    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ('PIL','numpy','cv2')),
                         'requires offline scene extraction dependencies')
    def test_partial_regeneration_preserves_unselected_scenes_and_rejects_unknown_ids(self):
        from PIL import Image
        import numpy as np
        import import_mainline_scenes as extractor

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root/'agent/data/navigation'
            data.mkdir(parents=True)
            stages = [{'id':i,'name':'fixture-'+str(i),'map_url':'map-'+str(i)} for i in (45,46)]
            (data/'mainline-stages.json').write_text(json.dumps({'viewport':[1147,720],'stages':stages}), encoding='utf8')
            metadata = {'version':1,'viewport':[1147,720],'future_field':{'keep':True},
                        'stages':[dict(stage, checks=[{'preserve':'original'}]) for stage in stages]}
            (data/'mainline-scenes.json').write_text(json.dumps(metadata), encoding='utf8')
            pixels = np.random.default_rng(46).integers(0,256,(590,940,3),dtype='uint8')
            image = Image.fromarray(pixels).convert('RGBA')
            mask = Image.new('L',image.size)
            with patch.object(extractor, 'render', return_value=(image,mask,{'missing_layers':[]})) as render:
                result = extractor.generate(root, root/'evidence', root, stage_ids=[46])
                self.assertEqual([call.args[1] for call in render.call_args_list], ['map-46']*3)
                self.assertEqual(result['stages'][0], metadata['stages'][0])
                self.assertEqual(result['future_field'], metadata['future_field'])
                self.assertEqual(len(result['stages'][1]['checks']), 2)
                before = (data/'mainline-scenes.json').read_bytes()
                with self.assertRaisesRegex(ValueError, 'Unknown stage ids'):
                    extractor.generate(root, root/'bad-evidence', root, stage_ids=[999])
                self.assertEqual((data/'mainline-scenes.json').read_bytes(), before)

    @unittest.skipUnless(importlib.util.find_spec('PIL') and importlib.util.find_spec('numpy'),
                         'requires Pillow/numpy extraction dependencies')
    def test_unsampled_animation_is_excluded_but_static_opaque_cover_is_safe(self):
        from PIL import Image
        import import_mainline_scenes as extractor

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root/'exports/fixture'
            (folder/'images').mkdir(parents=True)
            (folder/'DefineSprite_10_bg31').mkdir()
            (root/'maps').mkdir()
            (root/'agent/data/navigation').mkdir(parents=True)
            (root/'agent/data/navigation/mainline-stages.json').write_text(json.dumps({'stages':[
                {'map_url':'fixture','scene_layout':{'height':590,'screen_width':940,
                    'birth_point':[150,300],'cartoons':''}}]}), encoding='utf8')
            (root/'maps/fixture.dat').write_text('''<map>
                <imagelayer name="bg30"/>
                <imagelayer name="bg31"><properties><property name="x" value="300"/>
                    <property name="y" value="150"/></properties></imagelayer>
                <imagelayer name="bg10"><properties><property name="x" value="320"/>
                    <property name="y" value="170"/></properties></imagelayer>
                </map>''', encoding='utf8')
            (root/'maps/fixture.swf').write_bytes(b'fixture')
            (folder/'symbols.csv').write_text('10;bg31\n', encoding='utf8')
            (folder/'map.xml').write_text('''<swf><tags>
                <item type="DefineShapeTag" shapeId="11"><shapeBounds
                    Xmin="0" Ymin="0" Xmax="1600" Ymax="1600"/></item>
                <item type="DefineSpriteTag" spriteId="10" frameCount="60"><subTags>
                    <item type="PlaceObject2Tag" characterId="11"/>
                    <item type="ShowFrameTag"/></subTags></item>
                </tags></swf>''', encoding='utf8')
            Image.new('RGBA', (940,590), '#444455').save(folder/'images/1_bg30.png')
            # The first three exported frames are identical, but the authored
            # sprite lasts 60 frames. Sampling alone cannot prove stability.
            for frame in (1,2,3):
                Image.new('RGBA', (80,80), '#dd3344').save(folder/f'DefineSprite_10_bg31/{frame}.png')
            cover = Image.new('RGBA', (40,20), (255,255,255,255))
            cover.paste((255,255,255,128), (20,0,40,20))
            cover.save(folder/'images/2_bg10.png')
            with patch.object(extractor, 'ROOT', root):
                _, mask, _ = extractor.render(root, 'fixture', camera=(0,0))
            self.assertEqual(mask.getpixel((310,160)), 255, 'An unsampled animation is not a stable landmark')
            self.assertEqual(mask.getpixel((330,180)), 0, 'Opaque static foreground hides the animation')
            self.assertEqual(mask.getpixel((350,180)), 255, 'Partial transparency still exposes animation')
            self.assertEqual(mask.getpixel((400,160)), 0)
            extractor.sprite_geometry.cache_clear()

    def test_stored_failure_fixtures_have_reviewed_masks_and_hashes(self):
        manifest = json.loads((FIXTURES/'cases.json').read_text(encoding='utf8'))
        self.assertEqual(len(manifest['cases']), 2)
        for case in manifest['cases']:
            self.assertEqual(hashlib.sha256((FIXTURES/case['file']).read_bytes()).hexdigest(), case['sha256'])
            self.assertEqual(case['masks'], [[0,0,350,720],[0,600,1148,720]])


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS') == '1', 'opt-in Maa memory-only recognition')
class BuzhouSceneNativeTests(unittest.TestCase):
    def test_archived_backgrounds_need_shared_hud_and_other_screens_are_rejected(self):
        from test_shared_stage_entry import with_stage_controls
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        from fixture_pixels import read_png_bgr
        sys.path.insert(0, str(ROOT/'agent'))
        from battle_ui import NoRevivePrompt

        frames = [read_png_bgr(FIXTURES/f's046-phase-{i}.png') for i in (1,2)]
        controls = [ROOT/'tests/fixtures/navigation'/name for name in
                    ('task-map-real.png','settings-menu.png','login.png')]
        scene_directory = os.environ.get('MAINLINE_SCENE_FIXTURES')
        offline_scenes = sorted(Path(scene_directory).glob('s[0-9][0-9][0-9].png')) if scene_directory else []
        results = []
        failures = []

        class Pixels(CustomController):
            def connect(self): return True
            def request_uuid(self): return 'buzhou-scene-regression-memory-only'
            def screencap(self): return frames[0].copy()
            def forbidden(self, *args): raise AssertionError('No input allowed')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden

        class Inspect(CustomAction):
            def run(self, context, argv):
                try:
                    for index, frame in enumerate(frames):
                        detail = context.run_recognition('Mainline_046_Play', with_stage_controls(frame))
                        results.append(('phase-'+str(index+1), bool(detail and detail.hit), True))
                    for path in controls:
                        detail = context.run_recognition('Mainline_046_Play', read_png_bgr(path))
                        results.append((path.stem, bool(detail and detail.hit), False))
                    if offline_scenes:
                        from PIL import Image
                        import numpy as np
                        for path in offline_scenes:
                            with Image.open(path) as original:
                                frame = np.asarray(original.convert('RGB').resize((1147,720),Image.Resampling.BICUBIC))[:,:,::-1].copy()
                            detail = context.run_recognition('Mainline_046_Play', with_stage_controls(frame))
                            results.append((path.stem, bool(detail and detail.hit), True))
                    return True
                except Exception as error:
                    failures.append(repr(error))
                    return False

        with tempfile.TemporaryDirectory(prefix='buzhou-native-', ignore_cleanup_errors=True) as temp:
            Tasker.set_log_dir(Path(os.environ.get('MAAZMXY4_TEST_LOG_DIR', temp))/'buzhou-scene')
            resource, controller, tasker = Resource(), Pixels(), Tasker()
            resource.register_custom_recognition('battle_no_revive', NoRevivePrompt())
            self.assertTrue(controller.post_connection().wait().succeeded)
            controller.set_screenshot_use_raw_size(True)
            self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
            resource.register_custom_action('__buzhou_scene_test', Inspect())
            self.assertTrue(resource.override_pipeline({'__BuzhouSceneTest':{
                'action':{'type':'Custom','param':{'custom_action':'__buzhou_scene_test'}}}}))
            self.assertTrue(tasker.bind(resource, controller))
            self.assertTrue(tasker.post_task('__BuzhouSceneTest').wait().succeeded, failures)
        self.assertEqual(len(results), 5+len(offline_scenes))
        if scene_directory: self.assertEqual(len(offline_scenes), 62, 'The complete offline stage inventory is required')
        self.assertEqual([(case,hit) for case,hit,expected in results if hit != expected], [], results)


if __name__ == '__main__':
    unittest.main()
