"""Editable bundled recordings and file operations for the shared script library."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from script_library import (_binding_lock, _bindings_state, _index_path, _task_id,
                            library_recording_path)
from script_studio.model import MAX_FILE_BYTES, RecordingDocument

DEFAULT_INDEX = '.defaults.json'


def _read(path):
    if not path.exists(): return None
    with path.open('rb') as stream: raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES: raise ValueError('脚本库文件过大。')
    return raw


def _json(raw):
    return json.loads(raw.decode('utf-8-sig'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效数值。')))


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf8')


def _publish(path, raw, expected, verify=None):
    """Publish a complete file only if its previous revision is unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.library-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if verify is not None: verify()
        if _read(path) != expected: raise ValueError('脚本库已被其他窗口修改，请刷新后重试。')
        if expected is None: os.link(temporary, path)
        else: os.replace(temporary, path)
    finally:
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass


def _defaults(directory):
    raw = _read(_index_path(directory, DEFAULT_INDEX))
    value = dict(version=1, items={}) if raw is None else _json(raw)
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('items'), dict):
        raise ValueError('默认脚本登记信息损坏，请检查 .defaults.json。')
    for key, entry in value['items'].items():
        if not isinstance(key, str) or not isinstance(entry, dict) or entry.get('state') not in ('present', 'deleted'):
            raise ValueError('默认脚本登记信息无效。')
        library_recording_path(directory, entry.get('file'))
    return value, raw


def default_recordings(directory):
    value, _ = _defaults(Path(directory))
    return {entry['file']: key for key, entry in value['items'].items() if entry['state'] == 'present'}


def _same_origin(existing, packaged, ident):
    if not isinstance(existing, dict): return False
    if existing.get('builtin_id') == ident: return True
    editor = existing.get('editor')
    source = editor.get('source_macro') if isinstance(editor, dict) else None
    source = source if isinstance(source, dict) else {}
    if source.get('id') == ident: return True
    return existing.get('actions') == packaged.get('actions') and existing.get('base') == packaged.get('base')


def ensure_default_recordings(root):
    """Seed real, writable files once; never restore deleted or replace edited files."""
    root = Path(root).resolve()
    bundle = root / 'agent/recordings'
    manifest = bundle / 'manifest.json'
    if not manifest.is_file(): return []
    spec = _json(_read(manifest))
    if spec.get('version') != 1 or not isinstance(spec.get('items'), list):
        raise ValueError('默认脚本清单无效。')
    directory = root / 'recordings'
    directory.mkdir(parents=True, exist_ok=True)
    seeded = []
    with _binding_lock(directory):
        registry, previous = _defaults(directory)
        for item in spec['items']:
            ident = item['id']
            if ident in registry['items']: continue
            source = library_recording_path(bundle, item['file'], must_exist=True)
            raw = _read(source)
            if hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('默认脚本校验失败：' + item['file'])
            packaged = _json(raw)
            RecordingDocument.from_dict(packaged).to_dict(strict=True)
            name = item['file']
            path = library_recording_path(directory, name)
            existing = None
            if path.exists():
                try: existing = _json(_read(path))
                except (ValueError, UnicodeError): pass  # Keep damaged user files untouched and visible in the library.
            if path.exists() and not _same_origin(existing, packaged, ident):
                stem, suffix = Path(name).stem, 1
                while path.exists():
                    name = f'{stem}-默认{suffix if suffix > 1 else ""}.json'
                    path = library_recording_path(directory, name)
                    suffix += 1
            if not path.exists(): _publish(path, raw, None)
            registry['items'][ident] = dict(file=name, state='present')
            # Commit each item: an interrupted seeding resumes without overwriting it.
            updated = _encoded(registry)
            _publish(_index_path(directory, DEFAULT_INDEX), updated, previous)
            previous = updated
            seeded.append(name)
        if not registry.get('bindings_initialized') and spec.get('initial_bindings'):
            bindings, binding_raw = _bindings_state(directory)
            added_binding = False
            for initial in spec['initial_bindings']:
                entry = registry['items'].get(initial['default_id'])
                if entry and entry['state'] == 'present' and initial['task_id'] not in bindings:
                    bindings[initial['task_id']] = dict(file=entry['file'], mode=initial['mode'])
                    added_binding = True
            if added_binding:
                _publish(_index_path(directory), _encoded(dict(version=2, tasks=bindings)), binding_raw)
            registry['bindings_initialized'] = True
            _publish(_index_path(directory, DEFAULT_INDEX), _encoded(registry), previous)
    return seeded


def _revision_file(directory, filename, expected_revision):
    path = library_recording_path(directory, filename, must_exist=True)
    raw = _read(path)
    if not isinstance(expected_revision, str) or hashlib.sha256(raw).hexdigest() != expected_revision:
        raise ValueError('脚本已被其他窗口修改，请刷新后重试。')
    return path, raw


def binding_snapshot(directory):
    bindings, raw = _bindings_state(Path(directory))
    return bindings, hashlib.sha256(raw or b'').hexdigest()


def set_file_bindings(directory, filename, task_modes, expected_revision, expected_bindings):
    directory = Path(directory)
    with _binding_lock(directory):
        _revision_file(directory, filename, expected_revision)
        bindings, raw = _bindings_state(directory)
        if hashlib.sha256(raw or b'').hexdigest() != expected_bindings:
            raise ValueError('任务绑定已被其他窗口修改，请刷新后重新选择。')
        for key, value in list(bindings.items()):
            if value['file'] == filename and key not in task_modes: del bindings[key]
        for key, mode in task_modes.items():
            _task_id(key)
            if mode not in ('pipeline', 'replace'): raise ValueError('任务绑定方式无效。')
            bindings[key] = dict(file=filename, mode=mode)
        _publish(_index_path(directory), _encoded(dict(version=2, tasks=bindings)), raw,
                 verify=lambda: _revision_file(directory, filename, expected_revision))
    return dict(file=filename, task_ids=list(task_modes))


def duplicate_recording(directory, filename, name, expected_revision):
    directory = Path(directory)
    if not isinstance(name, str) or not name.strip(): raise ValueError('请输入脚本名称。')
    destination = library_recording_path(directory, name.strip() + '.json')
    with _binding_lock(directory):
        _, raw = _revision_file(directory, filename, expected_revision)
        value = _json(raw)
        value['name'] = name.strip()
        value.pop('builtin_id', None)
        RecordingDocument.from_dict(value).to_dict(strict=False)
        if destination.exists(): raise ValueError('同名脚本已存在，请使用其他名称。')
        _publish(destination, _encoded(value), None)
    return dict(file=destination.name)


def rename_recording(directory, filename, name, expected_revision):
    """Rename the saved document and every library-owned reference under one lock.

    Keep the original until publication completes; failed index writes roll back
    only our own revisions, never a concurrently modified file.
    """
    directory = Path(directory).resolve()
    if not isinstance(name, str) or not name.strip(): raise ValueError('请输入脚本名称。')
    name = name.strip()
    destination = library_recording_path(directory, name + '.json')
    with _binding_lock(directory):
        source, original = _revision_file(directory, filename, expected_revision)
        same_path = source == destination
        if any(p.name.casefold() == destination.name.casefold() and p != source
               for p in directory.glob('*.json')):
            raise ValueError('同名脚本已存在，请使用其他名称。')
        value = _json(original)
        value['name'] = name
        RecordingDocument.from_dict(value).to_dict(strict=False)
        renamed = _encoded(value)
        bindings, binding_raw = _bindings_state(directory)
        registry, default_raw = _defaults(directory)
        selected_path = _index_path(directory, '.selected.json')
        selected_raw = _read(selected_path)
        selected = _json(selected_raw) if selected_raw is not None else None
        updates = []
        if any(item['file'] == filename for item in bindings.values()):
            for item in bindings.values():
                if item['file'] == filename: item['file'] = destination.name
            updates.append((_index_path(directory), binding_raw, _encoded(dict(version=2, tasks=bindings))))
        for item in registry['items'].values():
            if item['file'] == filename: item['file'] = destination.name
        if default_raw is not None and _json(default_raw) != registry:
            updates.append((_index_path(directory, DEFAULT_INDEX), default_raw, _encoded(registry)))
        if isinstance(selected, dict) and selected.get('file') == filename:
            selected['file'] = destination.name
            updates.append((selected_path, selected_raw, _encoded(selected)))
        published = []
        file_published = False
        try:
            _publish(destination, renamed, original if same_path else None,
                     verify=lambda: _revision_file(directory, filename, expected_revision))
            file_published = True
            for index, before, after in updates:
                _publish(index, after, before)
                published.append((index, before, after))
            if same_path:
                # Windows compares these paths case-insensitively; rename still
                # updates the actual directory entry for a case-only edit.
                if source.name != destination.name: os.rename(source, destination)
            else:
                _revision_file(directory, filename, expected_revision)
                source.unlink()
        except Exception:
            for index, before, after in reversed(published):
                if _read(index) == after: _publish(index, before, after)
            if file_published and _read(destination) == renamed:
                if same_path: _publish(source, original, renamed)
                else: destination.unlink()
            raise
    return dict(file=destination.name, revision=hashlib.sha256(renamed).hexdigest())


def delete_recording(directory, filename, expected_revision):
    """Remove a file and its bindings together; retain its bytes in a private trash."""
    directory = Path(directory).resolve()
    with _binding_lock(directory):
        path, raw = _revision_file(directory, filename, expected_revision)
        bindings, binding_raw = _bindings_state(directory)
        registry, default_raw = _defaults(directory)
        affected = [key for key, value in bindings.items() if value['file'] == filename]
        for key in affected: del bindings[key]
        for entry in registry['items'].values():
            if entry['file'] == filename: entry['state'] = 'deleted'
        trash = directory / '.trash'
        if trash.resolve() != directory / '.trash': raise ValueError('脚本回收目录不能使用符号链接。')
        trash.mkdir(exist_ok=True)
        archive = trash / (uuid4().hex + '-' + filename)
        updates = []
        if affected:
            updates.append((_index_path(directory), binding_raw, _encoded(dict(version=2, tasks=bindings))))
        if default_raw is not None and _json(default_raw) != registry:
            updates.append((_index_path(directory, DEFAULT_INDEX), default_raw, _encoded(registry)))
        published = []
        try:
            for index, before, after in updates:
                _publish(index, after, before)
                published.append((index, before, after))
            _revision_file(directory, filename, expected_revision)
            os.replace(path, archive)
        except Exception:
            for index, before, after in reversed(published):
                if _read(index) == after:
                    if before is None: index.unlink()
                    else: _publish(index, before, after)
            raise
    return dict(deleted=filename, unbound_tasks=affected, recovery_file=str(archive))
