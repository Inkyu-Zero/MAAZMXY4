"""Extract small scene checks from offline map layers, without running the game.

The compositor follows TMXLayer, BackGroundContainer and FrontGroundContainer
in the archived offline client. Animated areas are excluded from scene crops.
Only generated PNGs/metadata are runtime inputs, never the source SWFs.
"""
import argparse
import csv
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from import_scroll_landmarks import bounds_reader, SCALE
from import_stage_variants import write_json

ROOT = Path(__file__).resolve().parents[2]


def properties(element):
    return {p.get('name'): p.get('value') for p in element.findall('properties/property')}


@lru_cache(maxsize=2)
def sprite_geometry(folder):
    from PIL import Image
    xml=ET.parse(folder/'map.xml').getroot()
    for bitmap in xml.iter():
        external=bitmap.get('_externalFile')
        if bitmap.get('type','').startswith('DefineBits') and external:
            with Image.open(folder/external) as image:
                ET.SubElement(bitmap,'bounds',Xmin='0',Ymin='0',Xmax=str(image.width*20),Ymax=str(image.height*20))
    # Exported sprites use the complete animation's bounding rectangle.
    for tags in xml.iter('subTags'):
        for tag in list(tags):
            if tag.get('type')=='ShowFrameTag':tags.remove(tag)
    for sprite in xml.iter():
        if sprite.get('type')=='DefineSpriteTag' and not any(tag.get('characterId') for tag in sprite.iter()):
            ET.SubElement(sprite,'bounds',Xmin='0',Ymin='0',Xmax='0',Ymax='0')
    return bounds_reader(xml)


def landing_y(layout, folder, classes, birth):
    """First authored floor below spawn, from map ground objects or SWF blocks."""
    px, py = birth
    surfaces = []
    for obj in layout.findall('.//object'):
        if obj.get('type') not in ('ground','floor'): continue
        ox,oy = float(obj.get('x','0')),float(obj.get('y','0'))
        polygon = obj.find('polygon')
        if polygon is not None:
            points=[(ox+float(x),oy+float(y)) for x,y in (pair.split(',') for pair in polygon.get('points').split())]
            for (x1,y1),(x2,y2) in zip(points,points[1:]+points[:1]):
                if x1!=x2 and min(x1,x2)<=px<=max(x1,x2):
                    surfaces.append(y1+(px-x1)*(y2-y1)/(x2-x1))
        elif ox<=px<=ox+float(obj.get('width','0')): surfaces.append(oy)
    if (folder/'map.xml').is_file() and 'blocks' in classes:
        definitions,bounds,transform=sprite_geometry(folder)
        reverse={ident:name for name,ident in classes.items()}
        for child in definitions[classes['blocks']].find('subTags'):
            ident=child.get('characterId')
            if not reverse.get(ident,'').startswith(('ground','floor')): continue
            box=transform(bounds(ident),child.find('matrix'))
            if box[0]<=px<=box[2]:
                if reverse[ident] not in ('ground','floor'):
                    import numpy as np
                    from PIL import Image
                    sprite=next(folder.glob('DefineSprite_'+ident+'_*'),None)
                    if sprite is None:raise ValueError('Missing collision silhouette: '+folder.name+'/'+reverse[ident])
                    with Image.open(sprite/'1.png') as terrain:
                        column=min(terrain.width-1,max(0,round((px-box[0])/(box[2]-box[0])*terrain.width)))
                        occupied=np.flatnonzero(np.asarray(terrain.convert('RGBA'))[:,column,3]>127)
                        surfaces.extend(box[1]+row/terrain.height*(box[3]-box[1]) for row in occupied)
                else:surfaces.append(box[1])
    below=[height for height in surfaces if height>=py-35]
    return min(below) if below else None


def animation_bounds(library, reference):
    """Conservative exclusion for the cached flipbook-only DragonBones asset."""
    name,armature=reference.split(',')
    skeleton=json.loads((library/'maps'/f'{name}_skeleton.json').read_bytes())
    texture=json.loads((library/'maps'/f'{name}_texture.json').read_bytes())
    atlas={item['name']:item for item in texture['SubTexture']}
    rig=next(item for item in skeleton['armature'] if item['name']==armature)
    bones={item['name']:item for item in rig['bone']}
    # This is extraction-time masking, not an animation player. Refuse
    # unsupported transforms instead of guessing an unbounded moving area.
    if any(frame.get('transform') for animation in rig['animation'] for timeline in animation['timeline'] for frame in timeline['frame']):
        raise ValueError('Moving animation needs explicit exclusion bounds: '+reference)
    boxes=[]
    for slot in rig['skin'][0]['slot']:
        bone=bones[slot['parent']]
        if bone.get('parent'):raise ValueError('Nested animation needs explicit exclusion bounds: '+reference)
        transform=bone.get('transform',{})
        if any(transform.get(key,0) for key in ('skX','skY')):raise ValueError('Rotated animation: '+reference)
        for display in slot['display']:
            item=atlas[display['name']];point=display.get('transform',{})
            x=transform.get('x',0)+point.get('x',0)*transform.get('scX',1)
            y=transform.get('y',0)+point.get('y',0)*transform.get('scY',1)
            boxes.append((x-5,y-5,x+item['width']*abs(transform.get('scX',1))+5,y+item['height']*abs(transform.get('scY',1))+5))
    return min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)


def render(library, name, *, camera=None, phase=0):
    import numpy as np
    from PIL import Image, ImageDraw
    folder = library/'exports'/name
    layout = ET.parse(library/'maps'/f'{name}.dat').getroot()
    props = properties(layout)
    # BattleMap takes its viewport limits/spawn from Dungeon.xml, not the
    # editor's tile grid (blood-sea grids still describe old 512px levels).
    records = json.loads((ROOT/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf-8'))['stages']
    config = next(stage['scene_layout'] for stage in records if stage['map_url']==name)
    max_y = max(0, config['height']-590)
    max_x = max(0, config['screen_width']-940)
    classes = {row[1]: row[0] for row in csv.reader(
        next(folder.rglob('symbols.csv')).read_text(encoding='utf-8').splitlines(), delimiter=';')}
    ground_y = landing_y(layout,folder,classes,config['birth_point'])
    if camera is None:
        camera = (min(max_x, max(0, config['birth_point'][0]-470)),
                  min(max_y,max(0,(ground_y if ground_y is not None else config['birth_point'][1])-295)))
    groups = {key: [] for key in ('bg3', 'bg2', 'bg1', 'other', 'front1')}
    speeds = {'bg3': [1.1, 6], 'bg2': [1.2, 6]}
    fixed_front = False
    empty,conditional,excluded = [],[],[]
    for layer in layout.findall('imagelayer'):
        label = layer.get('name', '')
        if layer.get('visible') == '0':
            continue
        p = properties(layer)
        key = next((key for key in ('front1', 'bg2', 'bg1', 'bg3') if key in label), 'other')
        if key in speeds:
            for axis, field in enumerate(('speedX', 'speedY')):
                if field in p: speeds[key][axis] = float(p[field])
        if key == 'front1' and 'fixedX' in p: fixed_front = p['fixedX'] not in ('false', '0', '')
        if 'aniPath' in p:
            box=animation_bounds(library,p['aniPath'])
            excluded.append(dict(layer=label,reference=p['aniPath'],box=[box[0]+float(p.get('x',0))-camera[0],
                box[1]+float(p.get('y',0))-camera[1],box[2]+float(p.get('x',0))-camera[0],box[3]+float(p.get('y',0))-camera[1]]))
            continue
        images = list((folder/'images').glob('*_'+label+'.png'))
        animated = False
        if images:
            image = Image.open(images[0]).convert('RGBA')
            origin = (0, 0)
        elif label in classes:
            sprite = next(folder.glob('DefineSprite_'+classes[label]+'_*'), None)
            if sprite is None: raise ValueError('Unexported layer: '+name+'/'+label)
            frames = sorted(sprite.glob('*.png'), key=lambda path:int(path.stem))
            image = Image.open(frames[min(phase,len(frames)-1)]).convert('RGBA')
            definitions, bounds, _ = sprite_geometry(folder)
            if not any(tag.get('characterId') for tag in definitions[classes[label]].iter()):
                # Authored empty timeline placeholders (e.g. Lumbini bg15).
                continue
            origin = bounds(classes[label])[:2]
            animated = int(definitions[classes[label]].get('frameCount', '1')) > 1
        else:
            if any(part.split('-')[1:2]==[label] for part in config['cartoons'].split(',')):
                # GoInBattleCmd loads these only while the first-clear comic
                # is active. Farm templates describe the normal scene; a
                # covering comic must miss and stop, never become a click.
                conditional.append(label)
            # TMXLayer.drawImage uses BitmapUtils.EMPTY for an absent class.
            empty.append(label)
            continue
        item = (image, p, origin, animated, label)
        if key in ('bg2', 'front1'): groups[key].insert(0, item)
        else: groups[key].append(item)
    canvas = Image.new('RGBA', (940, 590))
    unstable = Image.new('L', canvas.size)
    for key, layers in groups.items():
        dx, dy = -camera[0], -camera[1]
        if key in speeds:
            sx, sy = speeds[key]
            dx += camera[0]/sx if sx else 0
            dy += camera[1]/sy if sy else 0
        if key == 'front1' and fixed_front: dx = 0
        for image, p, origin, animated, label in layers:
            for repeat in range(int(p.get('repeat', 1))):
                x = round(float(p.get('x', 0))+origin[0]+dx+repeat*image.width)
                y = round(float(p.get('y', 0))+origin[1]+dy)
                canvas.alpha_composite(image, (x, y))
                # Opaque foreground can cover an earlier animation. Partial
                # alpha cannot: leave the uncertainty underneath marked.
                alpha = np.asarray(image)[:, :, 3]
                opaque = Image.fromarray((alpha == 255).astype('uint8')*255)
                unstable.paste(0, (x, y, x+image.width, y+image.height), opaque)
                # A few exported frames cannot prove that a longer timeline
                # stays still between samples (as with Buzhou's figure/swords).
                # Sprite exports cover the complete animation bounds.
                if animated or 'moveXSpeed' in p or 'aniPath' in p:
                    ImageDraw.Draw(unstable).rectangle((x,y,x+image.width,y+image.height), fill=255)
    for item in excluded:ImageDraw.Draw(unstable).rectangle(item['box'],fill=255)
    return canvas, unstable, dict(camera=list(camera), landing_y=ground_y, missing_layers=[],empty_layers=empty,
        conditional_comic_layers=conditional,excluded_animations=excluded,
        layout_sha256=hashlib.sha256((library/'maps'/f'{name}.dat').read_bytes()).hexdigest(),
        swf_sha256=hashlib.sha256((library/'maps'/f'{name}.swf').read_bytes()).hexdigest())


def generate(library, evidence, root=ROOT, *, stage_ids=None):
    import cv2
    import numpy as np
    from PIL import Image
    stages = json.loads((root/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf-8'))
    selected = set(stage_ids) if stage_ids is not None else {stage['id'] for stage in stages['stages']}
    unknown = selected - {stage['id'] for stage in stages['stages']}
    if unknown: raise ValueError('Unknown stage ids: '+repr(sorted(unknown)))
    previous = {}
    metadata = dict(version=1, viewport=stages['viewport'], stages=[])
    if stage_ids is not None:
        metadata = json.loads((root/'agent/data/navigation/mainline-scenes.json').read_text(encoding='utf8'))
        previous = {stage['id']:stage for stage in metadata['stages']}
        if set(previous) != {stage['id'] for stage in stages['stages']}:
            raise ValueError('Partial regeneration requires the complete existing scene inventory')
        metadata['stages'] = []
    evidence.mkdir(parents=True, exist_ok=True)
    failures = []
    for stage in stages['stages']:
        if stage['id'] not in selected:
            metadata['stages'].append(previous[stage['id']])
            continue
        name = stage['map_url']
        image, unstable, source = render(library, name)
        phases = [np.asarray(render(library,name,phase=phase)[0]) for phase in (1,2)]
        image.save(evidence/f's{stage["id"]:03d}.png')
        unstable.save(evidence/f's{stage["id"]:03d}-unstable.png')
        a = np.asarray(image)
        u = np.asarray(unstable)
        candidates = []
        # Stay clear of the upper-left HUD, right settings, lower skills,
        # spawn position and ground enemies. Match two separated landmarks.
        for y in range(110, 471, 20):
            for x in range(280, 841, 20):
                w, h = 72, 52
                patch = a[y:y+h, x:x+w]
                if patch.shape != (h,w,4) or patch[:,:,3].min()<255 or u[y:y+h,x:x+w].any(): continue
                if any(cv2.matchTemplate(frame[y:y+h,x:x+w,:3],patch[:,:,:3],cv2.TM_CCOEFF_NORMED)[0,0]<0.92 for frame in phases): continue
                gray = cv2.cvtColor(patch[:,:,:3],cv2.COLOR_RGB2GRAY)
                score = float(cv2.Laplacian(gray,cv2.CV_32F).var())
                if gray.std() >= 8: candidates.append((score, (x,y,w,h)))
        selected = []
        for _, box in sorted(candidates, reverse=True):
            if all(abs(box[0]-prev[0])+abs(box[1]-prev[1]) >= 120 for prev in selected): selected.append(box)
            if len(selected) == 2: break
        if len(selected)<2:
            failures.append(dict(id=stage['id'], map=name, candidates=len(candidates), source=source))
            continue
        checks = []
        scaled = image.resize(tuple(stages['viewport']), Image.Resampling.LANCZOS).convert('RGB')
        for index,(x,y,w,h) in enumerate(selected):
            box = [round(x*SCALE), round(y*SCALE), round(w*SCALE), round(h*SCALE)]
            px,py,pw,ph = box
            crop = scaled.crop((px,py,px+pw,py+ph))
            relative=f'navigation/mainline/scene-s{stage["id"]:03d}-{index}.png'
            path=root/'assets/resource/base/image'/relative
            path.parent.mkdir(parents=True,exist_ok=True);crop.save(path)
            # Small camera settling and authored rounding fit here; this is
            # still a local ROI (not a whole-screen background search).
            roi=[max(0,px-70),max(0,py-80),pw+140,ph+160]
            checks.append(dict(template=relative,box=box,roi=roi,threshold=0.83,
                               sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        metadata['stages'].append(dict(id=stage['id'],name=stage['name'],map_url=name,source=source,checks=checks,
            equivalent_ids=[7,8] if stage['id'] in (7,8) else [stage['id']]))
    write_json(evidence/'extraction-report.json',dict(stages=len(metadata['stages']),regenerated=sorted(selected),failures=failures))
    if failures: raise ValueError('Scene crops need review: '+repr(failures))
    write_json(root/'agent/data/navigation/mainline-scenes.json',metadata)
    return metadata


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library',type=Path)
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--stage-id',action='append',type=int,help='Regenerate only selected stages; preserve the other existing resources')
    args=parser.parse_args()
    print('Scene inventory:',len(generate(args.library,args.evidence,stage_ids=args.stage_id)['stages']),'stages')
