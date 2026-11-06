"""Extract native settings anchors from the game's SWF, without executing it.

Only static labels and the close button are matched. Checkboxes, sliders, button
backgrounds and character art are excluded, so their state cannot move a click.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from PIL import Image
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SCALE = 720 / 590


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')


def generate(source, root=ROOT):
    origin = next(s for s in json.loads((source / 'manifest.json').read_text(encoding='utf8'))
                  if s['name'] == 'systemset')
    if hashlib.sha256((source / origin['file']).read_bytes()).hexdigest() != origin['sha256']:
        raise ValueError('Settings SWF checksum changed')
    images = source / 'systemset'
    assets = []

    def template(file, name, box=None, mask=False):
        with Image.open(file) as im:
            im = im.convert('RGBA')
            if box: im = im.crop(box)
            elif mask: im = im.crop(im.getbbox())
            im = im.resize((round(im.width * SCALE), round(im.height * SCALE)), Image.Resampling.LANCZOS)
            pixels = np.array(im)
        if mask: pixels[pixels[:, :, 3] < 245, :3] = (0, 255, 0)
        elif np.any(pixels[:, :, 3] < 245): raise ValueError('Expected opaque label: ' + name)
        relative = 'navigation/settings/' + name + '.png'
        target = root / 'assets/resource/base/image' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(pixels[:, :, :3]).save(target)
        assets.append(dict(path=relative, sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            source_frame=file.relative_to(source).as_posix(),
            source_frame_sha256=hashlib.sha256(file.read_bytes()).hexdigest(), crop=box, green_mask=mask))
        return relative

    def match(paths, **params):
        return dict(type='TemplateMatch', param=dict(template=paths, **params))

    labels = [template(images / f'sprites/DefineSprite_63_btn_txt/{frame}.png', name, mask=True)
              for frame, name in [(1, 'continue'), (2, 'return')]]
    # Small physical clients resampled to 720 score ~0.817 on this text anchor.
    # The independent return label keeps the shared 0.83 threshold and ROI.
    menu = [dict(sub_name='continue', recognition=match(labels[0], threshold=.80, green_mask=True, roi=[400, 130, 370, 220])),
            dict(recognition=match(labels[1], green_mask=True, roi='continue', roi_offset=[-15, 35, 30, 95]))]
    readonly = dict(action=dict(type='DoNothing'), next=[], post_delay=0)
    nodes = {'Nav_SettingsMenu': dict(recognition=dict(type='And', param=dict(all_of=menu, box_index=0)), **readonly)}
    for name, index in [('Return', 1), ('Continue', 0)]:
        nodes['Nav_Settings' + name] = dict(recognition=dict(type='And', param=dict(
            all_of=deepcopy(menu) + ['Result_NoRevivePrompt'], box_index=index)),
            action=dict(type='Click', param=dict(target=True)), next=[], timeout=5000, post_delay=350)

    close = [template(images / f'buttons/DefineButton2_10/{state}.png', 'close-' + state, mask=True)
             for state in ('1_up', '2_over', '3_down')]
    alternatives, cases = [], []
    for name, sprite, caption, body in [
        ('sound', '34_soundSet', (393, 130, 549, 170), (304, 215, 398, 240)),
        ('detail', '47_detailSet', (385, 29, 541, 69), (368, 99, 539, 122)),
        ('damage', '73_battleSet', (350, 66, 580, 106), (374, 138, 572, 159)),
    ]:
        file = images / f'sprites/DefineSprite_{sprite}/1.png'
        title = template(file, name + '-title', caption)
        label = template(file, name + '-label', body)
        dx, dy = round((body[0] - caption[0]) * SCALE), round((body[1] - caption[1]) * SCALE)
        alternatives.append(dict(recognition=dict(type='And', param=dict(all_of=[
            dict(sub_name=name + '_title', recognition=match(title, roi=[280, 15, 570, 250])),
            dict(recognition=match(label, roi=name + '_title', roi_offset=[dx-8, dy-8, 100, 25])),
            dict(recognition=match(close, green_mask=True, roi=[750, 0, 120, 235])),
        ], box_index=2))))
        # Independent scaling of the full exported display list, on a neutral
        # background. Dynamic settings controls are not templates.
        with Image.open(file) as im:
            # A sprite export may include off-stage bounds (soundSet is
            # 948x598). Do not squeeze that overscan into the 940x590 game.
            frame = Image.new('RGB', (940, 590), (50, 60, 70))
            frame.paste(im, (0, 0), im)
        target = root / 'tests/fixtures/navigation/settings' / (name + '.png')
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.resize((1147, 720), Image.Resampling.BICUBIC).save(target)
        cases.append(dict(file=target.name, sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            source_frame=file.relative_to(source).as_posix(),
            source_frame_sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
    nodes['Nav_SettingsSubpanel'] = dict(recognition=dict(type='Or', param=dict(any_of=alternatives)), **readonly)
    nodes['Nav_CloseSettingsSubpanel'] = dict(recognition=dict(type='And', param=dict(
        all_of=['Nav_SettingsSubpanel', 'Result_NoRevivePrompt'], box_index=0)),
        action=dict(type='Click', param=dict(target=True)), next=[], timeout=5000, post_delay=350)
    write(root / 'assets/resource/base/pipeline/navigation_settings.json', nodes)
    write(root / 'agent/data/navigation/settings-ui.json', dict(version=1, source=origin, assets=assets))
    write(root / 'tests/fixtures/navigation/settings/cases.json', dict(source=origin, cases=cases,
        method='Complete FFDec sprite exports, independent BICUBIC scaling; no live game.'))
    path = root / 'agent/data/navigation/resources.json'
    data = json.loads(path.read_text(encoding='utf8'))
    if 'navigation_settings.json' not in data['pipelines']: data['pipelines'].append('navigation_settings.json')
    write(path, data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    generate(args.source)
