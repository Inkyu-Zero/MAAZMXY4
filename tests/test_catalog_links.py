"""Catalogue links use stable destinations and leave ambiguous names as text."""
from pathlib import Path
import importlib.util
import json
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('catalog_links',ROOT/'tools/research/catalog_links.py')
links=importlib.util.module_from_spec(spec);spec.loader.exec_module(links)


class CatalogueLinkTests(unittest.TestCase):
    def test_longest_name_wins_and_duplicate_names_do_not_get_guessed_targets(self):
        data={'entries':[dict(id='a',name='晶石',facts=[]),dict(id='b',name='七彩晶石',facts=[]),
            dict(id='c',name='重名',facts=[]),dict(id='d',name='重名',facts=[]),
            dict(id='weapon',name='武器',facts=[dict(label='获取方式',value='七彩晶石+晶石+重名')])]}
        links.add_catalog_links(data)
        fact=data['entries'][-1]['facts'][0]
        self.assertEqual(fact['value'],'七彩晶石+晶石+重名')
        self.assertEqual(fact['links'],[dict(text='七彩晶石',target='entry:b'),dict(text='晶石',target='entry:a')])
        before=json.dumps(data,ensure_ascii=False)
        self.assertEqual(json.dumps(links.add_catalog_links(data),ensure_ascii=False),before)

    def test_every_generated_local_link_resolves_and_rebuild_is_idempotent(self):
        data=json.loads((ROOT/'agent/data/game_database/catalog.json').read_text(encoding='utf8'))
        ids={e['id'] for e in data['entries']};count=0
        before=json.dumps(data,ensure_ascii=False)
        for entry in data['entries']:
            for fact in entry['facts']:
                for link in fact.get('links',[]):
                    count+=1;self.assertIn(link['text'],fact['value'])
                    if link['target'].startswith('entry:'):self.assertIn(link['target'][6:],ids)
        self.assertGreater(count,2000)
        self.assertEqual(json.dumps(links.add_catalog_links(data),ensure_ascii=False),before)
