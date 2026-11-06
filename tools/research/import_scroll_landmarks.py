"""Extract non-interactive map terrain for bounded, recognized post-login drags."""
from functools import lru_cache
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[2]
SCALE=720/590


def bounds_reader(xml):
    definitions={}
    for node in xml.iter():
        if not node.get('type','').startswith('Define'):continue
        for field in ('spriteId','shapeId','buttonId','textId','characterID'):
            if node.get(field):definitions[node.get(field)]=node
    def transform(box,matrix):
        if matrix is None:return box
        sx=float(matrix.get('scaleX','1'));sy=float(matrix.get('scaleY','1'))
        rx=float(matrix.get('rotateSkew0','0'));ry=float(matrix.get('rotateSkew1','0'))
        tx=float(matrix.get('translateX','0'))/20;ty=float(matrix.get('translateY','0'))/20
        points=[(sx*x+ry*y+tx,rx*x+sy*y+ty) for x in (box[0],box[2]) for y in (box[1],box[3])]
        return min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)
    @lru_cache(None)
    def bounds(ident):
        node=definitions[ident]
        for field in ('shapeBounds','textBounds','bounds','startBounds'):
            rect=node.find(field)
            if rect is not None:return tuple(float(rect.get(k))/20 for k in ('Xmin','Ymin','Xmax','Ymax'))
        boxes=[]
        if node.get('type')=='DefineButton2Tag':
            children=[e for e in node.find('characters') if e.get('buttonStateUp')=='true']
        else:
            children=[]
            sub_tags=node.find('subTags')
            for e in ([] if sub_tags is None else sub_tags):
                if e.get('type')=='ShowFrameTag':break
                if e.get('characterId'):children.append(e)
        for child in children:
            boxes.append(transform(bounds(child.get('characterId')),child.find('matrix')))
        if not boxes:raise ValueError('Missing drawable bounds: '+ident)
        return min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)
    return definitions,bounds,transform


def generate(source,root=ROOT):
    sources={s['name']:s for s in json.loads((source/'manifest.json').read_text(encoding='utf8'))}
    image_root=root/'assets/resource/base/image/navigation/scroll';image_root.mkdir(parents=True,exist_ok=True)
    fixture_root=root/'tests/fixtures/game-management/scroll';fixture_root.mkdir(parents=True,exist_ok=True)
    metadata=dict(version=1,maps=[],assets=[],state_evidence='Native town areas are restored from curRecord.areaId; levels_container starts at its authored position. Terrain crops exclude every clickable stage bound.')
    nodes={}
    for town,container,task,axis,steps in [('town5','448','伽岚（万世冥阁）','x',3),('town6','35','贪妄（金蟾宝宫）','y',12)]:
        origin=sources[town]
        if hashlib.sha256((source/origin['file']).read_bytes()).hexdigest()!=origin['sha256']:raise ValueError('Map checksum changed')
        xml=ET.parse(source/(town+'.xml')).getroot();definitions,bounds,transform=bounds_reader(xml)
        full_bounds=bounds(container);ox,oy=full_bounds[:2]
        excluded=[]
        for e in definitions[container].find('subTags'):
            if re.fullmatch(r's\d+',e.get('name','')):
                box=transform(bounds(e.get('characterId')),e.find('matrix'))
                excluded.append([box[0]-ox-12,box[1]-oy-12,box[2]-ox+12,box[3]-oy+12])
        folder=next((source/town/'sprites').glob('DefineSprite_'+container+'*'))
        with Image.open(folder/'1.png') as im:full=im.convert('RGBA')
        terrain=np.array(full);anchors=[];crops=[]
        positions=range(480,1301,60) if axis=='x' else range(270,2401,85)
        for position in positions:
            candidates=[]
            for cross in (range(210,421,20) if axis=='x' else range(430,771,40)):
                x,y=(position,cross) if axis=='x' else (cross,position)
                box=(x,y,x+75,y+65)
                if any(box[0]<b[2] and box[2]>b[0] and box[1]<b[3] and box[3]>b[1] for b in excluded):continue
                sample=terrain[y:y+65,x:x+75]
                if sample.shape!=(65,75,4) or sample[:,:,3].min()<245:continue
                gray=sample[:,:,:3].mean(axis=2)
                score=float(np.abs(np.diff(gray,axis=0)).mean()+np.abs(np.diff(gray,axis=1)).mean())
                if gray.std()>=12:candidates.append((score,box))
            if not candidates:continue
            _,box=max(candidates);crop=full.crop(box).resize((round(75*SCALE),round(65*SCALE)),Image.Resampling.LANCZOS).convert('RGB')
            name=f'{town}-{len(anchors):02}.png';target=image_root/name;crop.save(target)
            relative='navigation/scroll/'+name;anchors.append(relative);crops.append(box)
            metadata['assets'].append(dict(path=relative,crop=box,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),source_sha256=origin['sha256']))
        if len(anchors)<(3 if axis=='x' else 20):raise ValueError('Too few safe terrain landmarks: '+town)
        full.resize((round(full.width*SCALE),round(full.height*SCALE)),Image.Resampling.BICUBIC).convert('RGB').save(fixture_root/(town+'.png'))
        # Include complete 720-scaled patches: x=750 + width=75 reaches
        # 1007 px in town6, and y=420 + height=65 reaches 592 px in town5.
        # The previous right/bottom bounds clipped these visible landmarks.
        roi=[500,140,500,460] if axis=='x' else [160,350,880,225]
        anchor_node='GameManagement_'+town+'_Terrain'
        nodes[anchor_node]=dict(recognition=dict(type='TemplateMatch',param=dict(template=anchors,roi=roi,threshold=.9)),action=dict(type='DoNothing'),next=[])
        drag_node='GameManagement_'+town+'_Drag'
        nodes[drag_node]=dict(recognition=dict(type='And',param=dict(all_of=['Nav_TaskMap','GameManagement_NormalMap',anchor_node,'Result_NoRevivePrompt'],box_index=2)),
            action=dict(type='Swipe',param=dict(begin=True,end=True,end_offset=[-440,0,0,0] if axis=='x' else [0,-280,0,0],duration=450)),
            next=[],post_delay=350,timeout=3000)
        metadata['maps'].append(dict(task=task,target=task+'-关卡入口',town=town,axis=axis,max_steps=steps,drag_node=drag_node,
            source=origin,source_bounds=full_bounds,excluded_stage_bounds=excluded,fixture=town+'.png',crops=crops))
    # TownMap keeps levels_container's position when revisiting a cached town.
    # s47 is on the left, s61 on the right: neither has a fixed screen position.
    # Reuse the same non-interactive terrain; mirror only the safe drag region
    # and direction. A fresh native match determines both gesture endpoints.
    reverse_terrain='GameManagement_town5_TerrainBack'
    nodes[reverse_terrain]=deepcopy(nodes['GameManagement_town5_Terrain'])
    nodes[reverse_terrain]['recognition']['param']['roi']=[60,140,480,460]
    reverse_drag='GameManagement_town5_DragBack'
    nodes[reverse_drag]=deepcopy(nodes['GameManagement_town5_Drag'])
    nodes[reverse_drag]['recognition']['param']['all_of'][2]=reverse_terrain
    nodes[reverse_drag]['action']['param']['end_offset']=[440,0,0,0]
    reverse_recipe=deepcopy(metadata['maps'][0])
    reverse_recipe.update(task='无冥（孤魂岭）',target='无冥（孤魂岭）-关卡入口',drag_node=reverse_drag)
    metadata['maps'].append(reverse_recipe)
    metadata['state_evidence']=(
        'TownMap.changeTown reuses cached levels_container; Drag.onMouseMove clamps its live position '
        'to [-(width-940),0] and [-(height-590),0]. s47 is left of s61 in town5. '
        'Legacy task entries used unrestricted TemplateMatch and the matched click box. '
        'Terrain crops exclude every clickable stage bound; no screen coordinate is retained between frames.')
    normal=next((source/'town5'/'sprites').glob('DefineSprite_453*'))/'1.png'
    with Image.open(normal) as original:
        image=original.convert('RGBA').resize((round(original.width*SCALE),round(original.height*SCALE)),Image.Resampling.LANCZOS)
        pixels=np.array(image);pixels[pixels[:,:,3]<245,:3]=(0,255,0)
        target=image_root/'normal-mode.png';Image.fromarray(pixels[:,:,:3]).save(target)
        fixture=image.resize(image.size,Image.Resampling.BICUBIC)
        fixture.save(fixture_root/'normal-mode.png')
    relative='navigation/scroll/normal-mode.png'
    metadata['assets'].append(dict(path=relative,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),source_sha256=sources['town5']['sha256']))
    # The normal-mode switch says where a click WOULD go: 切至噩梦. It is
    # recognition only. Preserve the sprite threshold and add a strict text
    # alternative for clients whose button rasterization differs from SWF.
    nodes['GameManagement_NormalMap']=dict(recognition=dict(type='Or',param=dict(any_of=[
        dict(recognition=dict(type='TemplateMatch',param=dict(template=relative,roi=[980,580,167,140],threshold=.88,green_mask=True))),
        dict(recognition=dict(type='OCR',param=dict(expected=['^切至噩梦$'],roi=[900,560,247,160],threshold=.85))),
    ])),action=dict(type='DoNothing'),next=[])
    (root/'assets/resource/base/pipeline/game_management_scroll.json').write_text(json.dumps(nodes,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    (root/'agent/data/navigation/stage-scroll.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    resources=root/'agent/data/navigation/resources.json';value=json.loads(resources.read_text(encoding='utf8'))
    if 'game_management_scroll.json' not in value['pipelines']:value['pipelines'].append('game_management_scroll.json')
    resources.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    print('Prepared',len(metadata['assets']),'safe map landmarks')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path);args=parser.parse_args();generate(args.source)
