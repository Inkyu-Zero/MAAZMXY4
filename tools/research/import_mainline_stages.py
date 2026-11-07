"""Extract four-province/blood-sea entry ROIs from the offline SWF asset cache.

Only development-time Pillow/numpy are used. Runtime matching remains native
Maa TemplateMatch on a current screenshot; no placement coordinate is clicked.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from import_scroll_landmarks import bounds_reader, SCALE
from import_stage_variants import opaque_rectangle, write_json

ROOT = Path(__file__).resolve().parents[2]
PROVINCES = [('town1', '南瞻部洲', 122, 1, 16), ('town2', '东胜神洲', 94, 17, 29),
             ('town3', '北俱芦洲', 78, 30, 39), ('town4', '西牛贺洲', 57, 40, 46),
             ('town5', '幽冥血海', 448, 47, 62)]
VIEWPORT = (round(940*SCALE), 720)


def icon_patch(image, position=(0,0)):
    """Prefer an opaque interior; thin artwork uses Maa's native green mask."""
    import numpy as np
    from PIL import Image
    rgba = np.asarray(image).copy()
    alpha = rgba[:,:,3]
    # Fixed map HUD: upper toolbar, lower-left menu, right-side controls.
    # A large opaque icon interior may otherwise select pixels hidden by it.
    px,py=position
    for x,y,w,h in ((0,0,1147,120),(0,610,720,110),(1040,120,107,480),(1030,600,117,120)):
        x1,y1=max(0,x-px),max(0,y-py)
        x2,y2=min(image.width,x+w-px),min(image.height,y+h-py)
        if x2>x1 and y2>y1:alpha[y1:y2,x1:x2]=0
    image=Image.fromarray(rgba)
    try:
        box = opaque_rectangle(alpha)
        x,y,w,h = box
        return image.crop((x,y,x+w,y+h)).convert('RGB'), box, False
    except ValueError:
        ys,xs = np.where(alpha >= 235)
        if len(xs) < 256:
            raise ValueError('Icon has too few opaque pixels for a native masked match')
        # Keep the matched-box center on visible artwork so the shared centered
        # click cannot land in a transparent gap between petals/branches.
        cx,cy = (xs.min()+xs.max())//2,(ys.min()+ys.max())//2
        nearest = ((xs-cx)**2+(ys-cy)**2).argmin()
        cx,cy = int(xs[nearest]),int(ys[nearest])
        w,h = int(xs.max()-xs.min()+1),int(ys.max()-ys.min()+1)
        x,y = cx-w//2,cy-h//2
        patch = np.asarray(image.crop((x,y,x+w,y+h)))
        rgb = patch[:,:,:3].copy()
        rgb[patch[:,:,3]<235] = [0,255,0]
        return Image.fromarray(rgb), (x,y,w,h), True


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sprite_folder(source, town, ident):
    prefix = 'DefineSprite_' + str(ident)
    folders = [p for p in (source/town/'sprites').iterdir()
               if p.name == prefix or p.name.startswith(prefix+'_')]
    if len(folders) != 1:
        raise ValueError('Missing or ambiguous sprite export: '+town+'/'+prefix)
    return folders[0]


def generate(source, root=ROOT):
    from PIL import Image
    import numpy as np

    sources = {s['name']: s for s in json.loads((source/'manifest.json').read_text(encoding='utf-8'))}
    dungeon_file = source/'config/Dungeon.xml'
    records = {int(rec.findtext('id')): {field.tag: field.text for field in rec}
               for rec in ET.parse(dungeon_file).getroot()}
    metadata = dict(version=1, viewport=list(VIEWPORT), configuration_sha256=sha(dungeon_file),
        origin='Offline cached game SWFs and their FFDec exports; no game input or screenshots.',
        states='MovieClipButton frame 1 normal, frame 2 first-enterable, frame 3 hover.',
        provinces=[], stages=[])
    for town, label, container_id, first, last in PROVINCES:
        origin = sources[town]
        if sha(source/origin['file']) != origin['sha256']:
            raise ValueError('Offline map source changed: '+town)
        xml_file = source/(town+'.xml')
        definitions, bounds, transform = bounds_reader(ET.parse(xml_file).getroot())
        container = definitions[str(container_id)]
        placements = {int(p.get('name')[1:]): p for p in container.find('subTags')
                      if re.fullmatch(r's[0-9]+',p.get('name','')) and p.get('name') != 's0'}
        if set(placements) != set(range(first,last+1)):
            raise ValueError('Mainline map inventory is incomplete: '+town)
        metadata['provinces'].append(dict(id=town, label=label, first=first, last=last,
            source=origin, xml_sha256=sha(xml_file), container=container_id))
        for ident, placement in sorted(placements.items()):
            record = records[ident]
            sprite = placement.get('characterId')
            matrix = placement.find('matrix')
            if any(float(matrix.get(key,'0')) for key in ('rotateSkew0','rotateSkew1')):
                raise ValueError('Unexpected rotated mainline icon: '+str(ident))
            sx, sy = (float(matrix.get(key,'1'))*SCALE for key in ('scaleX','scaleY'))
            world_box = transform(bounds(sprite), matrix)
            offset = 560 if town == 'town5' and (world_box[0]+world_box[2])/2 > 750 else 0
            folder = sprite_folder(source, town, sprite)
            variants, seen, boxes = [], set(), []
            for state in (1,2,3):
                source_frame = folder/f'{state}.png'
                with Image.open(source_frame) as original:
                    image = original.convert('RGBA').resize(
                        (round(original.width*sx),round(original.height*sy)),Image.Resampling.LANCZOS)
                try:
                    origin=(round((world_box[0]-offset)*SCALE),round(world_box[1]*SCALE))
                    crop,(x,y,w,h),masked = icon_patch(image,origin)
                except ValueError as error:
                    raise ValueError(f'Unusable icon: stage {ident}, state {state}, sprite {sprite}') from error
                digest = hashlib.sha256(crop.tobytes()).hexdigest()
                px,py = round((world_box[0]-offset)*SCALE)+x,round(world_box[1]*SCALE)+y
                if px < 0 or py < 0 or px+w > VIEWPORT[0] or py+h > VIEWPORT[1]:
                    raise ValueError('Icon cannot fit its chosen endpoint: '+str(ident))
                boxes.append([px,py,w,h])
                if digest in seen:
                    continue
                seen.add(digest)
                relative = f'navigation/mainline/s{ident:03d}-f{state}.png'
                target = root/'assets/resource/base/image'/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                crop.save(target)
                variants.append(dict(frame=state, template=relative, crop=[x,y,w,h], green_mask=masked,
                    box=[px,py,w,h], sha256=sha(target), source_frame_sha256=sha(source_frame)))
            x1 = max(0,min(b[0] for b in boxes)-28)
            y1 = max(0,min(b[1] for b in boxes)-28)
            x2 = min(VIEWPORT[0],max(b[0]+b[2] for b in boxes)+28)
            y2 = min(VIEWPORT[1],max(b[1]+b[3] for b in boxes)+28)
            roi = [x1,y1,x2-x1,y2-y1]
            candidates = [dict(type='TemplateMatch',param=dict(
                template=[v['template'] for v in variants if v['green_mask']==masked],
                roi=roi,method=5,green_mask=masked)) for masked in (False,True)
                if any(v['green_mask']==masked for v in variants)]
            recognition = candidates[0] if len(candidates)==1 else dict(type='Or',param=dict(
                any_of=[dict(recognition=rec) for rec in candidates]))
            metadata['stages'].append(dict(id=ident, name=record['name'], town=town, province=label,
                map_url=record['mapURL'], source_sprite=int(sprite), source_bounds=list(world_box),
                scene_layout=dict(width=int(record['width']),height=int(record['height']),
                    screen_width=int(record['screenWidth']),birth_point=[float(v) for v in record['birthPoint'].split(',')],
                    class_type=record.get('classType') or '',cartoons=record.get('manhua') or ''),
                endpoint=('right' if offset else 'left') if town=='town5' else None,
                offset=offset, roi=roi, variants=variants,
                recognition=recognition))
    write_json(root/'agent/data/navigation/mainline-stages.json',metadata)
    print('Extracted',len(metadata['stages']),'mainline stage entries; largest ROI fraction:',
          max(s['roi'][2]*s['roi'][3] for s in metadata['stages'])/(VIEWPORT[0]*VIEWPORT[1]))
    return metadata


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args()
    generate(args.source,args.root)
