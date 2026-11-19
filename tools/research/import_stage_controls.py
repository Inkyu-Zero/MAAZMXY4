"""Crop only the public Wushuang glyphs, excluding energy fill and player data."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[2]
CROP=(14,641,92,681)

def generate(idle,ready,root=ROOT):
    assets=[]
    for state,source in [('idle',idle),('ready',ready)]:
        path=root/f'assets/resource/base/image/navigation/stage/wushuang-{state}.png'
        path.parent.mkdir(parents=True,exist_ok=True)
        with Image.open(source) as image:
            if image.height!=720 or image.width not in (1147,1148):
                raise ValueError('Expected a short-side-720 reference frame')
            image.convert('RGB').crop(CROP).save(path)
        assets.append(dict(path=path.relative_to(root/'assets/resource/base/image').as_posix(),
            state=state,crop=list(CROP),source_frame=source.name,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            source='Archived 2026-10/11 gameplay frame; public fixed Wushuang button glyphs only'))
    output=root/'agent/data/navigation/stage-ui.json'
    output.write_text(json.dumps(dict(version=1,viewport=[1148,720],assets=assets,
        scope='Shared stage presence; no map/background/Boss identity or energy amount'),ensure_ascii=False,indent=2)+'\n',encoding='utf8')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--idle',type=Path,default=ROOT/'tests/fixtures/bounty-candle/candle-battle-ready.png')
    parser.add_argument('--ready',type=Path,required=True)
    args=parser.parse_args();generate(args.idle,args.ready)
