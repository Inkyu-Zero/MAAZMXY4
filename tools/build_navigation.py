"""Compile shared navigation templates into MaaFramework recognition/actions.

Inheritance is resolved here, not by a second runtime interpreter. A logical
click is one And recognition on one image followed by one native action.
"""
from copy import deepcopy
from collections import Counter
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')


def compile_library(bank):
    nodes = {}
    for name, recognition in bank['recognizers'].items():
        nodes[name] = dict(deepcopy(bank['templates']['read']), recognition=recognition)
    for ident, screen in bank['screens'].items():
        parts = list(screen['all_of'])
        # Cheap fixed ROI colour tests reject an unrelated panel before its
        # template match. The source colour calibration remains unchanged.
        parts.sort(key=lambda name: bank['recognizers'].get(name, {}).get('type') != 'ColorMatch')
        nodes['Nav_See_' + ident] = dict(deepcopy(bank['templates']['read']),
            recognition=dict(type='And', param=dict(all_of=parts)))
    for name, action in bank['actions'].items():
        nodes[name] = dict(deepcopy(bank['templates'][action['base']]),
            recognition=dict(type='And', param=dict(all_of=action['all_of'], box_index=action['box_index'])))
        if 'post_delay' in action:
            nodes[name]['post_delay'] = action['post_delay']
    nodes.update(deepcopy(bank.get('nodes', {})))
    # Province landmarks are boolean guards, never input coordinates. Or stops
    # after the first matching landmark while retaining every source template,
    # ROI and per-template threshold. Do not apply this to ranked click targets.
    for name in bank.get('boolean_template_nodes', []):
        node = nodes[name]
        recognition = node['recognition']
        params = recognition.get('param', {})
        templates = params.get('template')
        if (recognition.get('type') != 'TemplateMatch' or not isinstance(templates, list)
                or not templates or params.get('index', 0) != 0
                or node.get('action', {}).get('type') != 'DoNothing'):
            raise ValueError('Boolean template guard must not select a ranked input target: ' + name)
        alternatives = []
        for index, template in enumerate(templates):
            part = deepcopy(params)
            part['template'] = template
            thresholds = part.get('threshold')
            if isinstance(thresholds, list):
                if not thresholds:
                    raise ValueError('Empty template thresholds: ' + name)
                part['threshold'] = thresholds[min(index, len(thresholds) - 1)]
            alternatives.append(dict(recognition=dict(type='TemplateMatch', param=part)))
        node['recognition'] = dict(type='Or', param=dict(any_of=alternatives))
    # Unused source probes stay in the source bank, not in the loaded pipeline.
    used = set(bank.get('public_recognizers', []))
    used.update('Nav_See_' + ident for ident in bank['screens'])
    used.update(bank['actions'])
    used.update(bank.get('nodes', {}))

    def references(value):
        if isinstance(value, str) and value in nodes:
            yield value
        elif isinstance(value, dict):
            for item in value.values(): yield from references(item)
        elif isinstance(value, list):
            for item in value: yield from references(item)

    pending = list(used)
    while pending:
        for name in references(nodes[pending.pop()]):
            if name not in used:
                used.add(name)
                pending.append(name)
    nodes = {name: node for name, node in nodes.items() if name in used}
    counts = Counter(name for value in nodes.values() for name in references(value))
    inline = {name for name in bank['recognizers'] if counts[name] == 1
              and name not in bank.get('public_recognizers', [])}

    def fold(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('all_of', 'any_of'):
                    value[key] = [dict(sub_name=part, recognition=deepcopy(nodes[part]['recognition']))
                                  if isinstance(part, str) and part in inline else part for part in item]
                fold(value[key])
        elif isinstance(value, list):
            for item in value: fold(item)
    for value in nodes.values(): fold(value)
    # A source-only leaf does not need its own editor/runtime node. Repeated
    # predicates remain named references, preserving actual configuration reuse.
    return {name: node for name, node in nodes.items() if name not in inline}


def build(root=ROOT):
    bank = json.loads((root / 'tools/navigation_library.json').read_text(encoding='utf8'))
    from mainline_tasks import stages, names
    bank['routines']['stages'].update({names(stage)['target']:stage['town'] for stage in stages(root)})
    nodes = compile_library(bank)
    write(root / 'assets/resource/base/pipeline/navigation.json', nodes)
    write(root / 'agent/data/navigation/routines.json', bank['routines'])
    return nodes


if __name__ == '__main__':
    print('Compiled', len(build()), 'shared navigation nodes')
