"""Import the game's shared WarnBar skin as a read-only native modal guard.

MoonTheme selects these symbols; WarnBar centers a nine-slice background and
places its buttons independently. No text or account-specific content is used.
"""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
SCALE=720/590


def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')


def nine_slice(image,width,height):
    w,h=image.size;xs=[0,w//3,2*(w//3),w];ys=[0,h//3,2*(h//3),h]
    dx=[0,xs[1],width-(w-xs[2]),width];dy=[0,ys[1],height-(h-ys[2]),height]
    result=Image.new('RGBA',(width,height))
    for y in range(3):
        for x in range(3):
            tile=image.crop((xs[x],ys[y],xs[x+1],ys[y+1]))
            tile=tile.resize((dx[x+1]-dx[x],dy[y+1]-dy[y]),Image.Resampling.BICUBIC)
            result.paste(tile,(dx[x],dy[y]))
    return result


def generate(source,root=ROOT):
    origin=json.loads((source/'manifest.json').read_text(encoding='utf8'))[0]
    if hashlib.sha256((source/origin['file']).read_bytes()).hexdigest()!=origin['sha256']:
        raise ValueError('Material SWF checksum changed')
    out=root/'assets/resource/base/image/navigation/dialogs'
    rendered=source/'rendered'
    bg_path=rendered/'DefineSprite_36_warn_background_2/1.png'
    bg=Image.open(bg_path).convert('RGBA')
    assets=[]
    def template(file,name,box=None):
        im=Image.open(file).convert('RGBA')
        if box:im=im.crop(box)
        im=im.resize((round(im.width*SCALE),round(im.height*SCALE)),Image.Resampling.LANCZOS)
        pixels=np.array(im)
        if np.any(pixels[:,:,3]<245):raise ValueError('Dialog templates must use opaque interiors')
        path=out/name;path.parent.mkdir(parents=True,exist_ok=True)
        Image.fromarray(pixels[:,:,:3]).save(path)
        relative=path.relative_to(root/'assets/resource/base/image').as_posix()
        assets.append(dict(path=relative,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            source_frame=file.relative_to(source).as_posix(),source_frame_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),crop=box))
        return relative
    left=template(bg_path,'confirm-left.png',(12,13,43,48))
    right=template(bg_path,'confirm-right.png',(bg.width-43,13,bg.width-12,48))
    # WarnBar places buttons 40 raw pixels above the bottom. Keep this crop
    # below that band, including on the minimum-height confirmation panel.
    bottom=template(bg_path,'confirm-bottom.png',(12,bg.height-30,43,bg.height-12))
    def match(images,**kw):return dict(type='TemplateMatch',param=dict(template=images,**kw))
    node=dict(recognition=dict(type='And',param=dict(all_of=[
        # WarnBar centers the panel; its left corner stays in the left half.
        # Include wide dialogs without searching unrelated right-hand controls.
        dict(sub_name='panel_corner',recognition=match(left,roi=[0,70,575,530])),
        dict(recognition=match(right,roi='panel_corner',roi_offset=[200,-8,650,16])),
        dict(recognition=match(bottom,roi='panel_corner',roi_offset=[-8,100,16,400])),
    ],box_index=0)),action=dict(type='DoNothing'),next=[],post_delay=0)
    write(root/'assets/resource/base/pipeline/navigation_common_ui.json',{'Nav_CommonConfirm':node})
    write(root/'agent/data/navigation/common-ui.json',dict(version=1,source=origin,assets=assets,
        use='Recognize and block unknown confirmation dialogs; never click a generic confirmation button.'))
    resource_path=root/'agent/data/navigation/resources.json'
    resource=json.loads(resource_path.read_text(encoding='utf8'))
    if 'navigation_common_ui.json' not in resource['pipelines']:resource['pipelines'].append('navigation_common_ui.json')
    write(resource_path,resource)
    fixture_dir=root/'tests/fixtures/navigation/common-dialog';fixture_dir.mkdir(parents=True,exist_ok=True)
    cases=[]
    for width,height in [(290,180),(440,260)]:
        for frame in (1,2,3):
            screen=Image.new('RGB',(940,590),(50,60,70))
            panel=nine_slice(bg,width,height);ox,oy=(940-width)//2,(590-height)//2
            screen.paste(panel,(ox,oy),panel)
            for i,(ident,label) in enumerate([(28,'sure'),(33,'cancel')]):
                im=Image.open(rendered/f'DefineSprite_{ident}_warn_button_{label}/{frame}.png').convert('RGBA')
                x=round(ox+(width/2-im.width)/2+width/2*i);y=oy+height-im.height-40
                screen.paste(im,(x,y),im)
            name=f'confirm-{width}-{frame}.png'
            screen.resize((1147,720),Image.Resampling.BICUBIC).save(fixture_dir/name)
            cases.append(dict(file=name,width=width,height=height,button_frame=frame,
                sha256=hashlib.sha256((fixture_dir/name).read_bytes()).hexdigest()))
    write(fixture_dir/'cases.json',dict(source=origin,cases=cases,method='Offline skin composition following WarnBar.render and Scale9Image; no live game.'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path)
    args=parser.parse_args();generate(args.source)
