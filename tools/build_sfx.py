#!/usr/bin/env python3
"""把已验证的完整客户端 ZIP 包装为 7-Zip 自解压 EXE，不修改输入文件。"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile

from build_release import validate_client

OFFICIAL_SHA256 = 'cf59d9470dd71cb2f0a4ad21cddcede25b55ca3fafde6d653f6475fdcf22a351'
SFX_PREFIX_LENGTH = 215040
SFX_PREFIX_SHA256 = 'b46b816df1f76de1c698fada3c3747f71c7c38eb6bb9ba714b0ab7fef1ca40de'
SEVEN_ZR_SHA256 = '9bf582b894dfa665f852d7e9112b02290a1c303c2d95a21fcdc41ffcb03143b7'
RESERVED_DEVICES = re.compile(r'^(CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$', re.IGNORECASE)
PRIVATE_ROOTS = {'config', 'recordings', 'debug', 'logs', 'backup', 'temp',
                 '.runtime-cache', '.local-migrations', '.personal-transfer'}
PRIVATE_NAMES = {'agents.md', '.bindings.json', '.defaults.json', '.selected.json'}
LICENSE_NAMES = ('LICENSE-7zip.txt', 'LICENSE-LGPL-2.1.txt', 'NOTICE-7zip.txt')


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def checked_file(path, label):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise ValueError(f'{label}缺失、为空或为链接：{path}')
    return path


def verified_prefix(official):
    official = checked_file(official, '官方 v2.3.1 自解压包')
    if sha256_file(official) != OFFICIAL_SHA256:
        raise ValueError('官方 v2.3.1 自解压包 SHA-256 不匹配。')
    with official.open('rb') as stream:
        prefix = stream.read(SFX_PREFIX_LENGTH)
        magic = stream.read(6)
    if (len(prefix) != SFX_PREFIX_LENGTH or
            hashlib.sha256(prefix).hexdigest() != SFX_PREFIX_SHA256 or
            magic != b"7z\xbc\xaf\x27\x1c"):
        raise ValueError('官方自解压前缀或 7z 数据位置不匹配。')
    return prefix


def verified_seven_zr(path):
    path = checked_file(path, '7zr.exe')
    if sha256_file(path) != SEVEN_ZR_SHA256:
        raise ValueError('7zr.exe SHA-256 不匹配。')
    return path.resolve()


def license_inputs(seven_zip_license, lgpl_license):
    result = []
    for path, label in ((seven_zip_license, '7-Zip 许可证'), (lgpl_license, 'LGPL 许可证')):
        path = checked_file(path, label)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError(label + '超过大小限制。')
        raw = path.read_bytes()
        try:
            contents = raw.decode('utf-8-sig').casefold()
        except UnicodeDecodeError as error:
            raise ValueError(label + '不是 UTF-8 文本。') from error
        result.append((raw, contents))
    if '7-zip' not in result[0][1] or 'gnu lgpl' not in result[0][1]:
        raise ValueError('7-Zip 许可证内容无效。')
    if 'gnu lesser general public license' not in result[1][1] or 'version 2.1' not in result[1][1]:
        raise ValueError('LGPL 2.1 许可证内容无效。')
    return result[0][0], result[1][0]


def safe_zip_entries(archive):
    """Validate ZIP paths and Windows collisions before writing any entries."""
    entries = archive.infolist()
    if not entries or len(entries) > 100000 or sum(i.file_size for i in entries) > 2 * 1024**3:
        raise ValueError('ZIP 为空或超过安全大小限制。')
    seen = {}
    for item in entries:
        name = item.filename
        directory = item.is_dir()
        if not name or name.startswith('/') or '\\' in name or '\x00' in item.orig_filename:
            raise ValueError('ZIP 路径无效：' + repr(name))
        parts = name[:-1].split('/') if directory else name.split('/')
        for part in parts:
            if (not part or part in ('.', '..') or part[-1:] in (' ', '.') or
                    any(ord(c) < 32 or c in '<>:"|?*' for c in part) or
                    RESERVED_DEVICES.fullmatch(part.split('.', 1)[0].rstrip(' '))):
                raise ValueError('ZIP 含不安全的 Windows 路径：' + repr(name))
            if part.casefold() in PRIVATE_NAMES or part.casefold() == '.git':
                raise ValueError('ZIP 含本地数据：' + repr(name))
        if parts[0].casefold() in PRIVATE_ROOTS or parts[0].casefold() == 'appsettings.json':
            raise ValueError('ZIP 含本地数据：' + repr(name))
        if len(parts) == 1 and parts[0].casefold() in {s.casefold() for s in LICENSE_NAMES}:
            raise ValueError('ZIP 与安装包许可证文件重名：' + repr(name))
        mode = (item.external_attr >> 16) & 0xFFFF
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
            raise ValueError('ZIP 含链接、特殊文件或加密文件：' + repr(name))
        if (stat.S_ISDIR(mode) and not directory) or (stat.S_ISREG(mode) and directory):
            raise ValueError('ZIP 文件类型与路径不一致：' + repr(name))
        for length in range(1, len(parts) + 1):
            current = '/'.join(parts[:length])
            key = unicodedata.normalize('NFC', current).casefold()
            kind = 'dir' if length < len(parts) or directory else 'file'
            previous = seen.get(key)
            if previous and (previous[0] != kind or previous[1] != current or kind == 'file'):
                raise ValueError('ZIP 含重复或冲突路径：' + repr(name))
            seen[key] = (kind, current)
    return entries


def extract_client(zip_path, destination):
    zip_path = checked_file(zip_path, '完整客户端 ZIP')
    destination = Path(destination)
    with zipfile.ZipFile(zip_path) as archive:
        entries = safe_zip_entries(archive)
        for item in entries:
            target = destination.joinpath(*item.filename.rstrip('/').split('/'))
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    return destination


def write_sfx(prefix, payload, output):
    output = Path(output)
    if output.suffix.casefold() != '.exe':
        raise ValueError('输出文件必须是 .exe。')
    if output.exists() or output.is_symlink():
        raise FileExistsError('输出文件已经存在：' + str(output))
    payload = checked_file(payload, '7z 载荷')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation makes an intervening creator fail safely as well.
    with output.open('xb') as target:
        target.write(prefix)
        with payload.open('rb') as source:
            shutil.copyfileobj(source, target, 1024 * 1024)
    return output


def run_7zr(tool, arguments, cwd):
    result = subprocess.run([str(tool), *arguments], cwd=str(cwd), capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=900)
    if result.returncode:
        raise RuntimeError('7zr 失败：' + (result.stderr or result.stdout)[-3000:])


def build_sfx(client_zip, official, seven_zr, seven_zip_license, lgpl_license, output):
    output = Path(output)
    if output.suffix.casefold() != '.exe':
        raise ValueError('输出文件必须是 .exe。')
    if output.exists() or output.is_symlink():
        raise FileExistsError('输出文件已经存在：' + str(output))
    prefix = verified_prefix(official)
    tool = verified_seven_zr(seven_zr)
    zip_license, lgpl = license_inputs(seven_zip_license, lgpl_license)
    with tempfile.TemporaryDirectory(prefix='maazmxy4-sfx-') as temp:
        temporary = Path(temp)
        stage = temporary/'client'
        stage.mkdir()
        extract_client(client_zip, stage)
        validate_client(stage)
        (stage/LICENSE_NAMES[0]).write_bytes(zip_license)
        (stage/LICENSE_NAMES[1]).write_bytes(lgpl)
        (stage/LICENSE_NAMES[2]).write_text(
            'This self-extracting package uses 7-Zip by Igor Pavlov.\n'
            '7-Zip is licensed under the GNU LGPL. Source: https://www.7-zip.org/\n'
            'See LICENSE-7zip.txt and LICENSE-LGPL-2.1.txt.\n', encoding='utf-8')
        payload = temporary/'payload.7z'
        run_7zr(tool, ['a', '-t7z', '-m0=LZMA2', '-mx=3', '-mmt=2', '-bd', '-sse',
                       str(payload), '*'], stage)
        run_7zr(tool, ['t', '-bd', str(payload)], temporary)
        return write_sfx(prefix, payload, output)


def main():
    parser = argparse.ArgumentParser(description='从完整客户端 ZIP 构建 7-Zip 自解压 EXE')
    parser.add_argument('--client-zip', required=True)
    parser.add_argument('--official-sfx', required=True)
    parser.add_argument('--seven-zr', required=True)
    parser.add_argument('--seven-zip-license', required=True)
    parser.add_argument('--lgpl-license', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    result = build_sfx(args.client_zip, args.official_sfx, args.seven_zr,
                       args.seven_zip_license, args.lgpl_license, args.out)
    print(f'自解压包：{result}\nSHA-256：{sha256_file(result)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
