"""Small append-only log blocks; the frontend owns the shared 64 MiB budget."""
import os
from pathlib import Path
import threading
import time
import uuid

BLOCK_BYTES = 4 * 1024 * 1024
_lock = threading.Lock()


def _log_path(path):
    path = Path(path)
    if path.suffix.lower() != '.log' or path.is_symlink():
        raise ValueError('仅能写入普通日志文件。')
    return path


def _open_append(path):
    try:
        return path.open('ab')
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open('ab')


def _archive(path, identity):
    try:
        current = path.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (identity.st_ino, identity.st_size, identity.st_mtime_ns):
            return None
        archive = path.with_name(f'{path.stem}.bak.{time.time_ns()}.{os.getpid()}.{uuid.uuid4().hex[:8]}.log')
        path.rename(archive)
        return archive
    except OSError:
        # A second writer can keep the block open or roll it first. Never
        # truncate a live block or turn a logging failure into a task failure.
        return None


def archive_log(path):
    """Retire a completed round's log without overwriting the previous round."""
    path = _log_path(path)
    with _lock:
        try:
            with path.open('rb') as stream:
                identity = os.fstat(stream.fileno())
        except FileNotFoundError:
            return None
        return _archive(path, identity) if identity.st_size else None


def append_log(path, text, *, block_bytes=BLOCK_BYTES):
    if block_bytes <= 0:
        raise ValueError('日志分块大小必须为正数。')
    path = _log_path(path)
    data = text.encode('utf-8')
    with _lock:
        with _open_append(path) as stream:
            # Append mode is already positioned at EOF: no directory-size scan
            # per line. One oversize record remains intact in its own block.
            size = stream.tell()
            if size == 0 or size + len(data) <= block_bytes:
                stream.write(data)
                return
            identity = os.fstat(stream.fileno())
        _archive(path, identity)
        with _open_append(path) as stream:
            stream.write(data)
