"""Sync selected native task definitions and their resource dependencies, with atomic backup.

Defaults to a read-only plan for all canonical tasks. Use --task to select tasks.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from legacy_script_bindings import plan_legacy_bindings
from script_library import _bindings_state, publish_new_file
from task_catalog import _configuration, custom_parameters, node_links

SEPARATOR = '<|||>'
MAX_JSON_BYTES = 16 * 1024 * 1024


class SyncConflict(ValueError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    if raw is None:
        raise ValueError('缺少必需的任务配置JSON文件。')
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('任务配置不能超过16MiB。')
    return json.loads(raw.decode('utf-8-sig'), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('JSON包含非有限数值。')))


def _encode(value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf8')
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('任务配置不能超过16MiB。')
    return raw


def _same_json(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(right, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _safe(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('资源路径必须是工程内相对路径。')
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or any(part in {'.', '..'} for part in relative_path.parts):
        raise ValueError('资源路径不能越出指定工程。')
    path = root.joinpath(*relative_path.parts)
    if path == root or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('资源路径不能越出指定工程。')
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('任务安装路径不能经过符号链接或目录联接。')
    return path


def _read(path, inputs):
    raw = path.read_bytes() if path.exists() else None
    inputs[path] = raw
    return raw


def _verify_inputs(inputs):
    for path, expected in inputs.items():
        current = path.read_bytes() if path.exists() else None
        if current != expected:
            raise SyncConflict('准备期间文件发生变化，停止安装：' + str(path))


def _publish(path, raw, expected):
    """Recheck the original bytes immediately before publishing a complete file."""
    current = path.read_bytes() if path.exists() else None
    if (None if current is None else _sha(current)) != expected:
        raise SyncConflict('写入前文件发生变化，未覆盖：' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.task-assets-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current = path.read_bytes() if path.exists() else None
        if (None if current is None else _sha(current)) != expected:
            raise SyncConflict('暂存期间文件发生变化，未覆盖：' + str(path))
        if expected is None:
            publish_new_file(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass  # A published file remains committed if temporary cleanup fails.


def _templates(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'template':
                values = [item] if isinstance(item, str) else item
                if not isinstance(values, list) or not values or any(not isinstance(name, str) for name in values):
                    raise ValueError('Pipeline模板引用无效。')
                yield from values
            else:
                yield from _templates(item)
    elif isinstance(value, list):
        for item in value:
            yield from _templates(item)


def _default_options(task, options):
    result = []
    for name in task.get('option', []):
        schema = options.get(name)
        if not isinstance(schema, dict):
            raise ValueError('任务专用选项缺失：' + str(name))
        selected = dict(name=name)
        if schema.get('type', 'select') == 'select':
            cases = schema.get('cases', [])
            default = schema.get('default_case', cases[0].get('name') if cases else None)
            indices = [index for index, case in enumerate(cases) if case.get('name') == default]
            if not indices:
                raise ValueError('任务选项缺少有效默认项：' + name)
            selected['index'] = indices[0]
        elif schema.get('type') == 'input':
            selected['data'] = {item['name']: str(item.get('default', '')) for item in schema.get('inputs', [])}
        else:
            raise ValueError('不支持的原生任务选项类型：' + name)
        result.append(selected)
    return result


def _old_count(item, default):
    value = item.get('repeat_count')
    if value is None:
        value = next((option.get('data', {}).get('刷取次数') for option in item.get('option', [])
                      if isinstance(option, dict) and option.get('name') == '刷取次数'), None)
    if type(value) is bool:
        return default
    try:
        parsed = int(str(value).strip())
        return parsed if 1 <= parsed <= 2147483647 else default
    except (ValueError, TypeError):
        return default


def _merge_instance(before, task, defaults, old_options, new_options):
    after = deepcopy(before)
    name, entry = task['name'], task['entry']
    items, history = after.get('TaskItems', []), after.get('CurrentTasks', [])
    if not isinstance(items, list) or not isinstance(history, list):
        raise ValueError('实例任务列表格式无效。')
    matches = [item for item in items if isinstance(item, dict) and item.get('name') == name]
    if matches:
        preferred = next((item for item in matches if item.get('entry') == entry), matches[0])
        updated = deepcopy(preferred)
        for key, value in task.items():
            if key not in {'option', 'repeat_count', 'default_check', 'label'}:
                updated[key] = deepcopy(value)
        updated['repeat_count'] = _old_count(preferred, task.get('repeat_count', 1))
        updated['default_check'] = preferred.get('default_check', task.get('default_check', False))
        choices = preferred.get('option', [])
        if not isinstance(choices, list): raise ValueError('原生任务选项格式无效。')
        def migrate(choice):
            previous = old_options.get(choice.get('name'), {})
            target = choice.get('name') if choice.get('name') in new_options else None
            if target is None and previous.get('label'):
                candidates = [key for key, value in new_options.items()
                              if value.get('label') == previous['label'] and value.get('type', 'select') == previous.get('type', 'select')]
                if len(candidates) == 1: target = candidates[0]
            if target is None: return None
            result = deepcopy(choice)
            result['name'] = target
            if isinstance(result.get('data'), dict):
                prior_inputs = {value['name']: value for value in previous.get('inputs', [])}
                inputs = new_options[target].get('inputs', [])
                data = {}
                for key, value in result['data'].items():
                    destinations = [field['name'] for field in inputs if field['name'] == key or
                                    prior_inputs.get(key, {}).get('label') and field.get('label') == prior_inputs[key]['label']]
                    if len(destinations) == 1: data[destinations[0]] = value
                result['data'] = data
            for field in ('children', 'option', 'sub_options'):
                if isinstance(result.get(field), list): result[field] = [v for item in result[field] if (v := migrate(item)) is not None]
            return result
        migrated = [value for choice in choices if isinstance(choice, dict) and (value := migrate(choice)) is not None]
        known = {value['name'] for value in migrated}
        updated['option'] = migrated + [deepcopy(value) for value in defaults if value['name'] not in known]
        replaced, inserted = [], False
        for item in items:
            if isinstance(item, dict) and item.get('name') == name:
                if not inserted: replaced.append(updated); inserted = True
            else: replaced.append(item)
        after['TaskItems'] = replaced
    def matching(value): return isinstance(value, str) and value.partition(SEPARATOR)[0] == name and SEPARATOR in value
    if matches or any(matching(value) for value in history):
        updated, inserted = [], False
        for value in history:
            if matching(value):
                if not inserted: updated.append(name + SEPARATOR + entry); inserted = True
            else: updated.append(value)
        if not inserted: updated.append(name + SEPARATOR + entry)
        after['CurrentTasks'] = updated
    return after


def _strings(value):
    if isinstance(value, str): yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value: yield from _strings(item)


def _options_for(tasks, options):
    result, pending = {}, [name for task in tasks for name in task.get('option', [])]
    while pending:
        name = pending.pop()
        if name in result: continue
        if name not in options: raise ValueError('任务选项缺失：' + name)
        result[name] = deepcopy(options[name])
        pending += [value for value in _strings(options[name]) if value in options and value not in result]
    return result


def prepare_task_assets(source_root, target_root, task_names=None, update_version=False):
    """Plan the existing migration without writes, for installers with a journal.

    The returned byte plans and input fingerprints are internal installation
    data, not the JSON report exposed by the command-line interface.
    """
    source_root, target_root = Path(source_root).resolve(), Path(target_root).resolve()
    source_inputs, target_inputs, plans = {}, {}, {}
    source_prefix = 'assets/' if _safe(source_root, 'assets/interface.json').is_file() else ''
    source_interface_path = _safe(source_root, source_prefix + 'interface.json')
    source_interface = _json(_read(source_interface_path, source_inputs))
    if (not isinstance(source_interface, dict) or not isinstance(source_interface.get('task'), list)
            or not isinstance(source_interface.get('option'), dict)
            or any(not isinstance(item, dict) for item in source_interface['task'])):
        raise ValueError('源任务定义格式无效。')
    tasks = [task for task in source_interface['task'] if task_names is None or task.get('name') in task_names]
    if not tasks or (task_names is not None and set(task_names) != {task['name'] for task in tasks}):
        raise ValueError('请选择源工程中存在的任务。')
    if len({task['name'] for task in tasks}) != len(tasks): raise ValueError('源任务名称重复。')
    options = _options_for(tasks, source_interface['option'])
    retired_tasks, retired_options = set(), set()
    migrations_path = _safe(source_root, 'agent/task-migrations.json')
    migrations_raw = _read(migrations_path, source_inputs)
    if task_names is None and migrations_raw is not None:
        migrations = _json(migrations_raw)
        if migrations.get('version') != 1:
            raise ValueError('不支持的任务迁移清单版本。')
        canonical = {(task['name'], task['entry']) for task in source_interface['task']}
        retired_tasks = {(item['name'], item['entry']) for item in migrations.get('retired_tasks', [])} - canonical
        retired_options = set(migrations.get('retired_options', [])) - set(source_interface['option'])
    pipeline_files, node_files = {}, {}
    base = _safe(source_root, source_prefix + 'resource/base/pipeline')
    for path in sorted(base.rglob('*.json')):
        raw = _read(_safe(source_root, path.relative_to(source_root).as_posix()), source_inputs)
        value = _json(raw)
        if not isinstance(value, dict): raise ValueError('Pipeline必须是对象。')
        pipeline_files[path] = (raw, value)
        for node in value:
            if node in node_files:
                raise ValueError('Pipeline重复声明了节点：' + node)
            node_files[node] = path
    new_nodes = {name: value for _raw, pipeline in pipeline_files.values() for name, value in pipeline.items()}
    pending = [task['entry'] for task in tasks]
    if any(entry not in node_files for entry in pending): raise ValueError('Pipeline缺少任务入口。')
    pending += [text for text in _strings([tasks, options]) if text in node_files]
    selected_files, navigation_metadata = set(), {}
    while pending:
        node = pending.pop()
        path = node_files[node]
        if path in selected_files: continue
        selected_files.add(path)
        value = pipeline_files[path][1]
        if not navigation_metadata and any(custom_parameters(definition).get('navigation') is not None
                                           for definition in value.values() if isinstance(definition, dict)):
            # Graph edges are selected at runtime and are not ordinary next
            # links. Follow the packaged resource declaration as well.
            directory = _safe(source_root, 'agent/data/navigation')
            manifest_path = _safe(source_root, 'agent/data/navigation/resources.json')
            manifest = _json(_read(manifest_path, source_inputs))
            names = manifest.get('pipelines')
            if manifest.get('version') != 1 or not isinstance(names, list) or not names:
                raise ValueError('导航资源依赖清单无效。')
            for name in names:
                if not isinstance(name, str) or '/' in name or not name.endswith('.json'):
                    raise ValueError('导航Pipeline依赖文件名无效。')
                dependency = _safe(source_root, source_prefix+'resource/base/pipeline/'+name)
                if dependency not in pipeline_files:
                    raise ValueError('缺少导航Pipeline依赖：' + name)
                pending.extend(pipeline_files[dependency][1])
            for metadata in sorted(directory.glob('*.json')):
                relative = metadata.relative_to(source_root).as_posix()
                navigation_metadata[relative] = _read(_safe(source_root, relative), source_inputs)
        for definition in value.values():
            if not isinstance(definition, dict): raise ValueError('Pipeline节点格式无效。')
            for link in node_links(new_nodes, definition):
                if link not in node_files: raise ValueError('Pipeline包含缺失的后续节点：' + link)
                pending.append(link)
        pending += [text for text in _strings(value) if text in node_files]

    def plan_bytes(relative, raw):
        path = _safe(target_root, relative)
        old = _read(path, target_inputs)
        if raw != old:
            plans[relative] = (raw, old)

    def plan_json(relative, before, after):
        if not _same_json(before, after):
            plan_bytes(relative, _encode(after))

    defaults = _read(_safe(source_root, source_prefix + 'resource/base/default_pipeline.json'), source_inputs)
    if defaults is not None:
        if not isinstance(_json(defaults), dict):
            raise ValueError('资源默认识别配置必须为对象。')
        plan_bytes('resource/base/default_pipeline.json', defaults)

    runtime_interface_path = _safe(target_root, 'interface.json')
    runtime_raw = _read(runtime_interface_path, target_inputs)
    if runtime_raw is None:
        raise ValueError('目标缺少运行目录 interface.json。')
    before = _json(runtime_raw)
    if (not isinstance(before, dict) or not isinstance(before.get('task'), list)
            or not isinstance(before.get('option'), dict)):
        raise ValueError('目标任务或选项列表格式无效。')
    after = deepcopy(before)
    if update_version:
        version = source_interface.get('version')
        if not isinstance(version, str) or not version.strip():
            raise ValueError('源客户端缺少有效版本号，未修改目标配置。')
        after['version'] = version
    by_name = {task['name']: task for task in tasks}
    after_tasks, installed = [], set()
    for item in after['task']:
        if (item.get('name'), item.get('entry')) in retired_tasks:
            continue
        name = item.get('name')
        if name in by_name:
            if name not in installed: after_tasks.append(deepcopy(by_name[name])); installed.add(name)
        else: after_tasks.append(item)
    after_tasks += [deepcopy(task) for task in tasks if task['name'] not in installed]
    after['task'] = after_tasks
    after['option'].update(options)
    # Keep legacy options when an unrelated custom task still references them.
    referenced, pending_options = set(), [name for task in after_tasks for name in task.get('option', [])]
    while pending_options:
        name = pending_options.pop()
        if name in referenced: continue
        referenced.add(name)
        pending_options += [value for value in _strings(after['option'].get(name, {}))
                            if value in after['option'] and value not in referenced]
    for name in retired_options - referenced:
        after['option'].pop(name, None)
    plan_json('interface.json', before, after)
    bundle_path = _safe(source_root, 'agent/recordings/manifest.json')
    bundle_raw = _read(bundle_path, source_inputs)
    if bundle_raw is not None:
        bundle = _json(bundle_raw)
        if bundle.get('version') != 1: raise ValueError('不支持的默认脚本清单版本。')
        for item in bundle.get('items', []):
            filename = item['file']
            if '/' in filename or not filename.endswith('.json') or filename == 'manifest.json':
                raise ValueError('默认录制文件名无效。')
            relative = 'agent/recordings/' + filename
            raw = _read(_safe(source_root, relative), source_inputs)
            if raw is None or _sha(raw) != item['sha256']: raise ValueError('默认录制校验失败：' + filename)
            plan_bytes(relative, raw)
        plan_bytes('agent/recordings/manifest.json', bundle_raw)
    for relative, raw in navigation_metadata.items():
        _json(raw)
        plan_bytes(relative, raw)
    if navigation_metadata:
        library = _json(navigation_metadata.get('agent/data/navigation/library.json'))
        if library.get('version') != 1 or not isinstance(library.get('assets'), list):
            raise ValueError('导航素材清单无效。')
        for asset in library['assets']:
            template = asset.get('path')
            if not isinstance(template, str) or not template.startswith('navigation/'):
                raise ValueError('导航素材必须位于独立的navigation目录。')
            raw = _read(_safe(source_root, source_prefix+'resource/base/image/'+template), source_inputs)
            if raw is None or _sha(raw) != asset.get('sha256'):
                raise ValueError('导航素材校验失败：' + template)
            plan_bytes('resource/base/image/'+template, raw)
    for path in sorted(selected_files):
        raw, pipeline = pipeline_files[path]
        plan_bytes(path.relative_to(source_root/source_prefix).as_posix(), raw)
        for template in set(_templates(pipeline)):
            source_template = _safe(source_root, source_prefix + 'resource/base/image/' + template)
            if not source_template.is_file(): raise ValueError('Pipeline源模板不存在：' + template)
            plan_bytes('resource/base/image/' + template, _read(source_template, source_inputs))
    instances = _safe(target_root, 'config/instances')
    updated_instances, prior_instances = [], []
    if instances.exists():
        for path in sorted(instances.glob('*.json')):
            relative = path.relative_to(target_root).as_posix()
            _safe(target_root, relative)
            original = _json(_read(path, target_inputs))
            if not isinstance(original, dict):
                raise ValueError('实例配置必须是JSON对象。')
            prior_instances.append((relative, original))
            updated = deepcopy(original)
            updated['TaskItems'] = [item for item in updated.get('TaskItems', [])
                                    if not isinstance(item, dict) or (item.get('name'), item.get('entry')) not in retired_tasks]
            retired_history = {name + SEPARATOR + entry for name, entry in retired_tasks}
            updated['CurrentTasks'] = [item for item in updated.get('CurrentTasks', []) if item not in retired_history]
            for task in tasks:
                task_options = _options_for([task], options)
                updated = _merge_instance(updated, task, _default_options(task, task_options), before['option'], task_options)
            if not _same_json(original, updated):
                updated_instances.append(relative)
                plan_json(relative, original, updated)
    bindings_path = _safe(target_root, 'recordings/.bindings.json')
    _read(bindings_path, target_inputs)
    old_bindings, _ = _bindings_state(target_root/'recordings')
    _old_interface, old_nodes = _configuration(target_root)
    bindings, migrated_bindings = plan_legacy_bindings(target_root, before, tasks, new_nodes,
                                                     prior_instances, old_bindings, old_nodes)
    if bindings != old_bindings:
        plan_bytes('recordings/.bindings.json', _encode(dict(version=2, tasks=bindings)))
    report = dict(dry_run=True, applied=False, backup=None, updated_instances=updated_instances,
                  migrated_bindings=migrated_bindings,
                  changes=[dict(path=path, operation='create' if old is None else 'update', sha256=_sha(raw))
                           for path, (raw, old) in plans.items()])
    return report, plans, source_inputs, target_inputs


def sync_task_assets(source_root, target_root, dry_run=True, task_names=None, update_version=False):
    target_root = Path(target_root).resolve()
    report, plans, source_inputs, target_inputs = prepare_task_assets(
        source_root, target_root, task_names, update_version)
    report['dry_run'] = dry_run
    if dry_run:
        return report
    _verify_inputs({**source_inputs, **target_inputs})
    if not plans:
        report['applied'] = True
        return report
    lock_path = _safe(target_root, '.task-assets-sync.lock')
    lock_descriptor = None
    written = []
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        _verify_inputs({**source_inputs, **target_inputs})
        backup_relative = 'backup/task-assets-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8]
        backup = _safe(target_root, backup_relative)
        backup.mkdir(parents=True, exist_ok=False)
        report['backup'] = backup_relative
        for relative, (_raw, old) in plans.items():
            if old is not None:
                path = _safe(target_root, backup_relative + '/' + relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(old)
        (backup / 'installation.json').write_bytes(_encode(dict(version=1, files=[
            dict(path=relative, existed=old is not None, before_sha256=None if old is None else _sha(old), after_sha256=_sha(raw))
            for relative, (raw, old) in plans.items()])))
        for relative, (raw, old) in plans.items():
            path = _safe(target_root, relative)
            _publish(path, raw, None if old is None else _sha(old))
            written.append((path, raw, old))
        report['applied'] = True
        return report
    except Exception as error:
        for path, raw, old in reversed(written):
            try:
                if path.exists() and _sha(path.read_bytes()) == _sha(raw):
                    if old is None:
                        path.unlink()
                    else:
                        _publish(path, old, _sha(raw))
                elif path.exists():
                    error.add_note('回滚保留了其它程序的新修改：' + str(path))
            except OSError as rollback_error:
                error.add_note('未完成回滚，请使用安装备份：' + str(rollback_error))
        if report['backup']:
            error.add_note('安装前备份：' + str(target_root / report['backup']))
        raise
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
            try:
                lock_path.unlink()
            except OSError:
                pass


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--target', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true', help='Write the plan after the runtime frontend is closed.')
    parser.add_argument('--task', action='append', help='Task name; repeat for several tasks. Default: all tasks.')
    parser.add_argument('--update-version', action='store_true', help='Also adopt the source client version during a complete manual upgrade.')
    args = parser.parse_args()
    print(json.dumps(sync_task_assets(args.source, args.target, dry_run=not args.apply, task_names=args.task,
                                    update_version=args.update_version), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
