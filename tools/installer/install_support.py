"""Prepare an Inno install using the candidate's bundled Python, never the old one.

Only a confirmed project installation has obsolete program files removed.
Personal data stays in place. No backup of old programs is made.
"""
import argparse
import configparser
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

PROGRAM_DIRS = ('agent', 'resource', 'libs', 'runtimes', 'docs', 'update_engine')
LEGACY_FILES = ('updater.py', 'MFAUpdater.exe', 'MFAUpdater.dll', 'MFAUpdater.deps.json',
                'MFAUpdater.runtimeconfig.json', 'version.json')


def safe(root, relative):
    root = Path(root).absolute()
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('安装路径越界：' + str(path))
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError('安装路径包含链接，未进行清理：' + str(item))
    return path


def identity(root):
    """Use project metadata plus entry/runtime files, never the folder name."""
    root = Path(root)
    try:
        if root == Path(root.anchor): return False
        doc = json.loads(safe(root, 'interface.json').read_text('utf-8-sig'))
        return (doc.get('name') == 'MAA造梦西游4' and isinstance(doc.get('task'), list)
                and isinstance(doc.get('version'), str) and bool(doc['version'])
                and all(safe(root, name).is_file() for name in
                        ('MFAAvalonia.exe', 'agent/main.py', 'runtimes/python/python.exe')))
    except (OSError, ValueError, TypeError):
        return False


def destination(selected):
    path = Path(selected).absolute()
    if identity(path): return path
    return path/'MAAZMXY4'


def personal(name):
    parts = Path(name).parts
    normalized = '/'.join(parts).casefold()
    return (parts[0].casefold() in ('config', 'recordings', '.personal-transfer')
            or normalized == 'appsettings.json'
            or normalized.startswith('agent/recordings/')
            or normalized.startswith('resource/') and parts[-1].casefold() == 'mfa_layout.json')


def ensure_idle(root):
    if os.name != 'nt': return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                    ('name', wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = kernel.CreateToolhelp32Snapshot(2, 0)
    if snap == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
    entry = Entry(); entry.size = ctypes.sizeof(entry)
    try:
        more = kernel.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            handle = kernel.OpenProcess(0x1000, False, entry.pid)
            if handle:
                try:
                    name = ctypes.create_unicode_buffer(32768); length = wintypes.DWORD(len(name))
                    if kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
                        if Path(name.value).resolve().is_relative_to(root.resolve()):
                            raise ValueError('请先退出此目录的小助手及其后台进程，再重试安装。')
                finally: kernel.CloseHandle(handle)
            elif entry.name.casefold() in ('mfaavalonia.exe', 'python.exe', 'pythonw.exe'):
                raise ValueError('无法核验客户端进程归属，请关闭后重试。')
            more = kernel.Process32NextW(snap, ctypes.byref(entry))
    finally: kernel.CloseHandle(snap)


def prepare(source, target, output):
    source, target, output = map(lambda p: Path(p).absolute(), (source, target, output))
    safe(target, '.')
    ensure_idle(target)
    old = identity(target)
    # Plan all migrations before removing even one old program file. Existing
    # task migration understands legacy script selections and unknown fields.
    plans = {}
    if old:
        sys.path.insert(0, str(source/'agent'))
        from task_asset_sync import prepare_task_assets
        _, plans, _, _ = prepare_task_assets(source, target, update_version=True)
        if 'interface.json' not in plans:
            raw = (target/'interface.json').read_bytes(); plans['interface.json'] = (raw, raw)
    for name, (raw, _) in plans.items():
        if name == 'interface.json' or personal(name):
            path = safe(output, name); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    names = json.loads((source/'installer-files.json').read_text('utf8'))['files']
    # Preflight every destination, including children in non-empty new folders.
    for name in names: safe(target, name)
    removed = []
    if old:
        candidates = {safe(target, name) for name in (*names, *LEGACY_FILES)}
        for folder in PROGRAM_DIRS:
            base = safe(target, folder)
            if base.exists():
                candidates.update(base.rglob('*'))
        checked = [safe(target, p.relative_to(target)) for p in candidates]
        # Keep identity anchors until Inno replaces them so interrupted installs
        # can still be recognized on retry. Do not touch unknown root files.
        anchors = {'interface.json', 'MFAAvalonia.exe', 'agent/main.py', 'runtimes/python/python.exe'}
        deleting = sorted((p for p in checked if p.is_file() and
                           p.relative_to(target).as_posix() not in anchors and
                           not personal(p.relative_to(target))), key=str)
        # Check exclusive access first: loaded executables/DLLs must stop cleanup
        # before any file is removed. No process is terminated by the installer.
        if os.name == 'nt':
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            k.CreateFileW.restype = wintypes.HANDLE; k.CloseHandle.argtypes = [wintypes.HANDLE]
            for path in deleting:
                h = k.CreateFileW(str(path), 0x10000 | 0x80000000, 7, None, 3, 0, None)
                if h == ctypes.c_void_p(-1).value: raise OSError('旧程序文件正在使用或不可替换：' + str(path))
                k.CloseHandle(h)
        for path in deleting:
            path.unlink(); removed.append(path.relative_to(target).as_posix())
    return dict(old_client=old, removed=removed, preserved='personal configuration, recordings and unrelated root files')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('plan', 'install'), required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.phase == 'plan':
            target = destination(args.target)
            safe(target, '.')
            ensure_idle(target)
            ini = configparser.ConfigParser(); ini['install'] = {'directory': str(target)}
            with (args.output.parent/'install-plan.ini').open('w', encoding='utf-16') as f: ini.write(f)
            return 0
        result = prepare(args.source, args.target, args.output)
        (args.output.parent/'install-plan.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf8')
        return 0
    except Exception as error:
        (args.output.parent/'install-error.txt').write_text(str(error), 'utf-8-sig')
        return 1


if __name__ == '__main__': raise SystemExit(main())
