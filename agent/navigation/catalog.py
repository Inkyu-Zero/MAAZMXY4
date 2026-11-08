"""Bind a task to authored MAA-style routines, without route planning."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
from .stage_view import has_standard_view

DATA = Path(__file__).resolve().parents[1] / 'data/navigation'


@lru_cache(maxsize=1)
def library():
    return json.loads((DATA / 'routines.json').read_text(encoding='utf8'))


@lru_cache(maxsize=1)
def bounty_descriptions():
    return json.loads((DATA / 'bounty-descriptions.json').read_text(encoding='utf8'))


def task_routine(context, parameters, *, targets=None):
    config = parameters.get('navigation', {})
    kind = config.get('kind')
    if kind not in (None, 'stage', 'bounty', 'screen'):
        raise ValueError('未知的任务导航类型。')
    result = deepcopy(library())
    result['kind'] = kind
    result['targets'] = list(targets or (['task_destination'] if kind in ('stage', 'bounty')
                                       else [config.get('screen', 'map')]))
    result['target_node'] = config.get('target_node')
    result['destination'] = config.get('screen', 'map')
    definitions = {}
    if kind in ('stage', 'bounty'):
        name = result['target_node']
        definition = context.get_node_data(name) if isinstance(name, str) else None
        if not isinstance(definition, dict) or not isinstance(definition.get('recognition'), dict):
            raise ValueError('任务导航目标缺少识别定义。')
        # Named references preserve instance overrides; normalized compounds
        # returned by get_node_data cannot be copied back as source JSON.
        parts = [name]
        definitions['Nav_TaskTarget'] = dict(recognition=dict(type='And', param=dict(all_of=parts)),
            action=dict(type='DoNothing'), next=[], post_delay=0)
        result['screens']['task_destination'] = dict(detector='Nav_TaskTarget', label=config.get('label', '本任务目标'))
        if kind == 'stage':
            result['province'] = result['stages'].get(name)
            result['standard_view'] = has_standard_view(name)
            result['screens']['task_destination']['detector'] = name
    if kind == 'bounty':
        aliases = config.get('title_aliases', [])
        if not isinstance(aliases, list):
            raise ValueError('悬赏任务别名必须是名称列表。')
        titles = [config.get('title'), *aliases]
        if any(not isinstance(title, str) or not title or len(title) > 100 for title in titles):
            raise ValueError('悬赏导航需要明确的任务名称。')
        expected = ['^' + re.escape(title) + '$' for title in titles]
        # Keep the existing node ID for shared references and saved profiles.
        # Detail identity comes only from the description body; title OCR can
        # merge with the first body line. Row selection still uses the title.
        descriptions = bounty_descriptions()
        phrases = descriptions['tasks'].get(config.get('title'))
        if not phrases:
            raise ValueError('悬赏任务缺少描述段落识别配置。')
        checks = [dict(recognition=dict(type='OCR', param=dict(
            expected=[r'\s*'.join('[盗盜]' if char == '盗' else re.escape(char) for char in phrase)],
            roi=deepcopy(descriptions['roi'])))) for phrase in phrases]
        definitions['Nav_BountyDetailTitle'] = dict(recognition=dict(type='And',
            param=dict(all_of=checks)),
            action=dict(type='DoNothing'), next=[], post_delay=0)
        definitions['Nav_ChooseBounty'] = dict(recognition=dict(type='And', param=dict(all_of=[
            dict(recognition=dict(type='OCR', param=dict(expected=expected, roi=[180,175,320,370]))),
            'Nav_AcceptedBounties', 'Result_NoRevivePrompt'], box_index=0)),
            action=dict(type='Click', param=dict(target=True)), next=[], post_delay=350, pre_delay=0)
    if definitions and not context.override_pipeline(definitions):
        raise ValueError('Maa 拒绝导航目标定义。')
    return result
