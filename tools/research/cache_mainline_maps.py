"""Cache map resources referenced by the offline client's level configuration.

Raw game assets stay outside the source tree. Existing downloads are immutable;
each URL, layout and content hash is recorded before offline extraction.
"""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]


def cache(output, ffdec, *, legacy_config=None, export=False):
    output=output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError('Raw game assets must remain outside the source tree')
    stages=json.loads((ROOT/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf-8'))
    # AssetsURL.getMapURL from the offline main SWF appends assets/map/<mapURL>.
    origin=stages['provinces'][0]['source']['url'].split('/assets/')[0]
    if not origin.startswith('https://sda.4399.com/'):
        raise ValueError('Unexpected game resource origin')
    names={s['map_url'] for s in stages['stages']}
    if legacy_config:
        names.update(rec.findtext('mapURL') for rec in ET.parse(legacy_config).getroot()
                     if 1<=int(rec.findtext('id'))<=62)
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+',name or '') for name in names):
        raise ValueError('Unsafe map filename')
    maps=output/'maps';maps.mkdir(parents=True,exist_ok=True)
    prior={row['file']:row for row in json.loads((output/'manifest.json').read_text(encoding='utf-8'))['files']} if (output/'manifest.json').exists() else {}
    def fetch(filename):
        path=maps/filename;url=origin+'/assets/map/'+filename
        if path.exists():
            data=path.read_bytes()
        else:
            request=urllib.request.Request(url,headers={'Referer':'https://www.4399.com/flash/zmhj.htm?g=4','User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(request,timeout=45) as response:data=response.read()
            if filename.endswith('.swf'):
                if data[:3] not in (b'FWS',b'CWS',b'ZWS'):raise ValueError('Invalid SWF response: '+filename)
            elif filename.endswith('.json'):json.loads(data)
            elif filename.endswith('.png'):
                if not data.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid texture response: '+filename)
            else:ET.fromstring(data)
            path.write_bytes(data)
        digest=hashlib.sha256(data).hexdigest()
        if filename in prior and prior[filename]['sha256']!=digest:
            raise ValueError('Cached map was changed: '+filename)
        return dict(file=filename,url=url,bytes=len(data),sha256=digest)
    with ThreadPoolExecutor(max_workers=4) as pool:
        files=list(pool.map(fetch,[name+suffix for name in sorted(names) for suffix in ('.dat','.swf')]))
        animations={node.get('value').split(',')[0] for name in names
            for node in ET.parse(maps/(name+'.dat')).iter('property') if node.get('name')=='aniPath'}
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+',name) for name in animations):raise ValueError('Unsafe animation name')
        files.extend(pool.map(fetch,[name+suffix for name in sorted(animations)
            for suffix in ('_skeleton.json','_texture.json','_texture.png')]))
    manifest=dict(version=1,offline_client_repository='https://github.com/AperturePlus/ZaoMengXiYou-4',
        loading_evidence='com/game/net/AssetsURL.as:getMapURL and controllers/GoInBattleCmd.as',
        configuration_sha256=stages['configuration_sha256'],files=files)
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Cached',len(names),'maps /',len(files),'files /',sum(f['bytes'] for f in files),'bytes',flush=True)
    if export:
        def extract(name):
            destination=output/'exports'/name
            destination.mkdir(parents=True,exist_ok=True)
            symbols=next(destination.rglob('symbols.csv'),None)
            command=['java','-Djava.awt.headless=true','-Xmx512m','-jar',str(ffdec)]
            with (output/(name+'-export.log')).open('a',encoding='utf-8') as log:
                if symbols is None:
                    subprocess.run(command+['-onerror','abort','-format','image:png','-export','image,symbolClass',str(destination),str(maps/(name+'.swf'))],
                        stdout=log,stderr=subprocess.STDOUT,timeout=120,check=True)
                    symbols=next(destination.rglob('symbols.csv'))
                classes={row[1]:row[0] for row in csv.reader(symbols.read_text(encoding='utf-8').splitlines(),delimiter=';')}
                layers=[layer.get('name','') for layer in ET.parse(maps/(name+'.dat')).getroot().findall('imagelayer')
                        if layer.get('visible')!='0']
                missing=[classes[layer] for layer in layers if layer in classes and not list((destination/'images').glob('*_'+layer+'.png'))]
                existing=json.loads((destination/'background-sprites.json').read_text(encoding='utf-8')).get('ids',[]) if (destination/'background-sprites.json').is_file() else []
                if missing and set(existing)!=set(missing):
                    sprite_ids=sorted(set(missing),key=int)
                    subprocess.run(command+['-onerror','abort','-select',','.join(value+':1' for value in sprite_ids),'-selectid',','.join(sprite_ids),
                        '-export','sprite',str(destination),str(maps/(name+'.swf'))],stdout=log,stderr=subprocess.STDOUT,timeout=120,check=True)
                    subprocess.run(command+['-swf2xml','-external','image',str(maps/(name+'.swf')),str(destination/'map.xml')],
                        stdout=log,stderr=subprocess.STDOUT,timeout=120,check=True)
                    (destination/'background-sprites.json').write_text(json.dumps(dict(ids=missing)),encoding='utf-8')
                if missing and (set(existing)!=set(missing) or not (destination/'background-phases.json').is_file()):
                    tree=ET.parse(destination/'map.xml')
                    counts={node.get('spriteId'):int(node.get('frameCount','1')) for node in tree.iter() if node.get('type')=='DefineSpriteTag'}
                    selection=[]
                    for ident in sorted(set(missing),key=int):
                        count=counts[ident]
                        selection.extend(ident+':'+str(frame) for frame in sorted({1,1+count//3,1+2*count//3}))
                    subprocess.run(command+['-onerror','abort','-select',','.join(selection),'-selectid',','.join(sorted(set(missing),key=int)),
                        '-export','sprite',str(destination),str(maps/(name+'.swf'))],stdout=log,stderr=subprocess.STDOUT,timeout=120,check=True)
                    (destination/'background-phases.json').write_text(json.dumps(dict(selection=selection)),encoding='utf-8')
                terrain=[ident for key,ident in classes.items() if key.startswith(('ground','floor'))]
                existing_terrain=json.loads((destination/'terrain-sprites.json').read_text(encoding='utf-8')).get('ids',[]) if (destination/'terrain-sprites.json').is_file() else []
                if terrain and set(terrain)!=set(existing_terrain):
                    subprocess.run(command+['-onerror','abort','-select',','.join(value+':1' for value in terrain),
                        '-selectid',','.join(terrain),'-export','sprite',str(destination),str(maps/(name+'.swf'))],
                        stdout=log,stderr=subprocess.STDOUT,timeout=120,check=True)
                    if not (destination/'map.xml').is_file():
                        subprocess.run(command+['-swf2xml','-external','image',str(maps/(name+'.swf')),str(destination/'map.xml')],
                            stdout=log,stderr=subprocess.STDOUT,timeout=120,check=True)
                    (destination/'terrain-sprites.json').write_text(json.dumps(dict(ids=terrain)),encoding='utf-8')
            return name
        with ThreadPoolExecutor(max_workers=2) as pool:
            for number,name in enumerate(pool.map(extract,sorted(names)),1):
                print('Exported',number,'/',len(names),name,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--ffdec',required=True,type=Path)
    parser.add_argument('--legacy-config',type=Path)
    parser.add_argument('--export',action='store_true')
    args=parser.parse_args()
    cache(args.output,args.ffdec,legacy_config=args.legacy_config,export=args.export)
