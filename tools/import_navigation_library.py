"""Import reviewed screenshot assets/provenance, never user recordings.

This offline tool does not generate runtime navigation or outcome pipelines.
Author behavior in navigation_library.json and compile with build_navigation.py.
"""
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')


def inside(root, relative):
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:
        raise ValueError('素材路径必须是相对路径。')
    logical = PurePosixPath(relative)
    if logical.is_absolute() or any(part in ('.','..') for part in logical.parts):
        raise ValueError('素材路径不能越界。')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('素材路径越界或不存在：' + str(relative))
    return path


def import_library(source, destination):
    source = Path(source).resolve() / 'assets/assistant'
    captures = source / 'captures'
    manifest_path, graph_path = captures / 'manifest.json', source / 'navigation.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    original = json.loads(graph_path.read_text(encoding='utf-8-sig'))
    width, height = original['width'], original['height']
    size = (width * 720 // min(width, height), height * 720 // min(width, height))
    scale = size[0] / width, size[1] / height
    frames = {item['id']: item for item in manifest['assets'] if item['role'] == 'client_frame'}
    pixels = {}
    privacy = json.loads((Path(__file__).with_name('navigation_privacy_masks.json')).read_text(encoding='utf8'))
    if privacy['version'] != 1 or privacy['base'] != list(size):
        raise ValueError('请重新审阅当前图片尺寸的隐私遮挡清单。')

    def frame(ident):
        if ident not in pixels:
            raw = inside(captures, frames[ident]['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != frames[ident]['sha256']:
                raise ValueError('源客户区截图校验失败：'+ident)
            image = Image.open(BytesIO(raw)).convert('RGB')
            if image.size != (width, height):
                raise ValueError('源客户区尺寸不一致：' + ident)
            scaled = image.resize(size, Image.Resampling.LANCZOS)
            mask = privacy['frames'].get(ident)
            if mask:
                if mask['source_sha256'] != frames[ident]['sha256']:
                    raise ValueError('截图已经改变，请重新审阅隐私遮挡：'+ident)
                draw = ImageDraw.Draw(scaled)
                for x,y,w,h in mask['boxes']:
                    draw.rectangle((x,y,x+w-1,y+h-1),fill=(32,32,32))
            pixels[ident] = scaled
        return pixels[ident]

    def rectangle(box):
        x, y, w, h = box
        left, top = int(x * scale[0]), int(y * scale[1])
        right, bottom = max(left + 1, int((x + w) * scale[0])), max(top + 1, int((y + h) * scale[1]))
        return [left, top, right - left, bottom - top]

    converted = {}
    for item in manifest['crops']:
        original_crop = inside(captures, item['path']).read_bytes()
        if hashlib.sha256(original_crop).hexdigest() != item['sha256']:
            raise ValueError('源裁剪素材校验失败：'+item['id'])
        x, y, w, h = rectangle(item['bbox'])
        crop = frame(item['parent_asset']).crop((x, y, x + w, y + h))
        relative = Path('navigation') / item['path']
        target = destination / 'assets/resource/base/image' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        crop.save(target)
        converted[item['id']] = dict(id=item['id'], name=item['name'], path=relative.as_posix(),
            bbox=[x, y, w, h], source_bbox=item['bbox'], source_sha256=item['sha256'],
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(), parent=item['parent_asset'],
            role=item['role'], action_policy=item.get('action_policy', 'recognize_only'))
        converted[item['id']]['redacted'] = any(x < rx+rw and rx < x+w and y < ry+rh and ry < y+h
            for rx,ry,rw,rh in privacy['frames'].get(item['parent_asset'],{}).get('boxes',[]))

    pause_box = [638, 343, 132, 34]
    pause_relative = 'navigation/panels/daily-pause-caption.png'
    pause_path = destination / 'assets/resource/base/image' / pause_relative
    pause_path.parent.mkdir(parents=True, exist_ok=True)
    x, y, w, h = pause_box
    frame('capture-daily-expanded-client').crop((x, y, x+w, y+h)).save(pause_path)
    write_json(destination / 'agent/data/navigation/pause-sources.json', [dict(
        source='capture-daily-expanded-client', source_sha256=frames['capture-daily-expanded-client']['sha256'],
        base=list(size), bbox=pause_box, path=pause_relative,
        sha256=hashlib.sha256(pause_path.read_bytes()).hexdigest())])
    probes, missing = set(), {}
    supported = {'navigation_paused', 'hub_scene', 'daily_menu_scene', 'home_label'}
    for key, definition in original['probes'].items():
        if 'reference' in definition:
            if converted[definition['reference']]['redacted']:
                raise ValueError('导航探针不能使用个人信息区域：' + key)
            probes.add(key)
        elif definition['detector'] in supported:
            probes.add(key)
        else:
            missing[key] = definition['detector']

    unavailable = {item['id']: [p for p in item['recognition'] if p not in probes]
                   for item in original['nodes'] if any(p not in probes for p in item['recognition'])}
    resources_path = destination / 'agent/data/navigation/resources.json'
    resources = json.loads(resources_path.read_text(encoding='utf8')) if resources_path.exists() else dict(version=1, pipelines=[])
    resources['pipelines'] = list(dict.fromkeys(['navigation.json', 'battle_outcomes.json', *resources['pipelines']]))
    write_json(resources_path, resources)
    write_json(destination / 'agent/data/navigation/source-catalog.json', original)
    write_json(destination / 'agent/data/navigation/library.json', dict(version=1, base=list(size),
        source_project='ZaoMengScript', source_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        source_graph_sha256=hashlib.sha256(graph_path.read_bytes()).hexdigest(),
        source_node_count=len(original['nodes']), source_edge_count=len(original['edges']),
        source_probe_count=len(original['probes']), assets=list(converted.values()),
        privacy_masks={ident:entry['boxes'] for ident,entry in privacy['frames'].items()},
        unavailable=unavailable, missing_detectors=missing,
        notice='游戏画面版权归4399及相关权利人；素材来自用户提供的真实截图，仅作界面识别参考，候选路径仍需实机验证。'))
    # Navigation behavior is authored in tools/navigation_library.json.
    # Rebuild it with tools/build_navigation.py after reviewing imported assets.
    # Native fixtures have no Windows title bar, and use the exact runtime size.
    for ident, item in frames.items():
        target = destination / 'tests/fixtures/navigation' / Path(item['path']).name
        target.parent.mkdir(parents=True, exist_ok=True)
        frame(ident).save(target)
    print(f'已迁移 {len(converted)} 份素材，{len(original['nodes'])} 个可定位界面、{len(original['edges'])} 条候选路径。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, default=ROOT)
    args = parser.parse_args()
    import_library(args.source, args.destination.resolve())
