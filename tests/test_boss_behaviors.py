"""Offline evidence integrity, concise tree coverage, and bounded AMF decoding."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools/research'))
from amf_xml_records import read_records


def u29(number):
    if number < 128: return bytes([number])
    if number < 16384: return bytes([128 | number >> 7, number & 127])
    if number < 2097152: return bytes([128 | number >> 14, 128 | (number >> 7 & 127), number & 127])
    return bytes([128 | number >> 22, 128 | (number >> 15 & 127), 128 | (number >> 8 & 127), number & 255])


def string(value):
    data = value.encode('utf8')
    return u29(len(data)*2+1)+data


def amf(rows):
    return b'\x0a\x0b\x01'+b''.join(string(key)+b'\x0b'+string(value) for key,value in rows)+b'\x01'


def nodes(roots):
    for node in roots:
        yield node
        yield from nodes(node['children'])


class AmfXmlTests(unittest.TestCase):
    def test_utf8_and_multibyte_lengths_preserve_record_values(self):
        text='<RECORD><id>123</id><name>巨水灵</name><description>'+('水'*200)+'</description></RECORD>'
        record=read_records(amf([('123',text)]))['123']
        self.assertEqual(record.findtext('description'),'水'*200)
        self.assertEqual(record.findtext('name'),'巨水灵')

    def test_rejects_unsafe_or_inconsistent_records_and_truncation(self):
        good=amf([('123','<RECORD><id>123</id></RECORD>')])
        bad=[b'\x0a\x07\x01',good+b'junk',good[:-1],good[:5],
             amf([('123','<RECORD><id>124</id></RECORD>')]),
             amf([('123','<!DOCTYPE RECORD><RECORD><id>123</id></RECORD>')]),
             amf([('123','<RECORD><id>123</id></RECORD>')]*2),
             b'\x0a\x0b\x01'+string('123')+b'\x0a\x01',
             b'\x0a\x0b\x01\x02']
        for value in bad:
            with self.subTest(value=value[:20]),self.assertRaises(ValueError):read_records(value)


class BossBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((ROOT/'agent/data/game_database/boss-behaviors.json').read_text(encoding='utf8'))
        self.catalog={e['id']:e for e in json.loads((ROOT/'agent/data/game_database/catalog.json').read_text(encoding='utf8'))['entries']}

    def test_only_the_five_selected_mainline_variants_are_attached_by_stable_id(self):
        profiles=self.data['profiles']
        self.assertEqual({self.catalog[p['entry_id']]['name'] for p in profiles},{'诛仙剑','戮仙剑','陷仙剑','绝仙剑','巨水灵'})
        self.assertEqual(len(profiles),5)
        for p in profiles:
            variants={v['id'] for v in self.catalog[p['entry_id']]['config_variants']}
            self.assertTrue(set(p['monster_ids'])<=variants)
            self.assertEqual(len(p['monster_ids']),1)
            self.assertIn('主线普通',p['scope'])
            self.assertIn('2026-10-19',p['version'])
            self.assertIn('历史快照',p['version'])
            self.assertIn('未实战复核',p['summary'])

    def test_each_claim_resolves_to_versioned_hashed_evidence_without_local_paths(self):
        evidence={e['id']:e for e in self.data['evidence']}
        self.assertEqual(len(evidence),len(self.data['evidence']))
        for e in evidence.values():
            self.assertRegex(e['sha256'],r'^[a-f0-9]{64}$')
            self.assertRegex(e['source_sha256'],r'^[a-f0-9]{64}$')
            self.assertTrue(e['url'].startswith(('https://github.com/AperturePlus/','https://sda.4399.com/')))
            self.assertTrue(e['version'] and e['locator'])
        for profile in self.data['profiles']:
            flat=list(nodes(profile['roots']))
            self.assertLess(len(flat),60)
            for n in flat:
                self.assertTrue(n['evidence_ids'])
                self.assertTrue(set(n['evidence_ids'])<=evidence.keys())
        self.assertNotIn('F:',json.dumps(self.data))

    def test_trees_preserve_conditional_special_mechanics_and_do_not_promise_hitboxes(self):
        profiles={self.catalog[p['entry_id']]['name']:p for p in self.data['profiles']}
        water=json.dumps(profiles['巨水灵'],ensure_ascii=False)
        healer=json.dumps(profiles['绝仙剑'],ensure_ascii=False)
        teleport=json.dumps(profiles['戮仙剑'],ensure_ascii=False)
        self.assertIn('死亡 → 水滴分裂',water)
        self.assertIn('第 44 帧',water)
        self.assertIn('不高于 90%',healer)
        self.assertIn('12 秒',healer)
        self.assertIn('30 秒',healer)
        self.assertIn('第 40 帧',teleport)
        invisibility=next(n for n in profiles['绝仙剑']['roots'][0]['children'] if '30 秒' in n['title'])
        self.assertNotIn('出招范围',invisibility['title'])
        self.assertLess(invisibility['text'].index('遁迹潜形'),invisibility['text'].index('出招范围'))
        for p in profiles.values():
            text=json.dumps(p,ensure_ascii=False)
            self.assertIn('这是 AI 选招范围',text)
            self.assertIn('未分析完整逐帧判定区域',text)

    def test_actual_resource_collector_includes_sidecar_and_excludes_research_inputs(self):
        sys.path.insert(0,str(ROOT/'tools'))
        from build_release import collect
        files={name for _,name in collect(str(ROOT))}
        self.assertIn('agent/data/game_database/boss-behaviors.json',files)
        self.assertFalse(any(name.endswith(('.as','.amf','.swf')) for name in files))


if __name__=='__main__':unittest.main()
