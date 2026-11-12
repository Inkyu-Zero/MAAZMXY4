"""Compile add-only mainline profiles into the existing shared round lifecycle."""
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# South/East Province maps have no normal/nightmare switch. A mode-button
# requirement there blocks a correctly identified, ready-to-enter stage.
MODE_SWITCH_TOWNS = {'town3', 'town4', 'town5'}


def stages(root=ROOT):
    return json.loads((root/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf-8'))['stages']


def names(stage):
    prefix = f'Mainline_{stage["id"]:03d}'
    return dict(name='主线（'+stage['name']+'）', entry=prefix, target=prefix+'_Target',
                repeat=prefix+'_Repeat', play=prefix+'_Play')


def build(root=ROOT):
    from build_script_tasks import parameters, round_node, play_node, node, custom, write, ENTRY_PRESS_MS
    from task_catalog import task_key
    interface_path = root/'assets/interface.json'
    interface = json.loads(interface_path.read_text(encoding='utf-8'))
    interface['task'] = [task for task in interface['task'] if not task['entry'].startswith('Mainline_')]
    counters = interface['option']['刷取次数']['pipeline_override']
    for key in list(counters):
        if key.startswith('Mainline_'): del counters[key]
    graph = {}
    scroll_path = root/'agent/data/navigation/stage-scroll.json'
    scroll = json.loads(scroll_path.read_text(encoding='utf-8'))
    scroll['maps'] = [item for item in scroll['maps'] if not item['target'].startswith('Mainline_')]
    for stage in stages(root):
        spec = names(stage)
        binding = task_key(spec['name'],spec['entry'])
        params = parameters(spec['name'],spec['entry'],binding,spec['play'],spec['target'],
                            kind='stage',repeat=spec['repeat'])
        # Only the requested stage is active. Identity/mode/overlay guards and
        # icon use the same frame, and box_index keeps input on the icon.
        target = dict(type='And',param=dict(all_of=[
            dict(recognition=deepcopy(stage['recognition'])),
            'Nav_Province_'+stage['town'],
            *(['GameManagement_NormalMap'] if stage['town'] in MODE_SWITCH_TOWNS else []),
            'Nav_ClearMap'],box_index=0))
        graph[spec['entry']] = round_node(spec['entry'],params,True)
        graph[spec['repeat']] = node(action=custom('loop_count',label=spec['name']),max_hit=1)
        graph[spec['target']] = node(target,dict(type='LongPress',param=dict(duration=ENTRY_PRESS_MS)))
        graph[spec['target']]['post_delay'] = 1000
        graph[spec['play']] = play_node(binding)
        interface['task'].append(dict(name=spec['name'],label=stage['province']+' · '+stage['name'],
            entry=spec['entry'],default_add=False,group=[stage['province']],option=['挂机模式','盒子与脚本倍速','刷取次数','战斗录制内容','失败自动重试'],
            description='从任务列表“＋”添加本关卡，并在脚本库绑定对应战斗录制。普通模式下自动导航、核对入口，进关后仅确认通用战斗界面，回放后确认返回再计次。\n录制包含返回地图请选择“是”；仅战斗请选择“否”。未绑定、未解锁或界面无法确认时停止。战败重试、挂机及游戏管理沿用公共流程。'))
        counters[spec['repeat']] = dict(max_hit='{刷取次数}')
        if stage['town'] == 'town5':
            left = stage['endpoint']=='left'
            scroll['maps'].append(dict(task=spec['name'],target=spec['target'],town='town5',axis='x',
                max_steps=2,drag_node='GameManagement_town5_DragBack' if left else 'GameManagement_town5_Drag',
                end_x=1000 if left else 20,view=stage['endpoint']))
    write(root/'assets/resource/base/pipeline/mainline.json',graph)
    write(interface_path,interface)
    write(scroll_path,scroll)
    return graph


if __name__=='__main__':
    import sys
    sys.path.insert(0,str(ROOT/'agent'))
    print('Compiled',len(build()),'mainline profile nodes')
