import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import bundled_box


class BoxFixture:
    """Tiny non-executable PE headers and AIR metadata, never a real game."""
    def __init__(self, directory, machine=0x8664):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        spec = json.loads(bundled_box.SPEC.read_text(encoding='utf8'))
        self.spec = directory / 'box.json'
        self.archive = directory / 'box.zip'
        pe = bytearray(256)
        pe[:2] = b'MZ'; struct.pack_into('<I', pe, 0x3c, 128)
        pe[128:132] = b'PE\0\0'; struct.pack_into('<H', pe, 132, machine)
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for item in spec['files']:
                raw = bytes(pe) if item['path'].endswith(('.exe', '.dll')) else b'public fixture'
                if item['path'].endswith('application.xml'):
                    raw = b'<application><id>dusk.zmBox</id><architecture>64</architecture><versionNumber>1.9</versionNumber></application>'
                item['sha256'] = hashlib.sha256(raw).hexdigest()
                archive.writestr(item['source'], raw)
        spec['sha256'] = bundled_box.sha256(self.archive)
        self.spec.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf8')
        (directory / 'NOTICE-box.txt').write_bytes(bundled_box.SPEC.with_name('NOTICE-box.txt').read_bytes())


class BundledBoxTests(unittest.TestCase):
    def test_complete_box_layout_and_manifest_ignore_adjacent_user_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = BoxFixture(root / 'source')
            (root / 'source/private.json').write_text('private')
            with patch.object(bundled_box, 'SPEC', fixture.spec):
                files = bundled_box.assemble(fixture.archive, root / 'client')
                for source, relative in files:
                    target = root / 'client' / relative
                    if target != source: target.write_bytes(source.read_bytes())
                result = bundled_box.verify(root / 'client')
                self.assertEqual(result['exe_path'], '造梦盒子/zmBox.exe')
                self.assertEqual(len(files), 9)
                self.assertFalse(any('private' in name for _, name in files))
                runtime = root / 'client/造梦盒子/Adobe AIR/Versions/1.0/Adobe AIR.dll'
                runtime.unlink()
                with self.assertRaisesRegex(ValueError, '缺失'): bundled_box.verify(root / 'client')

    def test_wrong_architecture_or_changed_download_cannot_enter_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = BoxFixture(root / 'source', machine=0x14c)
            with patch.object(bundled_box, 'SPEC', fixture.spec):
                with self.assertRaisesRegex(ValueError, 'x64'):
                    bundled_box.assemble(fixture.archive, root / 'client')
                with fixture.archive.open('ab') as stream: stream.write(b'changed')
                with self.assertRaisesRegex(ValueError, '摘要'):
                    bundled_box.assemble(fixture.archive, root / 'other')


if __name__ == '__main__': unittest.main()
