"""Saved-recording paths and live task bindings shared by studio and playback.

An index stores only a library filename. Editing that file changes the next
round; a task never owns a second recording snapshot or consults another project.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

INDEX_FILE = '.bindings.json'
MAX_INDEX_BYTES = 64 * 1024
_TASK_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def library_recording_path(directory, name, *, must_exist=False):
    directory = Path(directory)
    if (not isinstance(name, str) or not name or Path(name).name != name
            or any(c in name for c in '<>:"/\\|?*') or any(ord(c) < 32 for c in name)
            or name.startswith('.') or not name.lower().endswith('.json')
            or name.split('.')[0].upper() in _RESERVED):
        raise ValueError('请选择脚本库中的 JSON 文件。')
    path = directory / name
    if path.resolve().parent != directory.resolve():
        raise ValueError('脚本不能指向录制目录以外的位置。')
    if must_exist and not path.is_file():
        raise FileNotFoundError('任务绑定的脚本已不存在：' + name + '；请重新绑定，未改用其他录制。')
    return path


def _task_id(task_id):
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError('任务绑定标识无效。')
    return task_id


def _index_path(directory, name=INDEX_FILE):
    directory = Path(directory)
    path = directory / name
    if path.resolve().parent != directory.resolve():
        raise ValueError('任务绑定索引不能指向脚本库外部。')
    return path


def _read_index(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_INDEX_BYTES + 1)
        if len(raw) > MAX_INDEX_BYTES:
            raise ValueError('索引过大')
        value = json.loads(raw.decode('utf-8-sig'), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('无效数值')))
        return value, raw
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError('任务绑定索引损坏或无法读取，请检查 ' + path.name + '。') from error


def _bindings_state(directory):
    path = _index_path(directory)
    if not path.exists():
        return {}, None
    value, raw = _read_index(path)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] not in (1, 2)
            or not isinstance(value.get('tasks'), dict) or len(value['tasks']) > 256):
        raise ValueError('任务绑定索引格式无效。')
    tasks = {}
    for task_id, binding in value['tasks'].items():
        _task_id(task_id)
        binding = {'file': binding, 'mode': 'pipeline'} if isinstance(binding, str) else binding
        if not isinstance(binding, dict) or binding.get('mode') not in ('pipeline', 'replace'):
            raise ValueError('任务绑定的执行方式无效。')
        library_recording_path(directory, binding.get('file'))
        tasks[task_id] = dict(file=binding['file'], mode=binding['mode'])
    return tasks, raw


def read_task_bindings(directory):
    return {key: value['file'] for key, value in read_binding_records(directory).items()}


def read_binding_records(directory):
    return _bindings_state(directory)[0]


def resolve_task_recording(directory, task_id):
    task_id = _task_id(task_id)
    name = read_task_bindings(directory).get(task_id)
    if name is None:
        raise ValueError('任务尚未绑定脚本，请在脚本库选择具体任务并绑定已保存文件。')
    return library_recording_path(directory, name, must_exist=True)


def resolve_recording_reference(directory, reference):
    """Resolve a native task reference strictly, with no latest/manual fallback."""
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError('任务尚未选择录制脚本。')
    reference = reference.strip()
    if reference.startswith('@task:'):
        return resolve_task_recording(directory, reference[len('@task:'):])
    if reference == '@selected':
        path = _index_path(directory, '.selected.json')
        if not path.exists():
            raise ValueError('脚本工具尚未选择脚本，请先选择并保存。')
        value, _raw = _read_index(path)
        if not isinstance(value, dict) or not value.get('file'):
            raise ValueError('脚本工具尚未选择有效脚本。')
        return library_recording_path(directory, value['file'], must_exist=True)
    if reference.startswith('@'):
        raise ValueError('原生任务需要明确绑定脚本，不能使用 ' + reference + '。')
    path = Path(reference)
    if path.is_absolute():
        if path.resolve().parent != Path(directory).resolve():
            raise ValueError('任务脚本必须位于脚本库中。')
        reference = path.name
    if not reference.lower().endswith('.json'):
        reference += '.json'
    return library_recording_path(directory, reference, must_exist=True)


@contextmanager
def _binding_lock(directory):
    """OS advisory locking is released on process exit; the hidden file is inert."""
    path = _index_path(directory, '.bindings.lock')
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError('其他窗口正在更新任务绑定，请稍后重试。') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def bind_task_recording(directory, task_id, filename, *, expected_revision, mode='pipeline'):
    """Publish a filename binding only while the loaded saved file still matches."""
    directory = Path(directory)
    task_id = _task_id(task_id)
    if mode not in ('pipeline', 'replace'):
        raise ValueError('请选择有效的任务绑定方式。')
    recording = library_recording_path(directory, filename, must_exist=True)
    directory.mkdir(parents=True, exist_ok=True)
    with _binding_lock(directory):
        tasks, original = _bindings_state(directory)
        path = _index_path(directory)
        if hashlib.sha256(recording.read_bytes()).hexdigest() != expected_revision:
            raise ValueError('文件已被其他程序修改，请重新加载后绑定。')
        tasks[task_id] = dict(file=filename, mode=mode)
        raw = (json.dumps(dict(version=2, tasks=tasks), ensure_ascii=False, indent=2) + '\n').encode('utf8')
        if len(raw) > MAX_INDEX_BYTES:
            raise ValueError('任务绑定索引过大。')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.binding-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if (path.read_bytes() if path.exists() else None) != original:
                raise ValueError('任务绑定已被其他程序修改，未覆盖。')
            if hashlib.sha256(recording.read_bytes()).hexdigest() != expected_revision:
                raise ValueError('文件已被其他程序修改，请重新加载后绑定。')
            if original is None:
                os.link(temporary, path)
            else:
                os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass  # Publication succeeded; cleanup cannot undo the binding.
    return dict(task_id=task_id, file=filename, mode=mode)


def unbind_task_recording(directory, task_id, *, expected_file):
    directory = Path(directory)
    with _binding_lock(directory):
        tasks, original = _bindings_state(directory)
        binding = tasks.get(_task_id(task_id))
        if not binding: return dict(task_id=task_id, removed=False)
        if binding['file'] != expected_file:
            raise ValueError('任务绑定已被其他窗口修改，请刷新后再解除。')
        del tasks[task_id]
        raw = (json.dumps(dict(version=2, tasks=tasks), ensure_ascii=False, indent=2) + '\n').encode('utf8')
        path = _index_path(directory)
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.binding-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if path.read_bytes() != original: raise ValueError('任务绑定已被其他程序修改，未覆盖。')
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return dict(task_id=task_id, removed=True)
