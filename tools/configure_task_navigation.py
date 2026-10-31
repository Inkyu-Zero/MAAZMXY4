"""Bind the project's existing script tasks to shared native navigation/results.

Task names/entries and binding tokens remain unchanged. The MFA queue keeps its
existing round ownership; retries within a failed round never visit max_hit.
"""
from copy import deepcopy
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def custom(phase, **values):
    return dict(type='Custom', param=dict(custom_action='script_task_state',
        custom_action_param=dict(phase=phase, **values)))


def node(phase, *, next=(), **values):
    return dict(recognition=dict(type='DirectHit'), action=custom(phase, **values),
                next=list(next), timeout=10000, post_delay=0)


def write(path, value):
    indent=4 if path.name=='interface.json' else 2
    path.write_text(json.dumps(value, ensure_ascii=False, indent=indent) + '\n', encoding='utf8', newline='\n')


def guard_modal(node):
    """Preserve the action's original box while blocking foreground dialogs."""
    recognition = node['recognition']
    if recognition['type'] != 'And':
        recognition = dict(type='And', param=dict(all_of=[dict(recognition=recognition)], box_index=0))
        node['recognition'] = recognition
    parts = recognition['param']['all_of']
    if 'Result_NoRevivePrompt' not in parts:
        parts.append('Result_NoRevivePrompt')


def confirmed_outcomes(graph, prefix, parameters, normal_return, outcomes):
    guard_modal(graph[parameters['navigation']['target_node']])
    # Cleanup can itself finish a recorded Return Map mouse press. Keep the
    # observed result even when its screen has disappeared before this branch.
    parameters['result_guard'].update(failed_next=[prefix+'已确认战斗失败'],
                                     succeeded_next=[prefix+'已确认战斗成功'],
                                     revive_next=[prefix+'拒绝还魂丹',prefix+'等待拒绝复活',prefix+'战斗失败'])
    decline=prefix+'拒绝还魂丹'
    pending=prefix+'等待拒绝复活'
    graph[decline]=deepcopy(outcomes['Result_DeclineRevive'])
    # Only the subsequent recognized defeat marks this round as failed. A
    # disappearing prompt, failed click or unknown screen never counts success.
    graph[decline].update(next=[prefix+'战斗失败'],timeout=15000,on_error=[prefix+'失败'])
    # The caption can be visible before its cancel button settles. Hold this
    # branch so settings/return controls behind the modal cannot be selected.
    graph[pending]=deepcopy(outcomes['Result_RevivePrompt'])
    # The game cancels after ten seconds. A layout miss must leave time for
    # that countdown and the result animation, while still requiring an
    # actual defeat before this round becomes eligible for retry.
    graph[pending].update(next=[decline,prefix+'战斗失败'],timeout=15000,on_error=[prefix+'失败'])
    failure_next=[decline,pending,prefix+'失败返回地图',prefix+'失败返回确认']
    graph[prefix+'已确认战斗失败']=node('battle_failure',next=failure_next)
    graph[prefix+'已确认战斗失败']['on_error']=[prefix+'失败']
    graph[prefix+'已确认战斗成功']=dict(recognition=dict(type='DirectHit'),action=dict(type='DoNothing'),
        next=[prefix+'成功返回地图',normal_return],timeout=15000,post_delay=0,on_error=[prefix+'失败'])
    graph[prefix+'战斗失败']['next']=failure_next
    graph[prefix+'战斗失败']['recognition']=dict(type='And',param=dict(all_of=[
        dict(recognition=deepcopy(outcomes['Result_Failed']['recognition'])), 'Result_NoRevivePrompt']))
    for suffix,shared in [('失败返回地图','Result_FailedReturn'),('成功返回地图','Result_SucceededReturn')]:
        graph[prefix+suffix]['recognition']=deepcopy(outcomes[shared]['recognition'])
    # A prompt can also appear after the recording's final action, before the
    # native settings/return branch. Preserve blocking dialogs ahead of it.
    routes=list(parameters.get('routes',{}).values())
    routes += [graph[prefix+suffix]['next'] for suffix in ('点击设置','退出结果复核') if prefix+suffix in graph]
    for route in routes:
        if decline not in route:
            index=1 if route and route[0]==prefix+'阻断提示' else 0
            route.insert(index,decline)
        if pending not in route:route.insert(route.index(decline)+1,pending)
    route_node=prefix+'回放后分流'
    if route_node in graph:
        graph[route_node]['next']=deepcopy(parameters['routes'][parameters['exit_mode']])
    if prefix+'点击设置' in graph:
        guard_modal(graph[prefix+'点击设置'])
        # Check the foreground immediately before input; a two-second delay
        # after recognition could let a revival prompt replace the screen.
        graph[prefix+'点击设置']['pre_delay'] = 0
    if prefix+'返回地图' in graph:
        graph[prefix+'返回地图']['recognition'] = dict(type='And', param=dict(all_of=['Nav_SettingsReturn']))


def describe_bounty(task, parameters):
    navigation=parameters['navigation']
    if navigation.get('kind')!='bounty': return
    title=navigation['title']
    task['description']=(f'请先手动接取「{title}」。可从带“任务”按钮的地图、任务面板或已接悬赏详情开始；'
        '任务核对目标名称、已接受状态及“前往副本”后进入。\n'
        '在脚本库为本任务绑定从副本内开始的录制；包含返回地图请选择“是”，仅战斗请选择“否”。\n'
        '识别挑战失败后返回地图，失败不计次；“失败自动重试”默认关闭，启用后按每轮设置的上限重试。')


def guard_stage_entry(graph, prefix, parameters):
    if parameters.get('navigation', {}).get('kind') != 'stage':
        return
    # A loading delay or changed highlight must never become a blind map click.
    fallback = prefix + '点击场外'
    for definition in graph.values():
        if fallback in definition.get('on_error', []):
            definition['on_error'] = [prefix + '失败']
    if fallback in graph:
        graph[fallback] = dict(recognition=dict(type='DirectHit'),
            action=dict(type='DoNothing'), next=[prefix+'失败'], post_delay=0)


def configure(root=ROOT):
    interface_path = root / 'assets/interface.json'
    interface = json.loads(interface_path.read_text(encoding='utf-8-sig'))
    folder = root / 'assets/resource/base/pipeline'
    outcomes = json.loads((folder / 'battle_outcomes.json').read_text(encoding='utf8'))
    for name in ('Nav_OpenQuest', 'Nav_SelectBountyTab', 'Nav_CloseQuest', 'Nav_ResumeTaskMap'):
        guard_modal(outcomes[name])
    outcomes.pop('脚本任务设置', None)
    write(folder / 'battle_outcomes.json', outcomes)
    settings_path = folder / '脚本任务.json'
    settings = json.loads(settings_path.read_text(encoding='utf8'))
    settings['脚本任务设置']['action']['param']['custom_action_param']['failure_retry'] = dict(enabled=False, max_retries=3)
    write(settings_path, settings)
    changed = []
    for task in interface['task']:
        path = folder / (task['name'] + '.json')
        if not path.is_file():
            continue
        graph = json.loads(path.read_text(encoding='utf-8-sig'))
        play = next((key for key, value in graph.items()
                     if value.get('action', {}).get('param', {}).get('custom_action') == 'battle_play'), None)
        if play is None:
            continue
        prefix, entry = task['name'] + '-', task['entry']
        if prefix+'准备本轮' in graph or graph[entry].get('action', {}).get('param', {}).get('custom_action_param', {}).get('navigation'):
            prepare=prefix+'准备本轮' if prefix+'准备本轮' in graph else entry
            parameters=graph[prepare]['action']['param']['custom_action_param']
            normal_return=prefix+('等待返回' if parameters.get('execution') else '脚本返回确认')
            confirmed_outcomes(graph,prefix,parameters,normal_return,outcomes)
            guard_stage_entry(graph,prefix,parameters)
            describe_bounty(task,parameters)
            write(path,graph)
            changed.append(task['name'])
            continue
        is_profile = 'execution' in graph[entry].get('action', {}).get('param', {}).get('custom_action_param', {})
        bounty = re.fullmatch(r'悬赏（(.+)）', task['name'])
        target = prefix+'点击前往副本' if bounty else prefix+'关卡入口'
        navigation = dict(kind='bounty' if bounty else 'stage', target_node=target, label=task['name'])
        if bounty:
            navigation['title'] = bounty[1]
            body = target
        else:
            graph[target] = deepcopy(graph[entry])
            graph[target]['on_error'] = [prefix+'失败']
            # Preserve the old public entry and therefore the stable task ID.
            graph[entry] = dict(recognition=dict(type='DirectHit'), action=dict(type='DoNothing'),
                next=[prefix+'刷取次数'], timeout=10000, post_delay=0)
            body = target
        if not is_profile and entry != prefix+'刷取次数':
            # Maa does not recognize a public entry before executing its
            # action. Defer all clicks until navigation has located a screen.
            graph[entry] = dict(recognition=dict(type='DirectHit'), action=dict(type='DoNothing'),
                next=[prefix+'刷取次数'], timeout=10000, post_delay=0)
        navigate = prefix+'通用导航'
        prepare = entry if is_profile else prefix+'准备本轮'
        guard = dict(failed_next=[prefix+'战斗失败'], succeeded_next=[prefix+'成功返回地图'])
        if is_profile:
            parameters = graph[entry]['action']['param']['custom_action_param']
            graph[entry]['next'] = [navigate]
        else:
            file = graph[play]['action']['param']['custom_action_param']['file']
            parameters = dict(phase='begin', file=file, playback_node=play, exit_mode='battle_only')
            graph[prepare] = node('begin')
            graph[prepare]['action']['param']['custom_action_param'] = parameters
            graph[prepare]['next'] = [navigate]
            graph[prefix+'刷取次数']['next'] = [prepare]
        parameters.update(navigation=navigation, navigation_entry=navigate, result_guard=guard,
            settings_node='脚本任务设置', failure_retry=dict(enabled=False, max_retries=3),
            route_node=prefix+'回放后分流')
        normal_return = prefix+'等待返回' if is_profile else prefix+'脚本返回确认'
        guards = [prefix+'阻断提示'] if prefix+'阻断提示' in graph else []
        parameters['routes'] = {
            'recorded_return': [*guards, prefix+'战斗失败', prefix+'成功返回地图', normal_return],
            'battle_only': [*guards, prefix+'战斗失败', prefix+'成功返回地图', prefix+'点击设置'],
        }
        graph[navigate] = node('navigate', next=[body])
        graph[navigate]['on_error'] = [prefix+'失败']
        graph[prefix+'回放后分流'] = dict(recognition=dict(type='DirectHit'), action=dict(type='DoNothing'),
            next=parameters['routes'][parameters['exit_mode']], timeout=15000, post_delay=0,
            on_error=[prefix+'失败'])
        graph[play]['next'] = [prefix+'回放后分流']
        graph[play]['post_delay'] = 0
        graph[play]['on_error'] = [prefix+'失败']
        graph[prefix+'战斗失败'] = deepcopy(outcomes['Result_Failed'])
        graph[prefix+'战斗失败'].update(action=custom('battle_failure'), next=[prefix+'失败返回地图'])
        graph[prefix+'失败返回地图'] = deepcopy(outcomes['Result_FailedReturn'])
        graph[prefix+'失败返回地图']['next'] = [prefix+'失败返回确认']
        graph[prefix+'成功返回地图'] = deepcopy(outcomes['Result_SucceededReturn'])
        graph[prefix+'成功返回地图']['next'] = [normal_return]
        return_recognition = (dict(type='TemplateMatch', param=dict(template='任务.png')) if bounty else
                              deepcopy(graph[prefix+'脚本返回确认']['recognition']))
        graph[prefix+'失败返回确认'] = dict(recognition=return_recognition,
            action=custom('retry_after_failure'), next=[], timeout=15000, post_delay=0)
        if not is_profile:
            graph[prefix+'本轮成功'] = node('success', next=[prefix+'刷取次数', '停止运行'])
            graph[prefix+'脚本返回确认']['next'] = [prefix+'本轮成功']
            graph[prefix+'返回地图']['next'] = [prefix+'脚本返回确认']
            graph[prefix+'失败'] = node('failure', reason='任务导航、回放或返回画面未确认，本轮未计次。')
        # A result screen can appear between the final recorded event and exit.
        # Always choose its dedicated branch before the generic settings exit.
        if prefix+'点击设置' in graph:
            graph[prefix+'点击设置']['next'] = [*guards, prefix+'战斗失败', prefix+'成功返回地图', prefix+'返回地图']
            graph[prefix+'点击设置']['on_error'] = [prefix+'退出结果复核']
            graph[prefix+'退出结果复核'] = dict(recognition=dict(type='DirectHit'), action=dict(type='DoNothing'),
                next=[*guards, prefix+'战斗失败', prefix+'成功返回地图'], timeout=3000, post_delay=0,
                on_error=[prefix+'失败'])
        for suffix in ('失败返回地图', '成功返回地图'):
            graph[prefix+suffix]['on_error'] = [prefix+'失败']
        confirmed_outcomes(graph,prefix,parameters,normal_return,outcomes)
        # Retired navigation retries must not consume the brush-count quota.
        for name, definition in graph.items():
            if name.endswith('点击任务') and name != entry:
                definition['on_error'] = [navigate]
        task['option'].append('失败自动重试')
        task['description'] += '\n识别挑战失败后自动返回地图，失败轮次不计数；开启“失败自动重试”后在本轮预算内重新导航并回放。'
        if bounty:
            describe_bounty(task,parameters)
        guard_stage_entry(graph,prefix,parameters)
        write(path, graph)
        changed.append(task['name'])
    interface['option']['失败自动重试'] = dict(type='select', label='战败后在游戏内重试', default_case='关闭',
        description='确认战败并返回地图后重新进关，不重启盒子。重试用尽后，才按游戏管理中的失败重启开关处理。挂机模式下始终开启且不限次数。',
        cases=[dict(name='关闭', pipeline_override={'脚本任务设置': {'action': {'param': {'custom_action_param': {'failure_retry': {'enabled': False}}}}}}),
               dict(name='启用', option=['失败重试设置'], pipeline_override={'脚本任务设置': {'action': {'param': {'custom_action_param': {'failure_retry': {'enabled': True}}}}}})])
    interface['option']['失败重试设置'] = dict(type='input', label='游戏内重试次数',
        description='每轮允许追加的游戏内重试次数，失败不计入完成轮数。挂机时不受此上限影响。',
        inputs=[dict(name='失败重试次数', label='每轮最多重试', pipeline_type='int', default='3',
                     verify='^([1-9]|1[0-9]|20)$', pattern_msg='请输入1至20；不重试请选择关闭')],
        pipeline_override={'脚本任务设置': {'action': {'param': {'custom_action_param': {'failure_retry': {'max_retries': '{失败重试次数}'}}}}}})
    marker='\n\n游戏管理：'
    interface['option']['挂机模式'] = dict(type='select', label='挂机模式', default_case='关闭',
        description='开启后无限刷取、无限战败重试，直到手动停止。原次数与重试设置保留；验证码、未知界面、存档不符或脚本损坏仍安全停止。',
        cases=[dict(name=name, pipeline_override={'脚本任务设置': {'action': {'param': {'custom_action_param': {'afk_mode': enabled}}}}})
               for name,enabled in [('关闭',False),('启用',True)]])
    for task in interface['task']:
        if task['name'] not in changed: continue
        task['option'] = ['挂机模式'] + [name for name in task['option'] if name != '挂机模式']
        task['option']=[name for name in task.get('option',[]) if name!='自动恢复']
        task['description']=task.get('description','').split(marker)[0]+marker+(
            '在右侧设置现有造梦盒子程序、历史账号名称和已有存档角色。可分别开启任务失败后重启、'
            '内存超过1.8 GB重启及按分钟间隔重启；所有状态显示在日志。\n'
            '内存与定时重启在本轮结束、确认返回后执行；重启后核对账号和存档，再继续剩余轮次。'
            '已完成次数保留，失败轮次不计次。优先执行本任务的失败重试；重试耗尽或任务确认无法继续时，才按已开启的失败重启设置重启并续跑。仅勾选挂机模式不会启用重启。\n'
            '验证码、存档不符、脚本变更及人工停止会停止自动恢复；未启用游戏管理时保持原任务流程。')
    write(interface_path, interface)
    print('已接入公共导航与失败重试：' + '、'.join(changed))


if __name__ == '__main__':
    configure()
