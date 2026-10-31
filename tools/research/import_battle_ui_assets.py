"""Import verified revival/return UI assets into the existing native Maa nodes.

FFDec renderings are external inputs. Dialog variants are rendered by hiding
the mutually exclusive mc_vip/mc_normal and mc_p1/mc_p2 placements, as the
game's ResurrectionMediator does. No game code is executed.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

from PIL import Image
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
SCALE=720/590


def render_dialogs(source,jar):
    """Keep Flash's fractional placements while selecting real button states."""
    origins=json.loads((source/'manifest.json').read_text(encoding='utf8'))
    origin=next(e for e in origins if e['name']=='resurrection')
    if hashlib.sha256((source/origin['file']).read_bytes()).hexdigest()!=origin['sha256']:
        raise ValueError('Resurrection SWF checksum mismatch')
    command=['java','-Djava.awt.headless=true','-Xmx1g','-jar',str(jar)]
    with (source/'revival-render.log').open('w',encoding='utf8') as log:
        subprocess.run(command+['-swf2xml',str(source/origin['file']),str(source/'resurrection.xml')],stdout=log,stderr=log,check=True,timeout=90)
        for mode in ('normal','vip'):
            for player in ('p1','p2'):
                for state in ('up','over','down'):
                    tree=ET.parse(source/'resurrection.xml')
                    for parent in tree.getroot().iter():
                        for child in list(parent):
                            if child.get('name') in [('mc_vip' if mode=='normal' else 'mc_normal'),('mc_p2' if player=='p1' else 'mc_p1')]:
                                parent.remove(child)
                        if parent.get('buttonId') in ('23','39'):
                            characters=parent.find('characters')
                            selected=[record for record in characters if record.get('buttonState'+state.capitalize())=='true']
                            # An all-zero BUTTONRECORD is the binary end marker;
                            # leaving a hidden up-only record first would truncate
                            # the entire button when FFDec writes the SWF.
                            characters[:]=sorted(selected,key=lambda r:int(r.get('placeDepth')))
                            for record in characters:record.set('buttonStateUp','true')
                    name=f'revive-{mode}-{player}'+('' if state=='up' else '-'+state)
                    xml,swf=source/(name+'.xml'),source/(name+'.swf')
                    tree.write(xml,encoding='utf-8',xml_declaration=True)
                    subprocess.run(command+['-xml2swf',str(xml),str(swf)],stdout=log,stderr=log,check=True,timeout=90)
                    subprocess.run(command+['-selectid','42','-export','sprite',str(source/name),str(swf)],stdout=log,stderr=log,check=True,timeout=90)


def generate(source,root=ROOT):
    origins={e['name']:e for e in json.loads((source/'manifest.json').read_text(encoding='utf8'))}
    for key in ('resurrection','gameOver'):
        if hashlib.sha256((source/origins[key]['file']).read_bytes()).hexdigest()!=origins[key]['sha256']:
            raise ValueError('UI source checksum changed: '+key)
    metadata=dict(version=1,source_version='v.20261019.00:01',sources=[origins[k] for k in ('resurrection','gameOver')],assets=[])
    output=root/'assets/resource/base/image/navigation/outcomes'
    output.mkdir(parents=True,exist_ok=True)

    def crop(file,name,box=None,mask=False,scale_first=False):
        with Image.open(file) as image:
            image=image.convert('RGBA')
            if box is not None and not scale_first:image=image.crop(box)
            image=image.resize((round(image.width*SCALE),round(image.height*SCALE)),Image.Resampling.LANCZOS)
            if box is not None and scale_first:image=image.crop(tuple(round(v*SCALE) for v in box))
            array=np.array(image)
            if float(array[:,:,:3].mean(axis=2).std())<12:
                raise ValueError('Refusing a low-texture UI crop: '+name)
            if mask:array[array[:,:,3]<245,:3]=(0,255,0)
            image=Image.fromarray(array[:,:,:3])
        target=output/name;image.save(target)
        relative=target.relative_to(root/'assets/resource/base/image').as_posix()
        metadata['assets'].append(dict(path=relative,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            source_frame=file.relative_to(source).as_posix(),source_frame_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
            crop=box,scale=SCALE,scale_first=scale_first,green_mask=mask))
        return relative

    captions=[]
    for mode,box in [('normal',(250,47,378,72)),('vip',(128,76,422,99))]:
        file=source/f'revive-{mode}-p1/DefineSprite_42_mc_fh/1.png'
        captions.append(crop(file,f'revive-{mode}-caption.png',box))
    layouts={}
    for players in ('p1','p2'):
        layouts[players]=[]
        for state in ('up','over','down'):
            folder=f'revive-normal-{players}'+('' if state=='up' else '-'+state)
            file=source/f'{folder}/DefineSprite_42_mc_fh/1.png'
            # Only the top edges and spacing of the button row. Text begins
            # below this band; countdown digits and glyph rasterization cannot
            # determine whether a cancel click is allowed.
            layouts[players].append(crop(file,f'revive-layout-{players}-{state}.png',
                (30,150,430,161),scale_first=True))
    path=root/'assets/resource/base/pipeline/battle_outcomes.json'
    nodes=json.loads(path.read_text(encoding='utf8'))
    caption=dict(type='TemplateMatch',param=dict(template=captions,roi=[200,170,800,390],threshold=.88))
    def reference(name):
        return dict(type='And',param=dict(all_of=[name]))
    # Flash text rasterization can miss the SWF caption template even at the
    # correct scale. Read this exact free-revival sentence only inside a matched
    # confirmation panel. Ordinary combat frames never invoke OCR. This only
    # interrupts playback; the existing panel dimensions and button layout must
    # still agree before Result_DeclineRevive permits a cancel click.
    free_caption=dict(type='And',param=dict(all_of=[
        dict(sub_name='confirm_frame',recognition=reference('Nav_CommonConfirm')),
        dict(recognition=dict(type='OCR',param=dict(roi='confirm_frame',
            roi_offset=[120,63,390,-5],only_rec=True,
            expected=[r'^你确定免费复活[，,]?\s*继续进行挑战[？?]?$']))),
    ],box_index=1))
    nodes['Result_RevivePrompt']=dict(recognition=dict(type='Or',param=dict(any_of=[
        dict(recognition=deepcopy(caption)),dict(recognition=free_caption),
    ])),action=dict(type='DoNothing'),next=[],post_delay=0)
    nodes['Result_NoRevivePrompt']=dict(recognition=dict(type='Custom',param=dict(custom_recognition='battle_no_revive')),
        action=dict(type='DoNothing'),next=[],post_delay=0)
    readonly=dict(action=dict(type='DoNothing'),next=[],post_delay=0)
    nodes['Result_ReviveWindow']=dict(recognition=dict(type='And',param=dict(all_of=[
        dict(sub_name='frame_corner',recognition=reference('Nav_CommonConfirm')),
        'Result_RevivePrompt',
        # The offset is valid only for this fixed-size resurrection panel,
        # not for a resized generic confirmation with the same border skin.
        dict(recognition=dict(type='TemplateMatch',param=dict(template='navigation/dialogs/confirm-right.png',
            roi='frame_corner',roi_offset=[491,-6,20,12],threshold=.9))),
        dict(recognition=dict(type='TemplateMatch',param=dict(template='navigation/dialogs/confirm-bottom.png',
            roi='frame_corner',roi_offset=[-6,197,12,-9],threshold=.9))),
    ],box_index=0)),**readonly)
    geometry=[]
    # FFDec's complete mc_fh rendering places the shared 38x43 corner patch
    # at (19,21) after 720p scaling. Convert the source button centers once;
    # runtime points are relative to the freshly matched window, never screen XY.
    anchor_origin=[19,21]
    for players,cancel_x in [('p1',269),('p2',349)]:
        source_box=[cancel_x,155,78,33]
        dx=round((cancel_x+39)*SCALE-anchor_origin[0])
        dy=round((155+33/2)*SCALE-anchor_origin[1])
        name='Result_ReviveLayout'+players.upper()
        nodes[name]=dict(recognition=dict(type='And',param=dict(all_of=[
            dict(sub_name='revive_window',recognition=reference('Result_ReviveWindow')),
            dict(recognition=dict(type='TemplateMatch',param=dict(template=layouts[players],
                roi='revive_window',roi_offset=[10,155,472,-8],threshold=.9))),
            dict(recognition=dict(type='DirectHit',param=dict(roi='revive_window',
                roi_offset=[dx,dy,-37,-42]))),
        ],box_index=2)),**readonly)
        geometry.append(dict(layout=players,anchor='Nav_CommonConfirm upper-left patch',
            anchor_size=[38,43],source_anchor_720=anchor_origin,cancel_offset=[dx,dy],target_size=[1,1],
            source_cancel_box=source_box))
    nodes['Result_DeclineRevive']=dict(recognition=dict(type='And',param=dict(all_of=[
        dict(recognition=dict(type='Or',param=dict(any_of=['Result_ReviveLayoutP1','Result_ReviveLayoutP2']))),
    ],box_index=0)),action=dict(type='Click',param=dict(target=True)),next=[],timeout=15000,pre_delay=0,post_delay=250)
    metadata['revive_geometry']=geometry
    for kind,character in [('Failed',95),('Succeeded',50)]:
        variants=[]
        for state in ('1_up','2_over','3_down'):
            file=source/f'gameOver/buttons/DefineButton2_{character}/{state}.png'
            variants.append(crop(file,f'{kind.lower()}-return-{state}.png',mask=True))
        all_of=nodes['Result_'+kind+'Return']['recognition']['param']['all_of']
        original=all_of[0]['recognition']
        if original['type']=='Or':original=original['param']['any_of'][0]['recognition']
        all_of[0]=dict(recognition=dict(type='Or',param=dict(any_of=[dict(recognition=original),
            dict(recognition=dict(type='TemplateMatch',param=dict(template=variants,
                roi=[400,400,700,260],threshold=.88,green_mask=True))),
        ])))
        if 'Result_NoRevivePrompt' not in all_of:all_of.append('Result_NoRevivePrompt')
    path.write_text(json.dumps(nodes,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    target=root/'agent/data/navigation/battle-ui.json'
    target.write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    print('Prepared guarded revival cancellation and result-button states')


def fixtures(source,root):
    output=root/'tests/fixtures/navigation/revival';output.mkdir(parents=True,exist_ok=True)
    cases=[]
    for mode in ('normal','vip'):
        for players in ('p1','p2'):
            for state in ('up','over','down'):
                folder=f'revive-{mode}-{players}'+('' if state=='up' else '-'+state)
                file=source/f'{folder}/DefineSprite_42_mc_fh/1.png'
                with Image.open(file) as image:
                    panel=image.convert('RGBA').resize((round(image.width*SCALE),round(image.height*SCALE)),Image.Resampling.BICUBIC)
                frame=Image.new('RGB',(1147,720),(50,60,70));frame.paste(panel,(300,240),panel)
                name=f'{mode}-{players}-{state}.png';frame.save(output/name)
                cases.append(dict(file=name,mode=mode,players=players,state=state,
                    cancel_box=[300+round((269 if players=='p1' else 349)*SCALE),240+round(155*SCALE),round(78*SCALE),round(33*SCALE)],
                    source_frame_sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
    (output/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    returns=[]
    for kind,character in [('failed',95),('succeeded',50)]:
        for state in ('1_up','2_over','3_down'):
            file=source/f'gameOver/buttons/DefineButton2_{character}/{state}.png'
            with Image.open(file) as image:
                button=image.convert('RGBA').resize((round(image.width*SCALE),round(image.height*SCALE)),Image.Resampling.BICUBIC)
            frame=Image.new('RGB',(1147,720),(50,60,70))
            with Image.open(root/f'assets/resource/base/image/navigation/outcomes/{kind}-title.png') as title:
                frame.paste(title,(350,200))
            frame.paste(button,(600,510),button)
            name=f'return-{kind}-{state}.png';frame.save(output/name)
            returns.append(dict(file=name,kind=kind,button_box=[600,510,button.width,button.height],
                source_frame_sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
    (output/'return-cases.json').write_text(json.dumps(returns,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--ffdec-jar',type=Path)
    parser.add_argument('--fixtures',action='store_true')
    args=parser.parse_args()
    if args.ffdec_jar:render_dialogs(args.source,args.ffdec_jar)
    generate(args.source,args.root)
    if args.fixtures:fixtures(args.source,args.root)
