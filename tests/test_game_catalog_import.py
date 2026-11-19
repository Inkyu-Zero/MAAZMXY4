"""Public table layout variations must not silently drop skills or recipes."""
import importlib.util
from pathlib import Path
import struct
import unittest
import zlib

ROOT=Path(__file__).resolve().parents[1]
try:
    import bs4
    import requests
except ImportError:
    bs4=None


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tools/research'/(name+'.py'))
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


@unittest.skipUnless(bs4 is not None,'development-only BeautifulSoup/requests')
class CatalogueImportTests(unittest.TestCase):
    def test_fact_links_keep_original_text_and_only_public_https_destinations(self):
        html=b'<table><tr><td>\xe8\x8e\xb7\xe5\xbe\x97\xe6\x96\xb9\xe6\xb3\x95</td><td><a href="/gonglue/zmxy4/daoju/example.htm">material</a>+60 <a href="javascript:alert(1)">unsafe</a></td></tr></table>'
        result=module('import_game_catalog').article_facts(html,dict(name='test',source_url='https://news.4399.com/gonglue/zmxy4/item.htm'))
        fact=result['facts'][0]
        self.assertIn('material',fact['value']);self.assertIn('unsafe',fact['value'])
        self.assertEqual(fact['links'],[dict(text='material',target='https://news.4399.com/gonglue/zmxy4/daoju/example.htm')])

    def test_skills_with_inline_markup_and_parentheses(self):
        html='''<table><tr><td>技能</td></tr>
        <tr><td><p><strong>追击：</strong>朝前方冲刺</p></td></tr>
        <tr><td>旋转（攻击周围目标）</td></tr>
        <tr><td>死亡技</td></tr><tr><td>移除场景障碍</td></tr></table>'''
        result=module('import_game_catalog').article_facts(html.encode(),{'name':'示例Boss'})
        self.assertEqual(result['skills'],[{'name':'追击','description':'朝前方冲刺'},
            {'name':'旋转','description':'攻击周围目标'},{'name':'死亡技','description':'移除场景障碍'}])

    def test_crafting_formula_and_no_lore_or_recommendations(self):
        html='''<table><tr><td>描述</td></tr><tr><td>不收录的背景故事</td></tr>
        <tr><td>制作方法</td></tr><tr><td>图纸+矿石*2</td></tr>
        <tr><td>属性</td><td>攻击:4-8</td></tr>
        <tr><td>更多推荐</td></tr><tr><td>属性</td><td>别的装备</td></tr></table>'''
        result=module('import_game_catalog').article_facts(html.encode(),{'name':'示例装备'})
        self.assertEqual(result['facts'],[{'label':'制作方法','value':'图纸+矿石*2'},
            {'label':'属性','value':'攻击:4-8'}])

    def test_inline_links_do_not_break_a_recipe_into_separate_lines(self):
        html='''<table><tr><td>制作方法</td></tr>
        <tr><td><p><a href="/a">图纸</a>+<a href="/b">矿石</a>*2</p></td></tr></table>'''
        facts=module('import_game_catalog').article_facts(html.encode(),{'name':'示例'})['facts']
        self.assertEqual(facts,[{'label':'制作方法','value':'图纸+矿石*2'}])

    def test_named_phases_dash_and_unlabelled_mechanics(self):
        html='''<table><tr><td>甲技能</td></tr><tr><td>横扫——攻击前方</td></tr>
        <tr><td>乙技能</td></tr><tr><td>横扫：攻击后方</td></tr></table>
        <table><tr><td>技能表</td></tr><tr><td>击破全部分身后进入第二阶段。</td></tr>
        <tr><td>更多BOSS推荐</td></tr><tr><td>不应出现：其它条目</td></tr></table>'''
        skills=module('import_game_catalog').article_facts(html.encode(),{'name':'示例'})['skills']
        self.assertEqual([s['name'] for s in skills],['甲 · 横扫','乙 · 横扫','战斗机制'])

    def test_named_skill_table_and_separate_parenthesized_description(self):
        html='''<table><tr><td>示例Boss技能表</td></tr><tr><td>结界：</td></tr>
        <tr><td>在场景中央生成剑阵（一段时间后消失）</td></tr>
        <tr><td>技能（示例Boss-第二形态）</td></tr><tr><td>冲击：向前突进</td></tr></table>'''
        skills=module('import_game_catalog').article_facts(html.encode(),{'name':'示例Boss'})['skills']
        self.assertEqual(skills,[{'name':'结界','description':'在场景中央生成剑阵（一段时间后消失）'},
            {'name':'示例Boss-第二形态 · 冲击','description':'向前突进'}])


class GameConfigTests(unittest.TestCase):
    def container(self,name,payload,mode=None):
        encoded=name.encode();header=struct.pack('>H',len(encoded))+encoded
        block=struct.pack('>H',len(header))+header+(b'' if mode is None else struct.pack('>H',mode))+payload
        comp=zlib.compressobj(wbits=-15);raw=comp.compress(block)+comp.flush()
        return (b'mod' if mode is None else b'mo2')+struct.pack('>HI',1,len(raw))+raw

    def test_old_and_current_containers_and_opaque_amf(self):
        unpack=module('unpack_game_config').unpack
        for mode in (None,0):self.assertEqual(unpack(self.container('A.xml',b'<data/>',mode)),{'A.xml':b'<data/>'})
        self.assertEqual(unpack(self.container('A.xml',b'opaque',1)),{'A.xml.amf':b'opaque'})

    def test_invalid_or_escaping_inputs_are_rejected(self):
        unpack=module('unpack_game_config').unpack
        for blob in [b'CWS',self.container('../secret.xml',b'x'),self.container('A.xml',b'x')[:-1],self.container('A.xml',b'x',7)]:
            with self.assertRaises(ValueError):unpack(blob)


if __name__=='__main__':unittest.main()
