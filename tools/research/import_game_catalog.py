"""Build an offline PC-game fact catalogue from the public 4399 indexes.

Development-only dependencies: requests, beautifulsoup4. Raw HTML stays in an
explicit external cache; clients never scrape the web or execute HTML. Only
table facts are retained, excluding lore, adverts, comments and recommendations.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
import requests

BASE = 'https://news.4399.com/gonglue/zmxy4/'
INDEXES = {'zhuangbei': '装备', 'daoju': '道具', 'boss': 'Boss'}
FIELDS = {'获得方法', '获取方法', '获得方式', '获取方式', '作用', '用途', '类型', '品质',
          '使用条件', '使用等级', '装备等级', '等级', '属性', '基础属性', '强化属性',
          '成长属性', '出没点', '出没地点', '掉落装备', '掉落物品', '掉落材料', '编号', '生命',
          '攻击', '防御', '血量', '技能', '合成公式', '合成方法', '制作方法', '制作材料', '材料', '价格'}
LOCAL = threading.local()


def allowed(url):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and parsed.netloc == 'news.4399.com' and parsed.path.startswith('/gonglue/zmxy4/')


def read_page(url, cache, offline=False):
    if not allowed(url):
        raise ValueError('Not a PC-game official article: ' + url)
    path = cache / (hashlib.sha256(url.encode()).hexdigest() + '.html')
    if path.exists():
        return path.read_bytes()
    if offline:
        raise FileNotFoundError(url)
    if not hasattr(LOCAL, 'session'):
        LOCAL.session = requests.Session()
        LOCAL.session.headers['User-Agent'] = 'MAAZMXY4-catalog-research/1.0 (public PC-game reference)'
    time.sleep(.15)
    response = LOCAL.session.get(url, timeout=(10, 30))
    response.raise_for_status()
    if not allowed(response.url) or len(response.content) > 3_000_000:
        raise ValueError('Unexpected article response: ' + response.url)
    path.write_bytes(response.content)
    return response.content


def clean(element):
    # Preserve actual block/line breaks, not boundaries around inline links or
    # bold text. Otherwise recipes become one line per ingredient/operator and
    # <strong>Skill:</strong>description is accidentally split in two.
    parts = []
    for node in element.descendants:
        if isinstance(node, Tag) and node.name in ('br','p','div','li','tr','td','th'):
            parts.append('\n')
        elif type(node) is NavigableString and node.parent.name not in ('script','style'):
            parts.append(re.sub(r'\s+', ' ', str(node).replace('\xa0',' ')))
    return '\n'.join(line.strip() for line in ''.join(parts).splitlines() if line.strip())


def index_entries(content, kind):
    soup = BeautifulSoup(content, 'html.parser')
    labels = {}
    for a in soup.select('a[rel]'):
        rel = a.get('rel')
        if rel:
            labels[rel if isinstance(rel, str) else rel[0]] = a.get_text(strip=True)
    result = []
    for a in soup.select('li a[href]'):
        url = urljoin(BASE, a['href']).replace('http://', 'https://', 1)
        match = re.fullmatch(r'https://news\.4399\.com/gonglue/zmxy4/(?:zhuangbei|daoju|boss)/([0-9]+)\.html', url)
        if not match:
            continue
        name = a.get_text(strip=True)
        if not name or len(name) > 50:
            continue
        rel = a.parent.get('rel', '').split('*')[0]
        category = '道具' if rel == '38472' or kind == 'daoju' else INDEXES[kind]
        image = a.find('img')
        result.append(dict(id='4399-' + match[1], name=name, category=category,
            subtype=labels.get(rel, category), source_url=url,
            icon_url=urljoin(BASE, image.get('lz_src', image.get('src', ''))) if image else ''))
    return result


def skill_sections(soup, entry_name):
    """Read inline, multi-paragraph and named phase skill tables uniformly."""
    skills = []
    for table in soup.find_all('table'):
        if table.find_parent('table'):
            continue
        active, prefix, current = False, '', None
        for line in clean(table).splitlines():
            named = re.fullmatch(r'([^：:()（）]{1,24})技能(?:表|介绍|说明)?',line)
            phase = re.fullmatch(r'技能[（(]([^）)]{1,40})[）)]',line)
            if line in ('技能', '技能表', '技能说明', '技能介绍') or named or phase:
                active = True
                prefix = phase[1]+' · ' if phase else named[1]+' · ' if named and named[1]!=entry_name else ''
                current = None
                continue
            if line.startswith(('更多','推荐','热门','掉落','挑战攻略')) or line in ('简介','怪物简介','编号','出没点','出没地点'):
                active = False
            if not active:
                continue
            if current is not None and not current['description']:
                current['description'] = line
                continue
            match = re.match(r'(?:[\d一二三四五六七八九十]+[、.．]\s*)?([^:：]{2,20}?)(?:[:：]|——)(.*)',line)
            parenthesized = re.match(r'([^（(]{2,20})[（(](.*)',line)
            if match or parenthesized:
                match = match or parenthesized
                current = dict(name=prefix+match[1].strip(),description=match[2].strip().rstrip(')）') if parenthesized and not re.search('[:：]|——',line) else match[2].strip())
                skills.append(current)
            elif line in ('死亡技','场景技','被动技能','普通攻击'):
                current = dict(name=prefix+line,description='')
                skills.append(current)
            else:
                if current is None:
                    current = dict(name=prefix+'战斗机制',description='')
                    skills.append(current)
                current['description'] += line
    return list({(s['name'],s['description']):s for s in skills if s['description']}.values())


def linked_fact(label, value, node, source_url):
    fact = dict(label=label,value=value)
    links = []
    for anchor in node.find_all('a',href=True):
        text = clean(anchor)
        target = urljoin(source_url,anchor['href'])
        if target.startswith('http://news.4399.com/'):
            target = 'https://' + target[len('http://'):]
        uri = urlparse(target)
        if text and text in value and uri.scheme=='https' and uri.hostname=='news.4399.com' and not uri.username:
            link=dict(text=text,target=target)
            if link not in links:links.append(link)
    if links:fact['links']=links
    return fact


def article_facts(content, entry):
    soup = BeautifulSoup(content, 'html.parser')
    facts = []
    headers, mode = [], ''
    for table in soup.find_all('table'):
        if table.find_parent('table'):
            continue
        for row in table.find_all('tr'):
            if row.find_parent('table') is not table:
                continue
            cell_nodes = [td for td in row.find_all(['td','th'], recursive=False) if not td.find('table') and clean(td)]
            cells = [clean(td) for td in cell_nodes]
            if not cells:
                continue
            if any('更多推荐' in text or '推荐攻略' in text for text in cells):
                break
            if len(cells) == 1 and ('技能表' in cells[0] or cells[0] in ('技能', '技能介绍', '技能说明')):
                mode, headers = 'skills', []
                continue
            if all(text in FIELDS for text in cells):
                headers, mode = cells, 'facts'
                continue
            if len(cells) == 2 and cells[0] in FIELDS:
                facts.append(linked_fact(cells[0],cells[1],cell_nodes[1],entry.get('source_url','')))
                headers = []
                continue
            if mode == 'facts' and len(cells) == len(headers):
                facts.extend(linked_fact(key,value,td,entry.get('source_url','')) for key,value,td in zip(headers,cells,cell_nodes))
                headers = []
    # Source text is kept as text, never rendered as HTML. Unpublished values
    # stay absent (the UI displays "来源未提供"), never guessed or set to zero.
    facts = [f for f in facts if len(f['value']) <= 2000 and f['label'] != '技能']
    dedup = {(f['label'], f['value']): f for f in facts}
    entry = dict(entry, facts=list(dedup.values()), skills=skill_sections(soup, entry['name']),
                 source_sha256=hashlib.sha256(content).hexdigest())
    text = soup.get_text(' ', strip=True)
    published = re.search(r'时间[：:]\s*(\d{2,4}-\d{2}-\d{2})', text)
    entry['published'] = published[1] if published else ''
    entry['status'] = 'official-table' if facts or entry['skills'] else 'index-only'
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=3)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    entries = {}
    for kind in INDEXES:
        for entry in index_entries(read_page(BASE + kind + '/', args.cache, args.offline), kind):
            entries[entry['id']] = entry
    selected = list(entries.values())[:args.limit or None]
    print(f'Indexed {len(entries)} unique articles; reading {len(selected)}', flush=True)
    failures = []

    def fetch(entry):
        try:
            return article_facts(read_page(entry['source_url'], args.cache, args.offline), entry)
        except (requests.RequestException, OSError, ValueError) as error:
            failures.append(dict(url=entry['source_url'], error=str(error)))
            return dict(entry, facts=[], skills=[], published='', status='unavailable', source_sha256='')

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        result = []
        for i, entry in enumerate(pool.map(fetch, selected), 1):
            result.append(entry)
            if i % 100 == 0:
                print(f'Read {i}/{len(selected)}; failures={len(failures)}', flush=True)
    doc = dict(version=1, game='造梦西游4·洪荒大劫篇（电脑版）', checked_on=date.today().isoformat(),
               source_policy='官方图鉴优先；历史资料按页面日期标记。缺失数值不推算，手游资料不混用。',
               entries=sorted(result, key=lambda e: e['id']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    (args.cache / 'fetch-report.json').write_text(json.dumps(dict(total=len(result), failures=failures,
        detailed=sum(e['status']=='official-table' for e in result)), ensure_ascii=False, indent=2), encoding='utf8')
    print(f'Wrote {len(result)} entries; {len(failures)} unavailable', flush=True)


if __name__ == '__main__':
    main()
