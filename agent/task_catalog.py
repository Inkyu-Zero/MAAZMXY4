"""Discover tasks and script insertion points from this project's existing data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from script_library import read_binding_records, resolve_recording_reference
from script_studio.model import RecordingDocument, MAX_FILE_BYTES


def task_key(name, entry):
    return 'task-' + hashlib.sha256((name + '\0' + entry).encode('utf8')).hexdigest()[:32]


def read_document(path):
    # Same comment grammar as the repository's schema validator. Keep quoted
    # URLs and escape sequences intact while accepting resource JSONC files.
    source = path.read_text(encoding='utf-8-sig')
    # Published resources are ordinary JSON. Let the standard decoder handle
    # them directly; only JSONC needs the slower comment-aware character scan.
    # Do not cache mutable task/instance data: edits must be visible next call.
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        value = None
    else:
        if not isinstance(value, dict):
            raise ValueError('任务配置必须是对象：' + str(path))
        return value
    result, index, quoted, escaped = [], 0, False, False
    while index < len(source):
        char = source[index]
        if quoted:
            result.append(char)
            if escaped: escaped = False
            elif char == '\\': escaped = True
            elif char == '"': quoted = False
        elif char == '"':
            quoted = True
            result.append(char)
        elif source[index:index+2] == '//':
            index = source.find('\n', index)
            if index < 0: break
            result.append('\n')
        elif source[index:index+2] == '/*':
            end = source.find('*/', index+2)
            if end < 0: raise ValueError('未闭合JSON注释：' + str(path))
            result.append(' ')
            index = end + 1
        else:
            result.append(char)
        index += 1
    value = json.loads(''.join(result))
    if not isinstance(value, dict): raise ValueError('任务配置必须是对象：' + str(path))
    return value


def merge(base, overlay):
    result = deepcopy(base)
    for key, value in overlay.items():
        result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else deepcopy(value)
    return result


def _inside(root, value):
    path = (root / value.replace('{PROJECT_DIR}', str(root))).resolve()
    if not path.is_relative_to(root): raise ValueError('任务资源不能指向工程外部。')
    return path


def _configuration(root):
    path = next((root / name for name in ('interface.json', 'interface.jsonc', 'assets/interface.json') if (root / name).is_file()), None)
    if path is None: return {}, {}
    interface = read_document(path)
    imports = interface.get('import', [])
    for reference in [imports] if isinstance(imports, str) else imports:
        extra = read_document(_inside(root, reference))
        interface['task'] = [*interface.get('task', []), *extra.get('task', [])]
    nodes = {}
    for resource in interface.get('resource', []):
        paths = resource.get('path', [])
        for relative in [paths] if isinstance(paths, str) else paths:
            folder = _inside(root, relative) / 'pipeline'
            if not folder.is_dir() and path.parent.name == 'assets':
                folder = _inside(root, 'assets/' + relative.lstrip('./')) / 'pipeline'
            if folder.is_dir():
                for source in sorted(folder.rglob('*')):
                    if source.suffix in ('.json', '.jsonc'):
                        nodes.update(read_document(source))
    return interface, nodes


def node_links(nodes, node):
    """Resolve declared link targets for both discovery and resource installation."""
    for field in ('next', 'on_error'):
        links = node.get(field, [])
        if isinstance(links, (str, dict)): links = [links]
        for link in links:
            target = link.get('name') if isinstance(link, dict) else link
            if not isinstance(target, str): raise ValueError('Pipeline后续节点格式无效。')
            anchor = isinstance(link, dict) and link.get('anchor', False) or '[Anchor]' in target
            target = re.sub(r'^(?:\[[^\]]+\])+', '', target)
            if anchor:
                for node_name, definition in nodes.items():
                    anchors = definition.get('anchor', {})
                    if isinstance(anchors, str): anchors = [anchors]
                    if target in anchors:
                        resolved = anchors[target] if isinstance(anchors, dict) else node_name
                        if resolved: yield resolved
            else:
                yield target


def _playback_nodes(nodes, entry):
    pending, visited, matches = [entry], set(), []
    while pending:
        name = pending.pop()
        if name in visited or name not in nodes: continue
        visited.add(name)
        node = nodes[name]
        if _action_parameters(node).get('custom_action') == 'battle_play':
            matches.append(name)
        pending.extend(node_links(nodes, node))
    return sorted(matches)


def _action_parameters(node):
    action = node.get('action', {})
    return action.get('param', {}) if isinstance(action, dict) else node


def custom_parameters(node):
    """Normalize the object and legacy string forms accepted by battle_play."""
    value = _action_parameters(node).get('custom_action_param', {})
    if isinstance(value, str):
        try: parsed = json.loads(value)
        except ValueError: parsed = None
        return parsed if isinstance(parsed, dict) else {'file': value}
    return value if isinstance(value, dict) else {}


def task_catalog(root, bindings=None, *, overrides=None):
    root = Path(root).resolve()
    interface, nodes = _configuration(root)
    definitions = list(interface.get('task', []))
    for source in sorted((root / 'config/instances').glob('*.json')):
        definitions += read_document(source).get('TaskItems', [])
    if bindings is None: bindings = read_binding_records(root / 'recordings')
    tasks, seen = [], {}
    for definition in definitions:
        if not isinstance(definition, dict): continue
        name, entry = definition.get('name'), definition.get('entry')
        if not isinstance(name, str) or not isinstance(entry, str): continue
        if (name, entry) in seen and overrides is not None: continue
        graph = merge(nodes, definition.get('pipeline_override', {}) or {})
        if overrides is not None: graph = merge(graph, overrides)
        parameters = custom_parameters(graph.get(entry, {}))
        profile = None
        if isinstance(parameters, dict) and isinstance(parameters.get('execution'), dict):
            spec = parameters['execution']
            resume = spec.get('resume_entry')
            resume_parameters = custom_parameters(graph.get(resume, {}))
            profile = dict(entry=entry, resume_entry=resume, parameters=deepcopy(parameters),
                           resume_parameters=deepcopy(resume_parameters))
        ident = parameters.get('execution', {}).get('binding_id') if profile else None
        ident = ident or task_key(name, entry)
        playback = _playback_nodes(graph, entry)
        modes = ['pipeline', 'replace'] if playback else ['replace']
        binding = bindings.get(ident)
        management_profile = None
        # Discover one confirmed round from the existing graph. The host may
        # stop at this boundary for maintenance without resetting native quotas.
        pending, reachable = [entry], set()
        while pending:
            current = pending.pop()
            if current in reachable or current not in graph: continue
            reachable.add(current)
            pending.extend(node_links(graph, graph[current]))
        # 轮次边界由 begin/success 声明，不依赖可选的 navigation 策略。
        # 使用传统导航的任务也能声明游戏管理入口。
        begins = [n for n in reachable if custom_parameters(graph[n]).get('phase') == 'begin'
                  and _action_parameters(graph[n]).get('custom_action') == 'script_task_state']
        successes = [n for n in reachable if custom_parameters(graph[n]).get('phase') == 'success'
                     and _action_parameters(graph[n]).get('custom_action') == 'script_task_state']
        if len(begins) == 1 and len(successes) == 1:
            counters = [n for n in node_links(graph, graph[successes[0]]) if n in graph
                        and _action_parameters(graph[n]).get('custom_action') == 'loop_count']
            counter = counters[0] if len(counters) == 1 else None
            management_profile = dict(entry=begins[0], parameters=deepcopy(custom_parameters(graph[begins[0]])),
                success_node=successes[0], resume_entry='GameManagement_Resume',
                repeat_limit=graph[counter].get('max_hit', 1) if counter else 1,
                native_repeat=counter is not None)
        item = dict(id=ident, name=name, label=definition.get('label') or name,
                          entry=entry, playback_nodes=playback, modes=modes,
                          playback_parameters={node: deepcopy(custom_parameters(graph[node])) for node in playback},
                          playback_routes={node: deepcopy(custom_parameters(graph[node]).get('script_binding', {})) for node in playback},
                          binding=deepcopy(binding), profile=profile, management_profile=management_profile,
                          management_completion_only=_action_parameters(graph.get(entry,{})).get('custom_action')=='adventure_init')
        if (name, entry) in seen:
            # Library capabilities cover saved variants. Execution below always
            # uses the overrides supplied by the running instance, never another one.
            previous = seen[name, entry]
            previous['playback_nodes'] = sorted(set(previous['playback_nodes']) | set(playback))
            if playback: previous['modes'] = ['pipeline', 'replace']
        else:
            tasks.append(item)
            seen[name, entry] = item
    return tasks


def inspect_saved_recording(root, reference):
    path = resolve_recording_reference(Path(root) / 'recordings', reference)
    with path.open('rb') as stream: raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES: raise ValueError('脚本不能超过16MiB。')
    data = json.loads(raw.decode('utf-8-sig'))
    document = RecordingDocument.from_dict(data)
    document.to_dict(strict=True)
    if not data.get('actions'): raise ValueError('任务不能绑定空白脚本。')
    if document.diagnostics: raise ValueError('请先修复脚本中的操作诊断。')
    if min(document.base) != 720: raise ValueError('请先将脚本转换为本工程720坐标。')
    return dict(file=path.name, path=str(path), sha256=hashlib.sha256(raw).hexdigest(), base=list(document.base))


def execution_plan(root, name, entry, overrides=None, *, report_unbound=False):
    if overrides is not None and not isinstance(overrides, dict): raise ValueError('任务覆盖参数必须是对象。')
    if type(report_unbound) is not bool: raise ValueError('未绑定处理标志必须是布尔值。')
    task = next((item for item in task_catalog(root, overrides=overrides or {}) if item['name'] == name and item['entry'] == entry), None)
    if task is None: return dict(handled=False)
    binding = task['binding']
    requires_binding = any(str(parameters.get('file', '')).startswith('@task:')
                           for parameters in task['playback_parameters'].values())
    if not binding and (requires_binding or task['profile']):
        if report_unbound:
            return dict(task, handled=True, stop_reason='unbound_script',
                        message='任务“' + str(task['label']) + '”尚未绑定脚本，本次任务已结束。请在“录制与脚本”中绑定后再开始。')
        raise ValueError('请先在脚本库中为“' + str(task['label']) + '”绑定脚本。')
    if binding:
        if binding['mode'] not in task['modes']: raise ValueError('任务已没有脚本回放节点，请重新选择绑定方式。')
        task['recording'] = inspect_saved_recording(root, binding['file'])
    task['handled'] = bool(binding or task['profile'])
    return task
