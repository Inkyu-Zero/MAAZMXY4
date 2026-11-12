"""Build five readable, evidence-linked Boss trees from explicit offline inputs.

No game is executed. Original SWF/config/AS3 files stay outside the package.
This is a reviewed summary for five normal mainline variants, not an AI VM.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from amf_xml_records import read_records

from unpack_game_config import unpack

CONFIG_SHA = '57139ad6c4cac1476f57b165cdd9d6fdc4b4aab61ce0e1905cfa8b253da9e45c'
CODE_SHA = 'f6e04fd8139ff3873cd2bcb63488ab7805d524c1188daa235891ac8fed6e8147'
SAMPLES = [('3620', '4399-859463'), ('3625', '4399-859466'),
           ('3626', '4399-859465'), ('3627', '4399-859464'), ('3184', '4399-541433')]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def node(title, text='', evidence=(), children=()):
    return dict(title=title, text=text, evidence_ids=list(dict.fromkeys(evidence)), children=list(children))


def fields(record):
    return {child.tag: child.text or '' for child in record}


def seconds(value):
    return '～'.join(f'{int(v)/1000:g}' for v in value.split(',')) + ' 秒'


def build(assets, exports, singleplayer_swf, catalog):
    origin = next(s for s in json.loads((assets/'manifest.json').read_text(encoding='utf8')) if s['name']=='config')
    container = (assets/origin['file']).read_bytes()
    if sha(container) != CONFIG_SHA or sha(singleplayer_swf.read_bytes()) != CODE_SHA:
        raise ValueError('Unreviewed config or single-player SWF snapshot')
    raw = unpack(container)
    for name in ('Monster.xml', 'MAi.xml', 'Behavior.xml', 'Skill.xml.amf'):
        if raw[name] != (assets/'config'/name).read_bytes():
            raise ValueError('Config export differs from its container: '+name)
    tables = {name: {r.findtext('id'): r for r in ET.fromstring(raw[name])}
              for name in ('Monster.xml', 'MAi.xml', 'Behavior.xml')}
    skills = read_records(raw['Skill.xml.amf'])
    behavior = {ident: fields(r) for ident, r in tables['Behavior.xml'].items()}
    entries = {e['id']: e for e in catalog['entries']}
    export_manifest = json.loads((exports/'export-manifest.json').read_text(encoding='utf8'))
    if export_manifest['sources']['sources'][0]['sha256'] != CODE_SHA:
        raise ValueError('AS3 export belongs to another SWF')
    exported_hashes = {f['path']: f['sha256'] for f in export_manifest['files']}
    revision = export_manifest['sources']['source_revision']
    code_url = export_manifest['sources']['repository'] + '/commit/' + revision
    evidence = {}

    def config_evidence(table, ident, label):
        key = table.removesuffix('.xml').lower()+'-'+ident
        file = 'Skill.xml.amf' if table == 'Skill.xml' else table
        evidence[key] = dict(id=key, title=label, version='客户端配置 2026-10-19',
            locator=f'{file} · RECORD id={ident}', sha256=sha(raw[file]),
            url=origin['url'], source_sha256=CONFIG_SHA)
        return key

    def code_evidence(file, locator, anchors=()):
        key = 'code-'+Path(file).stem.lower()
        relative = 'main/scripts/'+file
        data = (exports/relative).read_bytes()
        if sha(data) != exported_hashes.get(relative):
            raise ValueError('AS3 export hash mismatch: '+file)
        text = data.decode('utf8')
        if any(anchor not in text for anchor in anchors):
            raise ValueError('Reviewed AS3 anchor changed: '+file)
        evidence[key] = dict(id=key, title=Path(file).stem+' · 静态代码',
            version='单机代码快照 '+revision[:7]+'（历史版本）', locator=file+' · '+locator,
            sha256=sha(data), url=code_url, source_sha256=CODE_SHA)
        return key

    range_code = code_evidence('models/managers/BattleModelManager.as', 'getManInAttackRangeView / checkInRance', ['param1.getSkillScope(param2)'])
    hit_code = code_evidence('viewers/entitys/man/PlayerEntity.as', '命中上限与间隔 / hitTest / getAttackBp', ['bitmapData.hitTest(', '_loc3_ < _loc6_.attInterval'])
    ai_code = code_evidence('models/ai/AITree.as', 'execute · 条件与分支调度')
    random_code = code_evidence('models/ai/RandChildSelNode.as', 'checkAllChild · 从条件成立的分支中选取', ['getRandom() * _loc1_.length'])
    wait_code = code_evidence('models/ai/acs/IdleAI.as', 'init · 等待区间', ['this.data.value[1]'])
    hp_code = code_evidence('models/ai/GetHpConNode.as', 'checkCondition · 下界开、上界闭', ['_loc2_ > data.value[0] && _loc2_ <= data.value[1]'])
    cd_code = code_evidence('models/ai/GetAttCDConNode.as', 'checkCondition / onExecute · 分支计时', ['_cd = _totalTime'])
    sequence_code = code_evidence('com/game/engine/ai/behaviors/node/CompositeNode.as', 'addNode · 只预检首个动作前的条件', ['param1 is IConditionNode && _checkFirstCon'])
    patrol_code = code_evidence('models/ai/GetManOnViewConNode.as', 'checkCondition · 视野内无对象时返回成立', ['_loc1_.length == 0'])
    profiles = []
    for mid, entry_id in SAMPLES:
        monster = fields(tables['Monster.xml'][mid])
        entry = entries[entry_id]
        if entry['name'] != monster['name'] or not any(v['id']==mid for v in entry['config_variants']):
            raise ValueError('Stable encyclopedia variant mismatch: '+mid)
        ai = tables['MAi.xml'][monster['aiId']]
        skill_map = {f'effect{i}': fields(skills[monster[f'effect{i}']]) for i in range(1,4)}
        def skill_name(slot):
            return skill_map[slot]['name'].split('-')[-1]
        monster_ev = config_evidence('Monster.xml', mid, monster['name']+' · 主线普通配置')
        ai_ev = config_evidence('MAi.xml', monster['aiId'], monster['name']+' · 行为树')
        used_ids = {n.findtext('id') for n in ai.iter('node')}
        behavior_ev = [config_evidence('Behavior.xml', ident, '行为节点 '+behavior[ident]['name']) for ident in sorted(used_ids)]

        def describe(ident):
            item = behavior[ident]; name, value = item['name'], item.get('value', '')
            if name == 'GetHp':
                lo, hi = map(float, value.split(','))
                return f'生命高于 {lo*100:g}% 且不高于 {hi*100:g}%'
            if name == 'GetAttCD': return '分支计时已就绪（配置间隔 '+seconds(value)+'）'
            match = re.fullmatch(r'GetInSkill([1-3])Range', name)
            if match: return '目标进入「'+skill_name('effect'+match[1])+'」的出招范围'
            match = re.fullmatch(r'GotoSkill([1-3])(Range)?', name)
            if match:
                skill = '「'+skill_name('effect'+match[1])+'」'
                return ('接近到'+skill+'出招范围' if match[2] else skill)
            if name == 'GotoWait': return '等待 '+seconds(value)
            if name == 'GotoAway': return '退避 '+seconds(value)
            raise ValueError('Unreviewed behavior node: '+name)

        attacks = []
        for top in ai.findall('./nodes/node'):
            if behavior[top.findtext('id')]['name'] != 'AttackSeq': continue
            branches = top.findall('./nodes/node')
            if len(branches)==1 and behavior[branches[0].findtext('id')]['name']=='RandChild':
                grouped = defaultdict(list)
                for branch in branches[0].findall('./nodes/node'):
                    parts = branch.findall('./nodes/node')
                    first = behavior[parts[0].findtext('id')]
                    condition = describe(parts[0].findtext('id')) if first['type']=='con' else '需要先接近目标'
                    actions = parts[1:] if first['type']=='con' else parts
                    grouped[condition].append(' → '.join(describe(n.findtext('id')) for n in actions))
                attacks.append(node('随机选择一条满足条件的连招', '每条分支按顺序执行；不代表固定轮流出招，也不等于各技能等概率。',
                    [ai_ev, random_code, wait_code, *behavior_ev],
                    [node(condition, evidence=[ai_ev, range_code], children=[node(route, evidence=[ai_ev]) for route in routes])
                     for condition, routes in grouped.items()]))
            else:
                # Only leading conditions are prechecked by CompositeNode.
                # A condition after an action executes in sequence: moving it
                # to the header would change when invisibility can start.
                prefix = 0
                while prefix < len(branches) and behavior[branches[prefix].findtext('id')]['type']=='con': prefix += 1
                conditions = [describe(n.findtext('id')) for n in branches[:prefix]]
                actions = [describe(n.findtext('id')) for n in branches[prefix:]]
                attacks.append(node('；'.join(conditions) or '进入攻击分支', ' → '.join(actions),
                    [ai_ev, hp_code, cd_code, sequence_code, *behavior_ev]))
        routine = node('选择行动', '攻击条件成立且当前动作允许时进入相应分支；已开始的连招会继续执行。', [ai_ev, ai_code], [
            node('暂未锁定目标', '视野内没有可追踪对象，且没有仇恨目标时巡逻并等待。', [ai_ev, patrol_code]),
            node('已有目标', '满足追击条件时接近目标。', [ai_ev]), *attacks])
        judgments = []
        for slot, skill in skill_map.items():
            sid = skill['id']; sev = config_evidence('Skill.xml', sid, skill['name'])
            scope = skill['skillScope'].split(',')
            distance = f'水平偏移 {scope[0]}～{scope[1]}'
            if len(scope)==4: distance += f'，竖直偏移 {scope[2]}～{scope[3]}'
            text = (f'出招条件：目标进入配置范围（{distance}，游戏坐标）。'
                    '这是 AI 选招范围，实际受击还要检查动画与碰撞。')
            detail = node('命中要点', f'本技能配置命中上限 {skill["attMax"]} 次，重复命中间隔 {skill["attInterval"]} 毫秒；'
                '实际次数取决于接触、子技能及战斗状态，不能据此保证总伤害。', [sev, hit_code])
            judgments.append(node(skill_name(slot), text, [sev, range_code], [detail]))

        enemy_file = 'viewers/entitys/man/enemys/E'+mid+'.as'
        if mid == '3625':
            ev = code_evidence(enemy_file, 'advanceTime / transPos / skill1', ['currentFrame >= 40', 'direction * -50'])
            judgments[0]['children'].append(node('瞬移判定', '技能一动画到第 40 帧后调整到仇恨目标附近的横向位置；落点受场景边界限制，每次施放只触发一次。', [ev]))
        elif mid == '3626':
            ev = code_evidence(enemy_file, 'toAttack', ['param1 == 72', 'position.x,400', 'position.x,485'])
            judgments[1]['children'].append(node('抬高后砸落', '技能二起手先调整自身高度，再进入后续动作；砸地命中仍看动画与碰撞。', [ev]))
            judgments[2]['children'].append(node('剑阵起手', '技能三起手会调整自身高度。配置文字说明剑阵内会造成伤害并限制技能；持续时间本样板未核定。', [ev, 'skill-2166400']))
        elif mid == '3627':
            ev = code_evidence(enemy_file, 'toAttack / advanceTime', ['_fogFrame >= 10000', 'currentFrame >= 8', 'createBuff([8013001]'])
            judgments[0]['children'].append(node('隐身与迷雾', '地图有对应迷雾图层时，技能一显示迷雾并隐藏剑身；计时结束后恢复显示。隐藏外观不代表无敌。', [ev]))
            judgments[2]['children'].append(node('回血启动', '技能三动画到第 8 帧后创建回血效果；离开该动作时移除。本样板不承诺打断窗口的秒数。', [ev]))
        elif mid == '3184':
            ev = code_evidence(enemy_file, 'advanceTime / addeffect3_1 / checkZhaoHuan', ['currentFrame >= 32', 'currentFrame >= 20', 'currentFrame >= 44', 'summonCoord.x * -1'])
            judgments[1]['children'].append(node('水弹生成', '技能二动画到第 32 帧后生成额外水弹实体；水弹与本体应分别看待。', [ev]))
            judgments[2]['children'].append(node('巨浪移动', '技能三动画到第 20 帧后生成巨浪实体，读取当时目标的位置来计算横向速度。', [ev]))
            death = fields(skills[monster['dieSkill']])
            dev = config_evidence('Skill.xml', death['id'], death['name'])
            if death['summonMonster'] != '3185': raise ValueError('Unreviewed water-spirit split')
            routine['children'].append(node('死亡 → 水滴分裂', '配置死亡技能时，切换到分裂动作；到第 44 帧后在两侧召唤较小水灵。普通战斗行为树随之结束。', [monster_ev, dev, ev]))
        elif mid == '3620':
            judgments[2]['children'].append(node('蓄力横扫', '配置文字说明先蓄力约 1 秒再横扫；该描述未用动画资源或实战重新计时。', ['skill-2165300']))
        roots = [routine, node('招式与判定', '展开查看各技能的出招条件和命中要点。', [monster_ev], judgments)]
        profiles.append(dict(entry_id=entry_id, monster_ids=[mid],
            scope='主线普通难度 · '+('不周山' if mid!='3184' else '巨水灵'),
            version='配置 2026-10-19 · 单机代码 '+revision[:7]+'（历史快照）',
            summary='离线静态整理，未实战复核；省略重复动作。旧图鉴名称与配置名称分别保留。动画帧未换算为秒，未分析完整逐帧判定区域。', roots=roots))
    return dict(version=1, checked_on='2026-11-09', profiles=profiles, evidence=list(evidence.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'exports', 'singleplayer-swf', 'catalog', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    data = build(args.assets, args.exports, args.singleplayer_swf,
                 json.loads(args.catalog.read_text(encoding='utf8')))
    encoded = (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode('utf8')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix=args.output.name+'.', suffix='.tmp', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_bytes(encoded)
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Prepared {len(data["profiles"])} Boss behavior trees; {len(data["evidence"])} evidence records')


if __name__ == '__main__':
    main()
