"""Native ROI checks on offline SWF artwork; no desktop or game connection."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('MAINLINE_ASSET_SOURCE','__offline_assets_not_configured__'))
SCENES=Path(os.environ.get('MAINLINE_SCENE_FIXTURES','__scene_fixtures_not_configured__'))


def map_frame(stage, state=1, *, normal=True):
    """Independent full map render, not a template pasted onto a blank image."""
    import numpy as np
    from PIL import Image
    sys.path.insert(0,str(ROOT/'tools/research'))
    from import_scroll_landmarks import bounds_reader
    metadata=json.loads((ROOT/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf8'))
    province=next(p for p in metadata['provinces'] if p['id']==stage['town'])
    definitions,bounds,transform=bounds_reader(ET.parse(SOURCE/(stage['town']+'.xml')).getroot())
    directory=SOURCE/stage['town']/'sprites'
    def folder(ident):return next(directory.glob('DefineSprite_'+str(ident)+'_*'),directory/('DefineSprite_'+str(ident)))
    container=str(province['container']); full=Image.new('RGBA',(1500 if stage['town']=='town5' else 940,590))
    origin=bounds(container)
    with Image.open(folder(container)/'1.png') as raw:full.alpha_composite(raw.convert('RGBA'),(round(origin[0]),round(origin[1])))
    placement=next(p for p in definitions[container].find('subTags') if p.get('name')=='s'+str(stage['id']))
    matrix=placement.find('matrix'); box=transform(bounds(placement.get('characterId')),matrix)
    with Image.open(folder(placement.get('characterId'))/f'{state}.png') as raw:
        sprite=raw.convert('RGBA').resize((round(raw.width*float(matrix.get('scaleX','1'))),
            round(raw.height*float(matrix.get('scaleY','1')))),Image.Resampling.BICUBIC)
        full.alpha_composite(sprite,(round(box[0]),round(box[1])))
    offset=stage['offset']
    frame=full.crop((offset,0,offset+940,590)).resize((1147,720),Image.Resampling.BICUBIC).convert('RGB')
    images=ROOT/'assets/resource/base/image'
    with Image.open(images/'任务.png') as task:frame.paste(task.convert('RGB'),(400,630))
    if not normal and stage['town'] not in ('town1', 'town2'):
        from PIL import ImageDraw
        ImageDraw.Draw(frame).rectangle((980,580,1147,720),fill='black')
    elif normal and stage['town'] not in ('town1', 'town2'):
        with Image.open(images/'navigation/scroll/normal-mode.png') as mode:
            rgb=mode.convert('RGB');a=np.asarray(rgb)
            mask=Image.fromarray(np.where((a==[0,255,0]).all(axis=2),0,255).astype('uint8'))
            frame.paste(rgb,(1036,617),mask)
    return np.asarray(frame)[:,:,::-1].copy()


@unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1' and SOURCE.is_dir() and SCENES.is_dir(),
                     'requires explicitly configured offline source fixtures and native memory tests')
class MainlineNativeTests(unittest.TestCase):
    def test_all_entries_and_common_stage_controls(self):
        import cv2
        import numpy as np
        from maa.controller import CustomController
        from maa.custom_action import CustomAction
        from maa.resource import Resource
        from maa.tasker import Tasker
        sys.path.insert(0,str(ROOT/'agent'))
        from battle_ui import NoRevivePrompt
        data=json.loads((ROOT/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf8'))['stages']
        from test_shared_stage_entry import with_stage_controls
        errors=[];evidence=[]
        class Pixels(CustomController):
            def connect(self):return True
            def request_uuid(self):return 'mainline-offline-memory-only'
            def screencap(self):return np.zeros((720,1147,3),dtype='uint8')
            def forbidden(self,*args):raise AssertionError('No input is allowed in recognition tests')
            click=swipe=touch_down=touch_up=touch_move=key_down=key_up=input_text=start_app=stop_app=forbidden
        class Inspect(CustomAction):
            def run(self,ctx,argv):
                for stage in data:
                    target=f'Mainline_{stage["id"]:03d}_Target'
                    for state in (1,2,3):
                        frame=map_frame(stage,state)
                        result=ctx.run_recognition(target,frame)
                        if not result or not result.hit:errors.append(('entry',stage['id'],state))
                        else:
                            x,y,w,h=result.box
                            roi=stage['roi']
                            if not (roi[0]<=x+w/2<=roi[0]+roi[2] and roi[1]<=y+h/2<=roi[1]+roi[3]):errors.append(('wrong_box',stage['id'],list(result.box)))
                        evidence.append((stage['id'],state,bool(result and result.hit)))
                    frame=map_frame(stage,normal=False)
                    result=ctx.run_recognition(target,frame)
                    expected = stage['town'] in ('town1', 'town2')
                    if bool(result and result.hit) != expected:
                        errors.append(('mode_guard_scope',stage['id'],expected))
                    frame=map_frame(stage);frame[600:]=0
                    result=ctx.run_recognition(target,frame)
                    if result and result.hit:errors.append(('missing_map_guard',stage['id']))
                scene_frames={stage['id']:cv2.resize(cv2.imread(str(SCENES/f's{stage["id"]:03d}.png')),(1147,720),interpolation=cv2.INTER_CUBIC) for stage in data}
                for stage in data:
                    recognizer=f'Mainline_{stage["id"]:03d}_Play'
                    # Background identity is intentionally irrelevant. Both a
                    # different stage and this stage need the same common HUD.
                    for frame in (scene_frames[stage['id']],scene_frames[47]):
                        result=ctx.run_recognition(recognizer,with_stage_controls(frame))
                        if not result or not result.hit:errors.append(('shared_hud',stage['id']))
                    for frame in (scene_frames[stage['id']],):
                        result=ctx.run_recognition(recognizer,frame)
                        if result and result.hit:errors.append(('background_only',stage['id']))
                print('MAINLINE_NATIVE '+json.dumps(dict(entries=len(evidence),shared_stage_cases=len(data)*3,errors=errors)),flush=True)
                return True
        with tempfile.TemporaryDirectory(prefix='mainline-native-',ignore_cleanup_errors=True) as temp:
            Tasker.set_log_dir(Path(temp)/'logs')
            resource,controller,tasker=Resource(),Pixels(),Tasker()
            resource.register_custom_recognition('battle_no_revive',NoRevivePrompt())
            self.assertTrue(controller.post_connection().wait().succeeded)
            controller.set_screenshot_use_raw_size(True)
            self.assertTrue(resource.post_bundle(ROOT/'assets/resource/base').wait().succeeded)
            resource.register_custom_action('__mainline_inspect',Inspect())
            resource.override_pipeline({'__MainlineInspect':{'action':{'type':'Custom','param':{'custom_action':'__mainline_inspect'}}}})
            self.assertTrue(tasker.bind(resource,controller))
            self.assertTrue(tasker.post_task('__MainlineInspect').wait().succeeded)
            self.assertEqual(len(evidence),186)
            self.assertEqual(errors,[])


if __name__=='__main__':unittest.main()
