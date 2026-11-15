"""Library-only folders; grouping never changes recording bytes or task references."""
import hashlib
from pathlib import Path
from uuid import uuid4

from script_library import _binding_lock, _index_path, library_recording_path
from recording_store import _read, _json, _encoded, _publish

INDEX = '.folders.json'


def state(directory):
    raw = _read(_index_path(directory, INDEX))
    value = dict(version=1, folders={}, members={}) if raw is None else _json(raw)
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('folders'), dict) or not isinstance(value.get('members'), dict)):
        raise ValueError('文件夹索引无法读取，请检查 .folders.json；原文件未覆盖。')
    for ident, folder in value['folders'].items():
        if not isinstance(ident, str) or not isinstance(folder, dict) or not isinstance(folder.get('name'), str):
            raise ValueError('文件夹索引内容无效；原文件未覆盖。')
    for filename, ident in value['members'].items():
        library_recording_path(directory, filename)
        if not isinstance(ident, str) or ident not in value['folders']:
            raise ValueError('文件夹索引包含失效分组；原文件未覆盖。')
    return value, raw


def snapshot(directory):
    value, raw = state(directory)
    return value, hashlib.sha256(raw or b'').hexdigest()


def _current(directory, revision):
    value, raw = state(directory)
    if hashlib.sha256(raw or b'').hexdigest() != revision:
        raise ValueError('文件夹已被其他窗口修改，请刷新后重试。')
    return value, raw


def _name(value, name, ident=None):
    if not isinstance(name, str) or not name.strip():
        raise ValueError('请输入文件夹名称。')
    name = name.strip()
    if any(folder['name'].casefold() == name.casefold() for key, folder in value['folders'].items() if key != ident):
        raise ValueError('同名文件夹已存在，请使用其他名称。')
    return name


def save_folder(directory, name, revision, ident=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with _binding_lock(directory):
        value, raw = _current(directory, revision)
        name = _name(value, name, ident)
        if ident is None:
            ident = uuid4().hex
            value['folders'][ident] = dict(name=name)
        else:
            value['folders'][ident]['name'] = name
        _publish(_index_path(directory, INDEX), _encoded(value), raw)
    return dict(folder_id=ident)


def move_recording(directory, filename, ident, revision):
    directory = Path(directory)
    with _binding_lock(directory):
        library_recording_path(directory, filename, must_exist=True)
        value, raw = _current(directory, revision)
        if ident is None:
            value['members'].pop(filename, None)
        else:
            if ident not in value['folders']:
                raise ValueError('目标文件夹已不存在，请刷新列表。')
            value['members'][filename] = ident
        _publish(_index_path(directory, INDEX), _encoded(value), raw)
    return dict(file=filename, folder_id=ident)


def membership_update(directory, *, renamed=None, removed=(), folder=None):
    """Called under the existing library lock; participates in its transaction."""
    value, raw = state(directory)
    before = _encoded(value)
    if renamed is not None:
        source, destination = renamed
        if source in value['members']:
            value['members'][destination] = value['members'].pop(source)
    for filename in removed:
        value['members'].pop(filename, None)
    if folder is not None:
        value['folders'].pop(folder, None)
        value['members'] = {name: ident for name, ident in value['members'].items() if ident != folder}
    after = _encoded(value)
    return [(_index_path(directory, INDEX), raw, after)] if before != after else []


def delete_folder(directory, ident, revision, revisions):
    from recording_store import _delete_recordings_locked
    directory = Path(directory).resolve()
    with _binding_lock(directory):
        value, _ = _current(directory, revision)
        if ident not in value['folders']:
            raise ValueError('文件夹已不存在，请刷新列表。')
        members = {name for name, folder in value['members'].items()
                   if folder == ident and library_recording_path(directory, name).is_file()}
        if not isinstance(revisions, dict) or set(revisions) != members:
            raise ValueError('文件夹内容已变化，请刷新后重新确认删除。')
        updates = membership_update(directory, removed=members, folder=ident)
        result = _delete_recordings_locked(directory, revisions, folder_updates=updates)
    return dict(folder_id=ident, **result)
