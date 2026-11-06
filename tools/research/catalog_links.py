"""Add stable local links to existing facts without changing their source text."""
from collections import defaultdict
import re

LINK_LABELS = {'获得方法','获取方法','获得方式','获取方式','作用','用途','制作方法','合成方法',
               '合成公式','制作材料','材料','掉落装备','掉落材料','掉落物品'}


def add_catalog_links(data):
    by_name = defaultdict(list)
    for entry in data['entries']:
        by_name[entry['name']].append(entry)
    unique = {name: rows[0]['id'] for name, rows in by_name.items() if len(rows)==1 and len(name)>1}
    pattern = re.compile('|'.join(re.escape(name) for name in sorted(unique,key=lambda n:(-len(n),n)))) if unique else None
    for entry in data['entries']:
        for fact in entry.get('facts',[]):
            if fact['label'] not in LINK_LABELS and not fact['label'].startswith('用途'):
                continue
            links = {link['text']: link for link in fact.get('links',[]) if link.get('text') in fact['value']}
            for match in pattern.finditer(fact['value']) if pattern else ():
                name=match[0]
                if unique[name] != entry['id']:
                    links[name]=dict(text=name,target='entry:'+unique[name])
            if links:
                fact['links']=sorted(links.values(),key=lambda link:fact['value'].find(link['text']))
    return data
