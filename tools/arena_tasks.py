"""Arena is a navigation destination followed by the shared stage round."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(root=ROOT):
    from build_script_tasks import parameters, round_node, play_node, node, custom, write, ENTRY_PRESS_MS
    from task_catalog import task_key
    name, entry, target, play, repeat = '竞技场', 'Arena', 'Arena_Target', 'Arena_Play', 'Arena_Repeat'
    binding = task_key(name, entry)
    params = parameters(name, entry, binding, play, target, kind='arena', repeat=repeat)
    params['navigation']['screen'] = 'arena'
    params['round_flow']['require_victory'] = True
    positions = [(386 + 159*i, 273) for i in range(5)] + [(386, 490)]
    labels = [f'第 {i} 位普通对手' for i in range(1, 6)] + ['神秘竞技者']
    graph = {
        entry: round_node(entry, params, True),
        repeat: node(action=custom('loop_count', label=name), max_hit=1),
        target: node(dict(type='And', param=dict(all_of=['Nav_See_arena', 'Result_NoRevivePrompt'])),
                     dict(type='LongPress', param=dict(duration=ENTRY_PRESS_MS, target=[*positions[4], 1, 1]))),
        play: play_node(binding),
    }
    graph[target]['post_delay'] = 1000
    write(root/'assets/resource/base/pipeline/arena.json', graph)
    path = root/'assets/interface.json'
    interface = json.loads(path.read_text(encoding='utf8'))
    interface['task'] = [task for task in interface['task'] if task['entry'] != entry]
    interface['task'].append(dict(name=name, entry=entry, default_add=False,
        option=['竞技场对手', '挂机模式', '盒子与脚本倍速', '刷取次数', '失败自动重试'],
        description='默认挑战第 5 位普通对手，可改选第 1～5 位或神秘竞技者。请绑定仅包含战斗的脚本，胜利结算由任务返回。\n'
                    '点击头像后沿用普通关卡的进关与回放流程；确认挑战成功并返回对手列表后计次。'
                    '脚本结束时若仍在战斗，最多再观察 4 秒等待结算。战败按重试设置处理，未出现结算则保留当前界面并停止。'))
    interface['option']['竞技场对手'] = dict(type='select', default_case=labels[4], cases=[
        dict(name=label, pipeline_override={target:dict(action=dict(param=dict(target=[x, y, 1, 1])))})
        for label, (x, y) in zip(labels, positions)])
    counters = interface['option']['刷取次数']['pipeline_override']
    counters.pop(repeat, None)
    counters[repeat] = dict(max_hit='{刷取次数}')
    write(path, interface)
