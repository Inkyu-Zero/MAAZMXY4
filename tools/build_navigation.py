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
    nodes = compile_library(bank)
    write(root / 'assets/resource/base/pipeline/navigation.json', nodes)
    write(root / 'agent/data/navigation/routines.json', bank['routines'])
    return nodes


if __name__ == '__main__':
    print('Compiled', len(build()), 'shared navigation nodes')
