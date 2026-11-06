"""Prepare native Maa templates from FFDec's exported map button frames.

Development dependencies: Pillow, numpy. The runtime uses only Maa's existing
And/Or/TemplateMatch, not Python image processing or the original game files.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
STAGES = [
    ('朱电（琉璃宫）', 'town2', 26), ('烛时（钟晷极地）', 'town3', 38),
    ('摩多（七叶树窟）', 'town4', 43), ('摩多（幕山）', 'town4', 45),
    ('无冥（孤魂岭）', 'town5', 47), ('伽岚（万世冥阁）', 'town5', 61),
    ('贪妄（金蟾宝宫）', 'town6', 65),
]


def opaque_rectangle(alpha):
    """Largest near-opaque interior using a histogram/monotonic stack.

    Alpha >= 235 tolerates Flash's anti-aliasing, with at most 8% background
    contribution. Avoid transparent margins; reject narrow/empty patches.
    """
    heights = [0] * (alpha.shape[1] + 1)
    best, box = 0, None
    for y, row in enumerate(alpha):
        for x, value in enumerate(row):
            heights[x] = heights[x] + 1 if value >= 235 else 0
        stack = []
        for x, height in enumerate(heights):
            start = x
            while stack and stack[-1][1] > height:
                left, h = stack.pop()
                width = x - left
                if min(width, h) >= 16 and width * h >= 900 and width * h > best:
                    best, box = width * h, (left, y + 1 - h, width, h)
                start = left
            if not stack or stack[-1][1] < height:
                stack.append((start, height))
    if box is None:
        raise ValueError('No sufficiently distinctive opaque icon interior')
    x, y, w, h = box
    cw, ch = min(w, 100), min(h, 90)
    return x + (w-cw)//2, y + (h-ch)//2, cw, ch


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')


def generate(source, root=ROOT):
    # These are development-only extraction dependencies; JSON formatting and
    # its cross-platform regression check do not require the image stack.
    import numpy as np
    from PIL import Image

    sources = {s['name']: s for s in json.loads((source / 'manifest.json').read_text(encoding='utf8'))}
    metadata = dict(version=1, source_version='v.20261019.00:01', viewport=[940, 590],
        processing='FFDec sprite frames; placement scale; short-edge 720; largest alpha>=235 rectangle; native TemplateMatch.',
        state_evidence='MovieClipButton: frame 1 normal, frame 2 first-enterable, frame 3 hover. No game execution.', stages=[])
    for task, town, stage in STAGES:
        origin = sources[town]
        if hashlib.sha256((source / origin['file']).read_bytes()).hexdigest() != origin['sha256']:
            raise ValueError('Source SWF hash mismatch: ' + town)
        xml = ET.parse(source / (town + '.xml')).getroot()
        placements = [e for e in xml.iter() if e.get('name') == 's' + str(stage)]
        if len(placements) != 1:
            raise ValueError('Ambiguous stage instance: ' + task)
        placement = placements[0]
        matrix = placement.find('matrix')
        sx = float(matrix.get('scaleX', '1')) * 720 / 590
        sy = float(matrix.get('scaleY', '1')) * 720 / 590
        sprite = placement.get('characterId')
        prefix = 'DefineSprite_' + sprite
        folders = [p for p in (source / town / 'sprites').iterdir()
                   if p.name == prefix or p.name.startswith(prefix + '_')]
        if len(folders) != 1:
            raise ValueError('Missing/ambiguous exported sprite: ' + prefix)
        frames = folders[0]
        variants, hashes, records = [], set(), []
        for frame in (1, 2, 3):
            file = frames / f'{frame}.png'
            with Image.open(file) as original:
                image = original.convert('RGBA').resize((round(original.width*sx), round(original.height*sy)), Image.Resampling.LANCZOS)
            try:
                box = opaque_rectangle(np.asarray(image)[:, :, 3])
            except ValueError as error:
                raise ValueError(f'{task} frame {frame}: {error}') from error
            x, y, w, h = box
            crop = image.crop((x, y, x+w, y+h)).convert('RGB')
            digest = hashlib.sha256(crop.tobytes()).hexdigest()
            if digest in hashes:
                continue
            hashes.add(digest)
            relative = f'navigation/stages/s{stage}-frame{frame}.png'
            target = root / 'assets/resource/base/image' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            crop.save(target)
            variants.append(relative)
            records.append(dict(frame=frame, path=relative, crop=list(box), scale=[sx,sy],
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                source_frame_sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
        path = root / 'assets/resource/base/pipeline' / (task + '.json')
        pipeline = json.loads(path.read_text(encoding='utf8'))
        node = pipeline[task + '-关卡入口']
        original_recognition = node['recognition']
        if original_recognition['type'] == 'And':
            original_recognition = original_recognition['param']['all_of'][1]['recognition']['param']['any_of'][0]['recognition']
        if original_recognition['type'] != 'TemplateMatch':
            raise ValueError('Unexpected legacy stage detector: ' + task)
        # Cheap legacy path first. The native Or evaluates extracted variants
        # only after the existing crop misses. Keep the click's matched box.
        node['recognition'] = dict(type='And', param=dict(all_of=[
            dict(recognition=dict(type='TemplateMatch', param=dict(template='任务.png', roi=[0,540,1147,180]))),
            dict(recognition=dict(type='Or', param=dict(any_of=[
                dict(recognition=deepcopy(original_recognition)),
                dict(recognition=dict(type='TemplateMatch', param=dict(template=variants,
                    threshold=.82, **({} if town == 'town5' else dict(roi=[0,115,1045,500])), method=5))),
            ]))),
            'Result_NoRevivePrompt',
        ], box_index=1))
        write_json(path, pipeline)
        profiles_path = root / 'tools/script_task_profiles.json'
        profiles = json.loads(profiles_path.read_text(encoding='utf8'))
        profiles[task]['target']['recognition'] = deepcopy(node['recognition'])
        write_json(profiles_path, profiles)
        metadata['stages'].append(dict(task=task, stage_id=stage, town=town, sprite_id=int(sprite),
            source_url=origin['url'], source_sha256=origin['sha256'], variants=records))
    write_json(root / 'agent/data/navigation/stage-variants.json', metadata)
    print('Prepared native variants for', len(STAGES), 'stages')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    generate(args.source, args.root)
