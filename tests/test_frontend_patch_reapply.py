"""The pinned-source preparation must remain repeatable with overlapping patches."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')


@unittest.skipUnless(POWERSHELL, 'PowerShell is required for the frontend preparation script')
class FrontendPatchReapplyTests(unittest.TestCase):
    def test_overlapping_patches_repeat_and_invalid_upstream_anchors_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='frontend-patches-') as temporary:
            root = Path(temporary)
            (root / 'tools').mkdir()
            (root / 'frontend/patches').mkdir(parents=True)
            (root / '.runtime-cache').mkdir()
            shutil.copy2(ROOT / 'tools/build_frontend.ps1', root / 'tools/build_frontend.ps1')
            (root / 'icon.ico').write_bytes(b'test icon')

            commit = 'fixture'
            archive = root / '.runtime-cache/mfa-source-1080d3e.zip'
            prefix = f'MFAAvalonia-{commit}/'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr(prefix + 'MFAAvalonia.Desktop/Desktop.csproj', '<Project/>')
                bundle.writestr(prefix + 'MFAAvalonia/Assets/placeholder', '')
                bundle.writestr(prefix + 'MFAAvalonia/Example.cs', 'before original after\n')
                bundle.writestr(prefix + 'MFAAvalonia/Example2.cs', 'begin second end\n')

            patches = [
                {'description': 'first', 'path': 'MFAAvalonia/Example.cs',
                 'find': 'original', 'replace': 'original patched'},
                {'description': 'later', 'path': 'MFAAvalonia/Example.cs',
                 'find': 'before original patched after', 'replace': 'before final after'},
                {'description': 'second', 'path': 'MFAAvalonia/Example2.cs',
                 'find': 'begin second end', 'replace': 'begin second patched end'},
                {'description': 'nested', 'path': 'MFAAvalonia/Example2.cs',
                 'find': 'second patched', 'replace': 'second final'},
            ]
            patch_names = []
            for index, patch in enumerate(patches):
                relative = f'frontend/patches/{index}.json'
                (root / relative).write_text(json.dumps(patch), encoding='utf8')
                patch_names.append(relative)
            manifest = {
                'commit': commit,
                'cache_directory': '.runtime-cache/source',
                'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'archive_url': 'https://example.invalid/not-used.zip',
                'project': 'MFAAvalonia.Desktop/Desktop.csproj',
                'overlay_directory': 'frontend/absent-overlay',
                'application_icon': 'icon.ico',
                'patches': patch_names,
            }
            (root / 'frontend/upstream.json').write_text(json.dumps(manifest), encoding='utf8')

            def prepare():
                return subprocess.run(
                    [POWERSHELL, '-NoProfile', '-File', str(root / 'tools/build_frontend.ps1'), '-PrepareOnly'],
                    cwd=root, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=60,
                )

            for _ in range(2):
                result = prepare()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((root / '.runtime-cache/source/MFAAvalonia/Example.cs').read_text(),
                                 'before final after\n')
                self.assertEqual((root / '.runtime-cache/source/MFAAvalonia/Example2.cs').read_text(),
                                 'begin second final end\n')

            (root / '.runtime-cache/source/MFAAvalonia/Example.cs').write_text('before corrupted after\n')
            result = prepare()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / '.runtime-cache/source/MFAAvalonia/Example.cs').read_text(),
                             'before final after\n')
            # Cached output is disposable; an invalid patch against the verified
            # upstream source must still fail without mutating the valid output.
            patches[0]['find'] = 'missing upstream anchor'
            (root / patch_names[0]).write_text(json.dumps(patches[0]), encoding='utf8')
            result = prepare()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Patch anchor is absent or ambiguous', result.stdout + result.stderr)
            self.assertEqual((root / '.runtime-cache/source/MFAAvalonia/Example.cs').read_text(),
                             'before final after\n')


@unittest.skipUnless(POWERSHELL, 'PowerShell is required for the frontend build')
class FrontendBuildCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='frontend-cache-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cache = self.root / '.runtime-cache/source'
        self.patches = []
        self.upstream = {'MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj': '<Project/>',
                         'MFAAvalonia/Assets/logo.ico': 'upstream icon'}
        self.manifest = dict(commit='f' * 40, archive_sha256='', archive_url='https://invalid.invalid/offline.zip',
            cache_directory='.runtime-cache/source', project='MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj',
            overlay_directory='frontend/ScriptStudio', application_icon='icon.ico', patches=[])
        self.write('tools/build_frontend.ps1', (ROOT/'tools/build_frontend.ps1').read_bytes())
        self.write('icon.ico', b'fixture icon; never executed')
        self.write('frontend/ScriptStudio/Services/Fixture.cs', b'// overlay fixture')

    def write(self, name, content):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf8'))
        return path

    def add_patch(self, patch, original):
        name = f'frontend/patches/{len(self.patches):02d}.json'
        self.patches.append(patch)
        self.manifest['patches'].append(name)
        self.upstream.setdefault(patch['path'], original)
        self.write(name, json.dumps(patch, ensure_ascii=False))

    def prepare_fixture(self):
        archive = self.root/'.runtime-cache/mfa-source-1080d3e.zip'
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, 'w') as output:
            for name, content in self.upstream.items():
                output.writestr('MFAAvalonia-'+self.manifest['commit']+'/'+name, content)
        self.manifest['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.save_manifest()

    def save_manifest(self):
        self.write('frontend/upstream.json', json.dumps(self.manifest, ensure_ascii=False))

    def prepare(self, *, shell=POWERSHELL, succeed=True):
        environment = dict(os.environ)
        if Path(shell).name.lower() == 'powershell.exe':
            # A parent PowerShell 7 process can export a module path that hides
            # 5.1's own built-ins. Let Windows PowerShell initialize its defaults.
            for name in list(environment):
                if name.lower() == 'psmodulepath': del environment[name]
        result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File',
            str(self.root/'tools/build_frontend.ps1'), '-PrepareOnly'], cwd=self.root,
            env=environment, capture_output=True, encoding='utf8', errors='replace', timeout=90)
        if succeed:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def menu_fixtures(self):
        for name in ('34-game-database-navigation.json', '37-task-description-in-settings.json'):
            patch = json.loads((ROOT/'frontend/patches'/name).read_text(encoding='utf8'))
            self.add_patch(patch, patch['find'])
        self.prepare_fixture()

    def test_old_and_already_duplicated_caches_match_the_clean_build(self):
        self.menu_fixtures()
        self.prepare()
        clean = {p['path']: (self.cache/p['path']).read_bytes() for p in self.patches}
        # These are the exact old replacement texts that contain the original
        # anchor. The former builder inserted the new replacement inside them.
        for state in ('previous', 'duplicated', 'partial'):
            for p in self.patches:
                old = p['previous_replacements'][-1]
                text = old if state == 'previous' else old.replace(p['find'], p['replace'])
                if state == 'partial': text = 'interrupted generated output\n' + text[:50]
                self.write('.runtime-cache/source/'+p['path'], text)
            self.prepare()
            for p in self.patches:
                self.assertEqual((self.cache/p['path']).read_bytes(), clean[p['path']], (state, p['path']))

    def test_repeat_build_preserves_target_timestamps_and_compiler_caches(self):
        self.menu_fixtures(); self.prepare()
        target = self.cache/self.patches[0]['path']
        before = target.stat().st_mtime_ns
        preserved = self.write('.runtime-cache/source/MFAAvalonia/obj/assets.sentinel', b'compiler cache')
        other = self.write('.runtime-cache/source/unmanaged.sentinel', b'not generated by a patch')
        self.prepare()
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(preserved.read_bytes(), b'compiler cache')
        self.assertEqual(other.read_bytes(), b'not generated by a patch')

    def test_changed_and_removed_patches_follow_manifest_order(self):
        first = dict(description='outer', path='MFAAvalonia/A.cs', find='ANCHOR', replace='before ANCHOR after')
        second = dict(description='inner', path='MFAAvalonia/A.cs', find='ANCHOR', replace='FINAL')
        self.add_patch(first, 'ANCHOR'); self.add_patch(second, 'ANCHOR'); self.prepare_fixture(); self.prepare()
        self.assertEqual((self.cache/first['path']).read_text(), 'before FINAL after')
        self.manifest['patches'] = self.manifest['patches'][1:]; self.save_manifest(); self.prepare()
        self.assertEqual((self.cache/first['path']).read_text(), 'FINAL')
        self.manifest['patches'] = []; self.save_manifest(); self.prepare()
        self.assertEqual((self.cache/first['path']).read_text(), 'ANCHOR')

    def test_invalid_anchor_does_not_partially_rewrite_cached_files(self):
        self.menu_fixtures(); self.prepare()
        originals = {p['path']: (self.cache/p['path']).read_bytes() for p in self.patches}
        first = dict(self.patches[0], replace='CHANGED FIRST PATCH')
        last = dict(self.patches[1], find='missing anchor')
        self.write(self.manifest['patches'][0], json.dumps(first, ensure_ascii=False))
        self.write(self.manifest['patches'][1], json.dumps(last, ensure_ascii=False))
        result = self.prepare(succeed=False)
        self.assertIn('anchor', result.lower())
        for name, content in originals.items(): self.assertEqual((self.cache/name).read_bytes(), content)

    def test_archive_checksum_and_path_checks_remain_enforced(self):
        self.menu_fixtures(); self.prepare()
        expected_hash = self.manifest['archive_sha256']
        self.manifest['archive_sha256'] = '0' * 64; self.save_manifest()
        self.assertIn('checksum mismatch', self.prepare(succeed=False))
        self.manifest['archive_sha256'] = expected_hash; self.save_manifest()
        patch = dict(self.patches[0], path='../outside.cs')
        self.write(self.manifest['patches'][0], json.dumps(patch))
        self.assertIn('Path must stay inside', self.prepare(succeed=False))
        self.assertFalse((self.root/'outside.cs').exists())

    def test_windows_powershell_51_prepares_the_same_utf8_output(self):
        shell = Path(os.environ.get('WINDIR', 'missing'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        if not shell.is_file(): self.skipTest('Windows PowerShell 5.1 is not installed')
        self.menu_fixtures(); self.prepare(shell=str(shell))
        for patch in self.patches:
            self.assertEqual((self.cache/patch['path']).read_bytes(), patch['replace'].encode('utf8'))


if __name__ == '__main__':
    unittest.main()
