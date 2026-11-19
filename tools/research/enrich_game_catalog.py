"""Join official facts, dated client configuration and community reference pages.

Inputs are explicit offline research exports. This tool never reads a player's
save, recordings, bindings, installed client configuration or live game state.
"""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import quote, urljoin
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from PIL import Image
import requests
from catalog_links import add_catalog_links

STATS = {'hpMax':'生命', 'mpMax':'魔法', 'atk':'攻击', 'phyDef':'防御', 'magRes':'魔抗',
         'reduceMagDef':'破魔', 'hit':'命中', 'dodge':'闪避', 'critical':'暴击',
         'toughness':'韧性', 'lucky':'幸运', 'guardian':'守护', 'reHP':'回血',
         'reMP':'回魔', 'suckBlood':'吸血', 'backLash':'反伤', 'spiritHit':'魂击率'}
BASE = 'https://sda.4399.com/4399swf/upload_swf/ftp15/csya/20150127/1/'
WIKI = 'https://rotating-stars.github.io/zmxy4-helpPages/'
CONFIG_SHA256 = '57139ad6c4cac1476f57b165cdd9d6fdc4b4aab61ce0e1905cfa8b253da9e45c'


def records(path):
    return [{e.tag: e.text or '' for e in row} for row in ET.parse(path).getroot()]


def enrich(catalog, assets, wiki):
    data = json.loads(catalog.read_text(encoding='utf8'))
    manifest = json.loads((assets/'manifest.json').read_text(encoding='utf8'))
    origin = next(s for s in manifest if s['name']=='config')
    if hashlib.sha256((assets/origin['file']).read_bytes()).hexdigest() != origin['sha256']:
        raise ValueError('Configuration source checksum changed')
    if origin['sha256'] != CONFIG_SHA256:
        raise ValueError('Importer metadata is pinned to the 2026-10-19 client configuration; update it before importing another snapshot')
    config = assets/'config'
    resources = {e['resourcesId']: e for e in records(config/'Resource.xml')}
    by_name = defaultdict(list)
    for kind, file in [('装备','Equipment.xml'),('道具','Prop.xml'),('Boss','Monster.xml')]:
        for row in records(config/file):
            if kind=='Boss' and row.get('boss')!='1':
                continue
            by_name[(kind,row['name'])].append(row)
    beads = defaultdict(list)
    for row in records(config/'SoulBead.xml'):
        beads[row.get('name', '')].append(row)
    locations = defaultdict(set)
    for row in records(config/'Dungeon.xml'):
        for ident in re.split('[;,]', row.get('monsterID','')):
            locations[ident].add(row['name'])
        if row.get('bossId') not in ('',None,'-1'):
            locations[row['bossId']].add(row['name'])
    names = {e['name']: e for e in data['entries']}
    icons = {}
    for e in data['entries']:
        e['references'] = [dict(title='4399 官方电脑版图鉴', url=e['source_url'], kind='official', date=e['published'])]
        e['config_variants'], e['wiki_facts'] = [], []
        rows = by_name.get((e['category'],e['name']), [])
        for row in rows:
            values = [dict(label=label, value=row[key]) for key,label in STATS.items()
                      if row.get(key) and row[key] not in ('0','-1')]
            if e['category']=='道具':
                values = [dict(label=label,value=row[key]) for key,label in [('userLevel','使用等级'),('price','出售基准价格')]
                          if row.get(key) and row[key] not in ('0','-1')]
                if row.get('describe'):
                    effect = BeautifulSoup(row['describe'],'html.parser').get_text(' ',strip=True)
                    values.insert(0,dict(label='作用',value=effect))
            if values:
                e['config_variants'].append(dict(id=row['id'], level=row.get('level',row.get('userLevel','')),
                    locations=sorted(locations[row['id']]) if e['category']=='Boss' else [], stats=values))
        # The official guide has copied the guardian attribute into several
        # high-level bead descriptions. Keep the source text, flag an explicit
        # mismatch and show the independently sourced client configuration.
        bead_rows = beads.get(e['name'], []) if e['category']=='道具' else []
        if len(bead_rows)==1:
            bead = bead_rows[0]
            label = STATS.get(bead.get('attribute', ''))
            value = bead.get('attributeT1', '')
            if label and value and value not in ('0', '-1'):
                e['config_variants'].append(dict(id=bead['id'], level='', locations=[],
                    stats=[dict(label=label+'增幅（比例）', value=value)]))
                for fact in e['facts']:
                    if fact['label'] != '用途':
                        continue
                    mentioned = {stat for stat in STATS.values() if stat+'值' in fact['value']}
                    if mentioned and label not in mentioned:
                        fact['label'] = '用途（官方图鉴原文，与客户端配置冲突）'
        if e['config_variants']:
            e['references'].append(dict(title='官方客户端配置（基准值，非实战结算）',url=origin['url'],kind='client',date='2026-10-19'))
        # An exact, unambiguous name/resource join is required for item artwork.
        paths = {resources.get(row.get('resourceId',''),{}).get('normal','') for row in rows}
        if e['category']!='Boss' and len(paths)==1:
            path = next(iter(paths))
            if re.fullmatch(r'[A-Za-z0-9_-]+\.(png|jpg)',path):
                e['icon'] = 'icons/' + path.rsplit('.',1)[0] + '.png'
                icons[e['icon']] = BASE + 'assets/icon/prop/' + path
        e.pop('icon_url', None)
    wiki_revision = subprocess.check_output(['git','-C',str(wiki),'rev-parse','HEAD'],text=True).strip()
    matched_pages = set()
    for page in sorted((wiki/'docs').rglob('*.html')):
        soup = BeautifulSoup(page.read_bytes(),'html.parser')
        content = soup.select_one('.vp-doc')
        if content is None:
            continue
        url = WIKI + quote(page.relative_to(wiki/'docs').as_posix(),safe='/')
        selected = None
        for element in content.find_all(['h1','h2','h3','h4','table']):
            if element.name.startswith('h'):
                label = element.get_text(' ',strip=True).replace('\u200b','').replace('#','').strip()
                selected = names.get(label)
                if selected is not None:
                    ref = dict(title='社区资料：'+page.stem,url=url,kind='wiki',date='2026-10-29')
                    if ref not in selected['references']:
                        selected['references'].append(ref)
                        matched_pages.add(url)
            elif selected is not None:
                lines = [[c.get_text(' ',strip=True) for c in r.find_all(['td','th'])] for r in element.find_all('tr')]
                if 1 < len(lines) <= 35 and any(re.search(r'\d', ' '.join(row)) for row in lines):
                    selected['wiki_facts'].append(dict(label='社区资料表', value='\n'.join(' · '.join(row) for row in lines),source_url=url))
    data['client_snapshot'] = dict(version='v.20261019.00:01',url=origin['url'],sha256=origin['sha256'],
        explanation='配置中的分号表示区间端点；小数比例保留原值（0.05 = 5%）。同名 Boss 的不同记录分别列出，不能将关卡、等级或形态混为同一实战数值。')
    data['wiki_snapshot'] = dict(repository='https://github.com/Rotating-stars/zmxy4-helpPages',revision=wiki_revision,
        license='MIT',matched_pages=len(matched_pages),date='2026-10-29')
    return add_catalog_links(data), icons


def download_icons(icons, output, cache):
    cache.mkdir(parents=True, exist_ok=True)
    failures, manifest = [], []
    def download(item):
        relative,url=item
        cached=cache/(hashlib.sha256(url.encode()).hexdigest()+'.png')
        try:
            if not cached.exists():
                time.sleep(.12)
                response=requests.get(url,headers={'Referer':'https://www.4399.com/flash/zmhj.htm?g=4'},timeout=(10,25))
                response.raise_for_status()
                if len(response.content)>1_000_000:
                    raise ValueError('Unexpected image size')
                with Image.open(BytesIO(response.content)) as image:
                    if max(image.size)>512:
                        raise ValueError('Not an item icon')
                    image.convert('RGBA').save(cached)
            target=output/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(cached,target)
            return dict(path=relative,url=url,sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        except (requests.RequestException,OSError,ValueError) as error:
            failures.append(dict(path=relative,url=url,error=str(error)))
            return None
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i,result in enumerate(pool.map(download,sorted(icons.items())),1):
            if result:manifest.append(result)
            if i%100==0:print(f'Icons {i}/{len(icons)}, unavailable={len(failures)}',flush=True)
    (cache/'failures.json').write_text(json.dumps(failures,ensure_ascii=False,indent=2),encoding='utf8')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('catalog','assets','wiki','output','icon-cache'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    data,icons=enrich(args.catalog,args.assets,args.wiki)
    args.output.mkdir(parents=True,exist_ok=True)
    manifest=download_icons(icons,args.output,args.icon_cache)
    available={e['path'] for e in manifest}
    for e in data['entries']:
        if e.get('icon') not in available:e.pop('icon',None)
    for name,value in [('catalog.json',data),('icons.json',dict(version=1,icons=manifest))]:
        (args.output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    shutil.copyfile(args.wiki/'LICENSE',args.output/'LICENSE-zmxy4-helpPages.txt')
    print(f"Saved {len(data['entries'])} entries, {len(available)} icons; wiki pages={data['wiki_snapshot']['matched_pages']}")
