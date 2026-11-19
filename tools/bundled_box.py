"""Assemble the pinned official x64 captive-AIR ZIP into the full client."""
import hashlib
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
import zipfile

SPEC = Path(__file__).resolve().parent / 'installer/box.json'
FOLDER = '造梦盒子'
MANIFEST = 'bundled-box.json'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_x64(raw):
    if len(raw) < 64 or raw[:2] != b'MZ': raise ValueError('盒子程序或 AIR 不是 PE 文件。')
    offset = struct.unpack_from('<I', raw, 0x3c)[0]
    if offset + 6 > len(raw) or raw[offset:offset + 4] != b'PE\0\0' or struct.unpack_from('<H', raw, offset + 4)[0] != 0x8664:
        raise ValueError('盒子程序及 AIR 必须均为 Windows x64。')


def assemble(archive, destination):
    spec = json.loads(SPEC.read_text(encoding='utf8'))
    if archive is None:
        raise ValueError('完整客户端需要 --box-zip 指定官方造梦盒子 x64 ZIP。')
    archive, destination = Path(archive), Path(destination)
    if sha256(archive) != spec['sha256']:
        raise ValueError('造梦盒子 ZIP 与固定官方摘要不一致。')
    files = []
    with zipfile.ZipFile(archive, metadata_encoding='gbk') as source:
        actual = {item.filename for item in source.infolist() if not item.is_dir()}
        if actual != {item['source'] for item in spec['files']}:
            raise ValueError('官方盒子 ZIP 文件清单不一致。')
        for item in spec['files']:
            raw = source.read(item['source'])
            if hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('盒子文件摘要不一致：' + item['path'])
            if item['path'].lower().endswith(('.exe', '.dll')): require_x64(raw)
            relative = FOLDER + '/' + item['path']
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            files.append((path, relative))
    notice = SPEC.with_name('NOTICE-box.txt')
    files.append((notice, FOLDER + '/NOTICE.txt'))
    manifest = dict(schema=1, version=spec['version'], source_url=spec['url'], source_sha256=spec['sha256'],
                    exe_path=FOLDER + '/zmBox.exe', files=[dict(path=relative, sha256=sha256(path)) for path, relative in files])
    metadata = destination / MANIFEST
    metadata.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    files.append((metadata, MANIFEST))
    verify(destination, manifest, notice_path=notice)
    return files


def verify(root, manifest=None, *, notice_path=None):
    root = Path(root).resolve()
    spec = json.loads(SPEC.read_text(encoding='utf8'))
    manifest = manifest or json.loads((root / MANIFEST).read_text(encoding='utf8'))
    expected = {FOLDER + '/' + item['path']: item['sha256'] for item in spec['files']}
    expected[FOLDER + '/NOTICE.txt'] = sha256(SPEC.with_name('NOTICE-box.txt'))
    if (manifest.get('schema') != 1 or manifest.get('version') != spec['version']
            or manifest.get('source_sha256') != spec['sha256']
            or manifest.get('exe_path') != FOLDER + '/zmBox.exe'
            or {item['path']: item['sha256'] for item in manifest.get('files', [])} != expected):
        raise ValueError('盒子打包清单不符合固定来源。')
    for relative, digest in expected.items():
        path = Path(notice_path) if notice_path is not None and relative.endswith('/NOTICE.txt') else root / relative
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise ValueError('内置盒子文件缺失或摘要变化：' + relative)
        if relative.lower().endswith(('.exe', '.dll')): require_x64(path.read_bytes())
    descriptor = ET.parse(root / FOLDER / 'META-INF/AIR/application.xml').getroot()
    fields = {element.tag.rsplit('}', 1)[-1]: element.text for element in descriptor}
    if fields.get('id') != 'dusk.zmBox' or fields.get('architecture') != '64' or fields.get('versionNumber') != spec['version']:
        raise ValueError('内置盒子 AIR 标识或版本不一致。')
    return manifest
