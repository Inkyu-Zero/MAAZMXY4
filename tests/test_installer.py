"""Installer payload, identity and destructive cleanup boundaries; no real client."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_task_asset_sync as migration_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import build_installer
spec = importlib.util.spec_from_file_location('install_support',ROOT/'tools/installer/install_support.py')
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = migration_fixture.TaskAssetSyncTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.source,self.target=self.fixture.source,self.fixture.target
        self.output=self.source.parent/'prepared'
        self.write=self.fixture.write; self.raw=self.fixture.bytes
        for root in (self.source,self.target):
            path=root/('assets/interface.json' if root==self.source else 'interface.json')
            doc=json.loads(path.read_bytes()); doc.update(name='MAA造梦西游4',version='2.4.5')
            self.write(path,doc)
        for name in ('MFAAvalonia.exe','agent/main.py','runtimes/python/python.exe'):
            self.raw(self.target/name,b'owned old program')
        self.write(self.source/'installer-files.json',dict(files=['MFAAvalonia.exe','interface.json','agent/main.py']))

    def test_confirmed_client_reuses_directory_and_unknown_always_appends(self):
        self.assertTrue(support.identity(self.target))
        self.assertEqual(support.destination(self.target),self.target)
        for name in ('name','version','task'):
            doc=json.loads((self.target/'interface.json').read_bytes()); before=dict(doc)
            doc[name]=None; self.write(self.target/'interface.json',doc)
            self.assertFalse(support.identity(self.target))
            self.assertEqual(support.destination(self.target),self.target/'MAAZMXY4')
            self.write(self.target/'interface.json',before)
        self.assertEqual(support.destination(self.source/'MAAZMXY4'),self.source/'MAAZMXY4/MAAZMXY4')
        # The daily client can also be a git worktree; .git is not a program
        # file and is never cleaned or used as the app's identity.
        self.raw(self.target/'.git',b'gitdir: outside'); self.assertTrue(support.identity(self.target))

    def test_old_cleanup_preserves_personal_data_and_removes_obsolete_programs_without_backup(self):
        personal=['config/custom.json','recordings/.bindings-extra.json','recordings/.draft.json',
                  'appsettings.json','resource/base/mfa_layout.json','agent/recordings/private.json',
                  '.personal-transfer/pending.json','unrelated.txt']
        for name in personal: self.raw(self.target/name,b'personal sentinel')
        obsolete=['agent/obsolete.py','libs/obsolete.dll','runtimes/old.dll','resource/old.json','updater.py']
        for name in obsolete:self.raw(self.target/name,b'obsolete')
        # A malformed instance is not a valid fixture for task migration.
        self.write(self.target/'config/custom.json',dict(custom='kept'))
        before={name:(self.target/name).read_bytes() for name in personal}
        report=support.prepare(self.source,self.target,self.output)
        self.assertTrue(report['old_client'])
        for name in obsolete:self.assertFalse((self.target/name).exists(),name)
        for name,raw in before.items():self.assertEqual((self.target/name).read_bytes(),raw,name)
        self.assertFalse((self.target/'backup').exists())
        merged=json.loads((self.output/'interface.json').read_bytes())
        self.assertEqual(merged['controller'],self.fixture.before_interface['controller'])

    def test_legacy_script_choice_is_migrated_before_cleanup(self):
        self.fixture.prepare_legacy_recording()
        doc=json.loads((self.target/'interface.json').read_bytes())
        doc.update(name='MAA造梦西游4',version='2.4.2');self.write(self.target/'interface.json',doc)
        report=support.prepare(self.source,self.target,self.output)
        self.assertTrue(report['old_client'])
        self.assertTrue((self.output/'recordings/.bindings.json').is_file())
        self.assertTrue((self.target/'recordings/战斗.json').is_file())

    def test_nonempty_unknown_directory_is_not_cleaned(self):
        self.write(self.target/'interface.json',dict(name='Other app'))
        self.raw(self.target/'agent/keep.py',b'other');self.raw(self.target/'arbitrary.txt',b'keep')
        report=support.prepare(self.source,self.target,self.output)
        self.assertFalse(report['old_client']);self.assertEqual(report['removed'],[])
        self.assertEqual((self.target/'agent/keep.py').read_bytes(),b'other')

    def test_bad_manifest_and_failed_migration_prevent_all_deletion(self):
        old=self.target/'agent/obsolete.py';self.raw(old,b'old')
        self.write(self.source/'installer-files.json',dict(files=['../outside']))
        with self.assertRaises(ValueError):support.prepare(self.source,self.target,self.output)
        self.assertEqual(old.read_bytes(),b'old')
        self.write(self.source/'installer-files.json',dict(files=['agent/main.py']))
        with patch('task_asset_sync.prepare_task_assets',side_effect=ValueError('bad config')):
            with self.assertRaisesRegex(ValueError,'bad config'):support.prepare(self.source,self.target,self.output)
        self.assertEqual(old.read_bytes(),b'old')

    def test_active_client_prevents_cleaning(self):
        self.raw(self.target/'libs/old.dll',b'old')
        with patch.object(support,'ensure_idle',side_effect=ValueError('running')):
            with self.assertRaisesRegex(ValueError,'running'):support.prepare(self.source,self.target,self.output)
        self.assertTrue((self.target/'libs/old.dll').is_file())

    def test_dependency_hash_failure_is_fatal(self):
        binary=self.source/'dependency.exe';binary.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError,'SHA-256'):
            build_installer.verify_binary(binary,dict(sha256='0'*64),signature=False)
        build_installer.verify_binary(binary,dict(sha256=hashlib.sha256(b'corrupted').hexdigest()),signature=False)


if __name__=='__main__':unittest.main()
