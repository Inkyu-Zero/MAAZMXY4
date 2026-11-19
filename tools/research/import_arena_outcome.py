"""Extract public arena result controls; never copy account/opponent portraits."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def generate(source, root=ROOT):
    # Supplied box capture: one pixel border and 39 pixel title bar. Match the
    # game's existing 1147 x 720 native recognition coordinate system.
    with Image.open(source) as original:
        frame = original.crop((1, 39, 1176, 776)).resize((1147, 720), Image.Resampling.BOX).convert('RGB')
    entries = []
    for name, box, roi in [('arena-succeeded-title', (380, 20, 745, 105), [350, 0, 460, 130]),
                           ('arena-succeeded-return', (520, 570, 630, 620), [480, 545, 190, 90])]:
        relative = 'navigation/outcomes/'+name+'.png'
        output = root/'assets/resource/base/image'/relative
        frame.crop(box).save(output)
        entries.append(dict(template=relative, crop=list(box), roi=roi,
            sha256=hashlib.sha256(output.read_bytes()).hexdigest()))
    provenance = dict(source='User-provided arena victory screenshot',
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        client_crop=[1,39,1176,776], normalized_size=[1147,720],
        privacy='Only victory heading and return button retained; no account or opponent information.', assets=entries)
    path = root/'agent/data/navigation/arena-assets.json'
    path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2)+'\n', encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    generate(parser.parse_args().source)
