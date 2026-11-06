"""Import province landmarks from pinned SWF exports, without running the game."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from PIL import Image
import numpy as np

from import_scroll_landmarks import bounds_reader, SCALE
from import_stage_variants import opaque_rectangle

ROOT = Path(__file__).resolve().parents[2]


def generate(source, root=ROOT):
    import cv2
    bank_path = root / 'tools/navigation_library.json'
    bank = json.loads(bank_path.read_text(encoding='utf8'))
    origins = {s['name']: s for s in json.loads((source/'manifest.json').read_text(encoding='utf8'))}
    image_root = root/'assets/resource/base/image/navigation/hubs'
    image_root.mkdir(parents=True, exist_ok=True)
    metadata = dict(version=1, assets=[], portals=[],
        evidence='TownMapMediator maps st1/17/30/40/47 to town1/2/3/4/5; current town0 has st63 and Dungeon.xml starts town6 at 63. Clicks use matched native template boxes, never placement coordinates.')
    for town, ident in [('town1','122'), ('town2','94'), ('town3','78'), ('town4','57')]:
        origin = origins[town]
        if hashlib.sha256((source/origin['file']).read_bytes()).hexdigest() != origin['sha256']:
            raise ValueError('Map source hash changed: ' + town)
        xml = ET.parse(source/(town+'.xml')).getroot()
        definitions, bounds, transform = bounds_reader(xml)
        ox, oy, _, _ = bounds(ident)
        excluded = []
        for placement in definitions[ident].find('subTags'):
            if re.fullmatch(r's\d+', placement.get('name','')):
                b = transform(bounds(placement.get('characterId')), placement.find('matrix'))
                excluded.append((b[0]-ox-10,b[1]-oy-10,b[2]-ox+10,b[3]-oy+10))
        folder = next((source/town/'sprites').glob('DefineSprite_'+ident+'*'))
        full = Image.open(folder/'1.png').convert('RGBA')
        if town == 'town1':
            fixture = root/'tests/fixtures/navigation/hubs/nanzhan-source.png'
            fixture.parent.mkdir(parents=True,exist_ok=True)
            full.crop(tuple(round(v) for v in (-ox,-oy,940-ox,590-oy))).resize(
                (1147,720),Image.Resampling.BICUBIC).convert('RGB').save(fixture)
            metadata['fixture'] = dict(path='hubs/nanzhan-source.png',source=origin,
                sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
                note='Offline SWF map render; tests add a synthetic toolbar, not a real game screenshot.')
        pixels = np.array(full)
        candidates = []
        for y in range(160,421,20):
            for x in range(150,800,25):
                box = (x,y,x+75,y+65)
                if any(box[0]<b[2] and box[2]>b[0] and box[1]<b[3] and box[3]>b[1] for b in excluded): continue
                sample = pixels[y:y+65,x:x+75]
                if sample.shape != (65,75,4) or sample[:,:,3].min()<245: continue
                gray = sample[:,:,:3].mean(axis=2)
                if gray.std() < 12: continue
                candidates.append((float(np.abs(np.diff(gray,axis=0)).mean()+np.abs(np.diff(gray,axis=1)).mean()), box))
        selected=[]
        for _,box in sorted(candidates,reverse=True):
            if any(abs(box[0]-b[0])+abs(box[1]-b[1])<150 for b in selected): continue
            selected.append(box)
            if len(selected)==2: break
        if len(selected)!=2: raise ValueError('Missing distinct terrain landmarks: '+town)
        templates=[]
        for i,box in enumerate(selected):
            file=image_root/f'{town}-{i}.png'
            full.crop(box).resize((round(75*SCALE),round(65*SCALE)),Image.Resampling.LANCZOS).convert('RGB').save(file)
            relative='navigation/hubs/'+file.name;templates.append(relative)
            metadata['assets'].append(dict(path=relative,crop=list(box),source_origin=[ox,oy],source=origin,sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
        bank['nodes']['Nav_Province_'+town]=dict(deepcopy(bank['templates']['read']),
            recognition=dict(type='TemplateMatch',param=dict(template=templates,roi=[120,135,920,465])))
    scroll=json.loads((root/'assets/resource/base/pipeline/game_management_scroll.json').read_text(encoding='utf8'))
    for town in ['town5','town6']:
        recognition=deepcopy(scroll['GameManagement_'+town+'_Terrain']['recognition'])
        recognition['param']['roi']=[50,120,990,480]
        bank['nodes']['Nav_Province_'+town]=dict(deepcopy(bank['templates']['read']),recognition=recognition)
    # Existing redacted-world crops have exact source rectangles and hashes in
    # library.json. These rectangles restrict search, not an unverified click.
    portals=[('town1',7,[397,460,330,202],1),('town2',8,[865,188,247,266],17),
             ('town3',2,[427,33,228,193],30),('town4',4,[44,207,250,241],40),
             ('town5',6,[88,471,262,189],47),('town6',3,[694,36,243,213],63)]
    world_xml=ET.parse(source/'town0.xml').getroot()
    if hashlib.sha256((source/origins['town0']['file']).read_bytes()).hexdigest()!=origins['town0']['sha256']:
        raise ValueError('World source hash changed')
    world_path=root/'tests/fixtures/navigation/world.png'
    world=np.array(Image.open(world_path).convert('RGB'))
    for town,landmark,reference_roi,stage in portals:
        placement=next(e for e in world_xml.iter() if e.get('name')=='st'+str(stage))
        sprite=placement.get('characterId');matrix=placement.find('matrix')
        frame=next((source/'town0/sprites').glob('DefineSprite_'+sprite+'*'))/'1.png'
        raw=Image.open(frame).convert('RGBA')
        sx=float(matrix.get('scaleX','1'))*SCALE;sy=float(matrix.get('scaleY','1'))*SCALE
        rendered=raw.resize((round(raw.width*sx),round(raw.height*sy)),Image.Resampling.LANCZOS)
        pixels=np.array(rendered);x,y,w,h=opaque_rectangle(pixels[:,:,3])
        # Insets keep the entire native random-click rectangle inside the
        # button's opaque artwork, away from transparent/overlapping edges.
        x+=8;y+=8;w-=16;h-=16
        rx,ry,rw,rh=reference_roi
        crop=pixels[y:y+h,x:x+w,:3].copy()
        scores=cv2.matchTemplate(world[ry:ry+rh,rx:rx+rw].copy(),crop,cv2.TM_CCOEFF_NORMED)
        _,score,_,position=cv2.minMaxLoc(scores)
        if score < .93:raise ValueError('World portal artwork needs review: '+town)
        px,py=rx+position[0],ry+position[1]
        path=image_root/('portal-'+town+'.png')
        Image.fromarray(world[py:py+h,px:px+w]).save(path)
        relative='navigation/hubs/'+path.name
        roi=[px-12,py-12,w+24,h+24]
        metadata['assets'].append(dict(path=relative,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            crop=[px,py,w,h],source=dict(path='tests/fixtures/navigation/world.png',sha256=hashlib.sha256(world_path.read_bytes()).hexdigest()),
            shape_source=origins['town0'],sprite=sprite,opaque_crop=[x,y,w,h],source_match_score=round(score,6)))
        probe='Nav_Portal_'+town
        bank['recognizers'][probe]=dict(type='TemplateMatch',param=dict(template=relative,roi=roi))
        name='Nav_WorldNanzhan' if town=='town1' else 'Nav_World_'+town
        bank['actions'][name]=dict(base='click',all_of=['Nav_See_world',probe,'Result_NoRevivePrompt'],box_index=1,
                                  source='world',destination='map',label='世界地图 → '+dict(town1='南瞻部洲',town2='东胜神洲',town3='北俱芦洲',town4='西牛贺洲',town5='幽冥血海',town6='六欲魔天')[town])
        bank['routines']['actions'][name]=dict(**{'from':['world']},verify=['map'],label=bank['actions'][name]['label'])
        metadata['portals'].append(dict(town=town,instance='st'+str(stage),template=relative,
            roi=roi,click_region=[px,py,w,h],reference_roi=reference_roi))
    bank['nodes']['Nav_Nanzhan']=dict(deepcopy(bank['templates']['read']),recognition=dict(type='And',param=dict(all_of=['Nav_ClearMap','Nav_Province_town1'])))
    # The full toolbar crop includes map background: it scored only .927 on
    # another province. The opaque compass interior matches both at 1.000,
    # without weakening the original threshold or retaining an old position.
    original=root/'assets/resource/base/image/navigation/templates/map-a/world.png'
    compass=image_root/'world-compass.png';crop=[23,15,70,68]
    with Image.open(original) as im:im.crop(crop).save(compass)
    metadata['assets'].append(dict(path='navigation/hubs/world-compass.png',crop=crop,
        source=dict(path='navigation/templates/map-a/world.png',sha256=hashlib.sha256(original.read_bytes()).hexdigest()),
        sha256=hashlib.sha256(compass.read_bytes()).hexdigest()))
    bank['recognizers']['Nav_WorldCompass']=dict(type='TemplateMatch',param=dict(
        template='navigation/hubs/world-compass.png',roi=[1050,25,97,100]))
    bank['actions']['Nav_OpenWorld']['all_of'][1]='Nav_WorldCompass'
    bank['routines']['screens']['nanzhan']=dict(detector='Nav_Nanzhan',label='南瞻部洲',exit='Nav_OpenWorld')
    bank['routines']['routes']['nanzhan']=['Nav_WorldNanzhan','Nav_OpenWorld','Nav_Go_task_map_world']
    variants=json.loads((root/'agent/data/navigation/stage-variants.json').read_text(encoding='utf8'))
    bank['routines']['stages']={s['task']+'-关卡入口':s['town'] for s in variants['stages']}
    bank_path.write_text(json.dumps(bank,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    (root/'agent/data/navigation/hubs.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    generate(parser.parse_args().source)
