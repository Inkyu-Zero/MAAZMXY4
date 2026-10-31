"""Test SFX input safety without running downloaded binaries."""
import hashlib
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import build_sfx


class BuildSfxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, content):
        target = self.root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def archive(self, name, entries):
        target = self.root/name
        with zipfile.ZipFile(target, 'w') as output:
            for entry, content in entries:
                output.writestr(entry, content)
        return target

    def test_valid_archive_preserves_unicode_contents(self):
        archive = self.archive('ok.zip', [('agent/code.py', b'ok'), ('资源/脚本.json', b'{}'),
                                          ('runtimes/win-x64/native/plugins/', b'')])
        destination = self.root/'extracted'
        destination.mkdir()
        build_sfx.extract_client(archive, destination)
        self.assertEqual((destination/'资源/脚本.json').read_bytes(), b'{}')
        self.assertEqual((destination/'agent/code.py').read_bytes(), b'ok')
        self.assertTrue((destination/'runtimes/win-x64/native/plugins').is_dir())

    def test_rejects_unsafe_and_private_paths_before_extracting_anything(self):
        invalid = [
            '../escape', '/absolute', 'C:/windows/file', 'agent\\..\\escape',
            'folder//name', 'folder/../name', 'folder/file:stream', 'folder/name.',
            'folder/name ', 'folder/CON.txt', 'folder/COM¹.log', 'folder/CON .txt',
            'folder/AGENTS.md', 'config/user.json', 'recordings/user.json',
            'appsettings.json', 'agent/recordings/.bindings.json', '.git/config',
            '.personal-transfer/pending/config/config.json',
            'LICENSE-7zip.txt',
        ]
        for index, name in enumerate(invalid):
            with self.subTest(name=name):
                archive = self.archive(f'bad-{index}.zip', [('valid.txt', b'valid'), (name, b'bad')])
                destination = self.root/f'out-{index}'
                destination.mkdir()
                with self.assertRaises(ValueError):
                    build_sfx.extract_client(archive, destination)
                self.assertEqual(list(destination.iterdir()), [])

    def test_rejects_case_unicode_and_file_directory_collisions(self):
        variants = [
            [('agent/A.txt', b'1'), ('agent/a.TXT', b'2')],
            [('folder', b'1'), ('folder/file', b'2')],
            [('resource/Café.txt', b'1'), ('resource/Cafe\u0301.txt', b'2')],
            [('Folder/a.txt', b'1'), ('folder/b.txt', b'2')],
        ]
        for index, entries in enumerate(variants):
            with self.subTest(entries=entries):
                archive = self.archive(f'collision-{index}.zip', entries)
                with zipfile.ZipFile(archive) as source, self.assertRaises(ValueError):
                    build_sfx.safe_zip_entries(source)

    def test_rejects_symlink_archive_entry(self):
        info = zipfile.ZipInfo('resource/link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.archive('link.zip', [(info, b'../../outside')])
        with zipfile.ZipFile(archive) as source, self.assertRaises(ValueError):
            build_sfx.safe_zip_entries(source)

    def test_official_prefix_and_7zr_require_pinned_bytes(self):
        prefix = b'MZ fixture prefix'
        official = self.write('official.exe', prefix + b"7z\xbc\xaf\x27\x1c" + b'archive')
        tool = self.write('7zr.exe', b'tool fixture')
        with patch.object(build_sfx, 'OFFICIAL_SHA256', hashlib.sha256(official.read_bytes()).hexdigest()), \
             patch.object(build_sfx, 'SFX_PREFIX_LENGTH', len(prefix)), \
             patch.object(build_sfx, 'SFX_PREFIX_SHA256', hashlib.sha256(prefix).hexdigest()), \
             patch.object(build_sfx, 'SEVEN_ZR_SHA256', hashlib.sha256(tool.read_bytes()).hexdigest()):
            self.assertEqual(build_sfx.verified_prefix(official), prefix)
            self.assertEqual(build_sfx.verified_seven_zr(tool), tool.resolve())
            tool.write_bytes(b'replaced')
            with self.assertRaisesRegex(ValueError, '7zr.exe SHA-256'):
                build_sfx.verified_seven_zr(tool)
            official.write_bytes(prefix + b'not-7z')
            with self.assertRaisesRegex(ValueError, '官方 v2.3.1'):
                build_sfx.verified_prefix(official)

    def test_empty_or_wrong_licenses_are_rejected(self):
        zip_license = self.write('7zip.txt', b'7-Zip\nGNU LGPL\n')
        lgpl = self.write('lgpl.txt', b'GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1\n')
        self.assertEqual(build_sfx.license_inputs(zip_license, lgpl), (zip_license.read_bytes(), lgpl.read_bytes()))
        lgpl.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, '为空'):
            build_sfx.license_inputs(zip_license, lgpl)
        lgpl.write_bytes(b'not a license')
        with self.assertRaisesRegex(ValueError, 'LGPL 2.1'):
            build_sfx.license_inputs(zip_license, lgpl)

    def test_sfx_output_does_not_overwrite_existing_file(self):
        payload = self.write('payload.7z', b'payload')
        output = self.write('output.exe', b'keep existing bytes')
        with self.assertRaises(FileExistsError):
            build_sfx.write_sfx(b'prefix', payload, output)
        self.assertEqual(output.read_bytes(), b'keep existing bytes')
        with self.assertRaises(ValueError):
            build_sfx.write_sfx(b'prefix', payload, self.root/'wrong.zip')

    def test_build_includes_licenses_and_verifies_client_before_compression(self):
        prefix = b'MZ fixture prefix'
        official = self.write('official.exe', prefix + b"7z\xbc\xaf\x27\x1c" + b'original')
        tool = self.write('7zr.exe', b'tool fixture')
        zip_license = self.write('7zip.txt', b'7-Zip\nGNU LGPL\n')
        lgpl = self.write('lgpl.txt', b'GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1\n')
        archive = self.archive('client.zip', [('interface.json', b'{}')])
        output = self.root/'candidate.exe'
        calls = []
        def validate(stage):
            self.assertTrue((stage/'interface.json').is_file())
            self.assertFalse((stage/'LICENSE-7zip.txt').exists())
            calls.append('validate')
        def run_tool(tool_path, args, cwd):
            if args[0] == 'a':
                self.assertEqual(calls, ['validate'])
                self.assertEqual((cwd/'LICENSE-7zip.txt').read_bytes(), zip_license.read_bytes())
                self.assertEqual((cwd/'LICENSE-LGPL-2.1.txt').read_bytes(), lgpl.read_bytes())
                self.assertIn('https://www.7-zip.org/', (cwd/'NOTICE-7zip.txt').read_text(encoding='utf8'))
                Path(args[-2]).write_bytes(b'7z fixture payload')
            calls.append(args[0])
        with patch.object(build_sfx, 'OFFICIAL_SHA256', hashlib.sha256(official.read_bytes()).hexdigest()), \
             patch.object(build_sfx, 'SFX_PREFIX_LENGTH', len(prefix)), \
             patch.object(build_sfx, 'SFX_PREFIX_SHA256', hashlib.sha256(prefix).hexdigest()), \
             patch.object(build_sfx, 'SEVEN_ZR_SHA256', hashlib.sha256(tool.read_bytes()).hexdigest()), \
             patch.object(build_sfx, 'validate_client', side_effect=validate), \
             patch.object(build_sfx, 'run_7zr', side_effect=run_tool):
            build_sfx.build_sfx(archive, official, tool, zip_license, lgpl, output)
        self.assertEqual(calls, ['validate', 'a', 't'])
        self.assertEqual(output.read_bytes(), prefix+b'7z fixture payload')


if __name__ == '__main__':
    unittest.main()
