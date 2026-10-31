"""Bind a task destination to the shared, packaged navigation graph."""
from copy import deepcopy
import json
from pathlib import Path
import re


DATA = Path(__file__).resolve().parents[1] / 'data/navigation'


def task_graph(context, parameters, *, targets=None):
    graph = json.loads((DATA / 'graph.json').read_text(encoding='utf8'))
    nodes, edges = graph['nodes'], graph['edges']
    maps = ['task_map', 'map_a', 'map_b']
    for node in nodes:
        if node['id'] != 'task_map' and 'task_map' not in node.setdefault('obscures', []):
            node['obscures'].append('task_map')
        if node['id'] == 'settings_menu':
            node['recognition'] = ['Nav_SettingsMenu']
    for item in edges:
        if item['source'] == 'settings_menu' and item['target'] == 'task_map':
            item['entry'] = 'Nav_SettingsContinue'
    nodes += [
        dict(id='quest_panel', label='任务面板', recognition=['Nav_QuestPanel'], obscures=list(maps)),
        dict(id='accepted_bounties', label='已接悬赏任务', recognition=['Nav_AcceptedBounties'],
             obscures=['quest_panel', *maps]),
    ]
    definitions = {}
    config = parameters.get('navigation', {})
    kind = config.get('kind')
    if kind not in ('bounty', 'stage', None):
        raise ValueError('未知的任务导航类型。')
    if kind:
        name = config.get('target_node')
        definition = context.get_node_data(name) if isinstance(name, str) else None
        if not isinstance(definition, dict) or not isinstance(definition.get('recognition'), dict):
            raise ValueError('任务导航目标缺少识别定义。')
        # get_node_data() returns Maa's normalized compound subnodes, which
        # cannot be fed back as source JSON (they can become DirectHit).
        # run_recognition() only recognizes; it never executes this node's
        # action. Reusing its name also preserves the current task overrides.
        # Stage overlays are extended below; sharing this list with maps or
        # another state would make the destination obscure itself.
        obscures = ['accepted_bounties', 'quest_panel', *maps] if kind == 'bounty' else list(maps)
        nodes.append(dict(id='task_destination', label=config.get('label', '本任务目标'),
            recognition=[name], obscures=obscures))
        if kind == 'stage':
            for node in nodes:
                if node['id'] not in maps + ['task_destination'] and set(node.get('obscures', [])) & set(maps):
                    node.setdefault('obscures', []).append('task_destination')
    nodes.append(dict(id='settings_subpanel', label='声音、详细或伤害设置',
        recognition=['Nav_SettingsSubpanel'],
        obscures=[node['id'] for node in nodes if node['id'] != 'profile_sell']))
    for ident, label, detector in [('challenge_failed', '挑战失败', 'Result_Failed'),
                                    ('challenge_succeeded', '挑战成功', 'Result_Succeeded')]:
        nodes.append(dict(id=ident, label=label, recognition=[detector],
            obscures=[node['id'] for node in nodes if node['id'] not in ('challenge_failed', 'challenge_succeeded','profile_sell')]))
    normal_ids=[node['id'] for node in nodes if node['id'] not in ('challenge_failed','challenge_succeeded','profile_sell')]
    nodes.append(dict(id='paused_task_map',label='任务地图已暂停',recognition=['Nav_PausedTaskMap'],obscures=normal_ids))
    for node in nodes:
        if node['id'] in ('challenge_failed','challenge_succeeded'):
            node['obscures'].append('paused_task_map')
    nodes.append(dict(id='common_confirm',label='确认浮窗（需先处理）',recognition=['Nav_CommonConfirm'],
        obscures=[node['id'] for node in nodes if node['id']!='profile_sell']))
    for node in nodes:
        if node['id']=='profile_sell':node.setdefault('obscures',[]).append('common_confirm')
    nodes.append(dict(id='revive_prompt',label='还魂丹或免费复活确认',
        recognition=['Result_RevivePrompt'],obscures=[node['id'] for node in nodes]))

    def edge(source, target, entry, label):
        edges.append(dict(id=source+'__'+target, source=source, target=target, entry=entry,
            label=label, cost=1, timeout_ms=10000, max_attempts=2))

    for source in maps:
        edge(source, 'quest_panel', 'Nav_OpenQuest', '打开人物界面的任务面板')
    edge('settings_subpanel', 'settings_menu', 'Nav_CloseSettingsSubpanel', '关闭已确认的设置子窗口')
    edge('paused_task_map','task_map','Nav_ResumeTaskMap','恢复已确认的任务地图')
    edge('quest_panel', 'accepted_bounties', 'Nav_SelectBountyTab', '切换悬赏任务')
    for source in ('quest_panel', 'accepted_bounties'):
        edge(source, 'task_map', 'Nav_CloseQuest', '关闭已确认的任务面板')
    edge('challenge_failed', 'task_map', 'Result_FailedReturn', '从挑战失败返回地图')
    edge('challenge_succeeded', 'task_map', 'Result_SucceededReturn', '从挑战成功返回地图')
    edge('revive_prompt','challenge_failed','Result_DeclineRevive','拒绝复活并确认失败结果')
    if kind == 'bounty':
        title = config.get('title')
        if not isinstance(title, str) or not title or len(title) > 100:
            raise ValueError('悬赏导航需要明确的任务名称。')
        aliases = config.get('title_aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(value, str) or not value or len(value)>100 for value in aliases):
            raise ValueError('悬赏任务别名必须是明确的名称。')
        definitions['Nav_ChooseBounty'] = dict(recognition=dict(type='And', param=dict(all_of=[
            dict(recognition=dict(type='OCR', param=dict(expected=['^'+re.escape(value)+'$' for value in [title,*aliases]], roi=[180, 175, 320, 370]))),
            dict(recognition=dict(type='OCR', param=dict(expected=['悬赏任务'], roi=[180, 90, 500, 100]))),
            'Result_NoRevivePrompt',
        ], box_index=0)), action=dict(type='Click', param=dict(target=True)), next=[], timeout=10000, post_delay=350)
        edge('accepted_bounties', 'task_destination', 'Nav_ChooseBounty', '选择已接悬赏：'+title)
    elif kind == 'stage':
        # Closing an overlay can expose the destination directly, so the
        # planner needs that possible arrival in addition to the generic map.
        # The runtime always re-localizes: a wrong map never becomes success.
        # Do not open unrelated panels or the world just to manufacture a route
        # back to the same map when this stage's icon is absent.
        edges[:] = [item for item in edges if item['source'] not in maps]
        for item in list(edges):
            if item['target'] in maps and item['source'] != 'world':
                arrival = deepcopy(item)
                arrival.update(id=item['id']+'__stage_ready', target='task_destination',
                               label=item['label']+'，核对本关入口')
                edges.append(arrival)
    graph['targets'] = list(targets or (['task_destination'] if kind else maps))
    graph['abort'] = {'Nav_BlockedDialog': '验证码、登录或阻断提示未处理，导航已停止。'}
    if definitions and not context.override_pipeline(definitions):
        raise ValueError('Maa拒绝导航目标定义。')
    return graph
