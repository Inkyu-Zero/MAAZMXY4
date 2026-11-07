"""Windows script persistence without hard links; only isolated temporary data."""
import errno
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'agent'), str(ROOT/'tools')]
import script_studio_service as studio
import script_library as library
import recording_store as store
import task_asset_sync as sync
import migrate_zaomeng_scripts as migration


def recording():
    return dict(version=1, name='测试录制', builtin_id='test-default', base=[1148, 720],
                duration_ms=100, actions=[dict(t=0, op='keydown', key=65), dict(t=100, op='keyup', key=65)])


@unittest.skipUnless(os.name == 'nt', 'Windows filesystem publication contract')
class FilesystemPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def writers(self):
        raw = b'{"complete":true}\n'
        return {
            'studio': lambda p: studio.atomic_json(p, {'complete': True}, overwrite=False),
            'library': lambda p: store._publish(p, raw, None),
            'resources': lambda p: sync._publish(p, raw, None),
            'migration': lambda p: migration._atomic_write(p, raw),
        }

    def test_all_new_file_writers_work_without_hard_links(self):
        for name, write in self.writers().items():
            with self.subTest(writer=name):
                target = self.root/(name+'.json')
                with patch.object(os, 'link', side_effect=OSError(errno.ENOTSUP, 'hard links unavailable')) as link:
                    write(target)
                self.assertEqual(json.loads(target.read_bytes()), {'complete': True})
                link.assert_not_called()
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_default_copy_rename_and_first_binding_without_hard_links(self):
        bundle = self.root/'agent/recordings'
        bundle.mkdir(parents=True)
        raw = json.dumps(recording(), ensure_ascii=False).encode('utf-8')
        (bundle/'默认.json').write_bytes(raw)
        (bundle/'manifest.json').write_text(json.dumps(dict(version=1, items=[dict(
            id='test-default', file='默认.json', sha256=hashlib.sha256(raw).hexdigest())])), encoding='utf-8')
        directory = self.root/'recordings'
        with patch.object(os, 'link', side_effect=OSError(errno.ENOTSUP, 'hard links unavailable')) as link:
            self.assertEqual(store.ensure_default_recordings(self.root), ['默认.json'])
            revision = hashlib.sha256((directory/'默认.json').read_bytes()).hexdigest()
            library.bind_task_recording(directory, 'test-task', '默认.json', expected_revision=revision)
            copied = store.duplicate_recording(directory, '默认.json', '副本', revision)
            self.assertTrue((directory/copied['file']).is_file())
            store.rename_recording(directory, '默认.json', '新名称', revision)
            self.assertEqual(library.read_task_bindings(directory), {'test-task': '新名称.json'})
            self.assertEqual(store.ensure_default_recordings(self.root), [])
            link.assert_not_called()

    def test_save_over_real_jsonl_worker_without_hard_links(self):
        code = """import errno, os, sys
sys.path.insert(0, sys.argv.pop(1))
def unsupported(*args, **kwargs):
    raise OSError(errno.ENOTSUP, 'hard links unavailable')
os.link = unsupported
import script_studio_service
script_studio_service.main()
"""
        imported = self.root/'待导入.json'
        imported.write_text(json.dumps(recording()), encoding='utf-8')
        requests = [dict(id=1, method='new'), dict(id=2, method='save', params=dict(file='新脚本.json')),
                    dict(id=3, method='load', params=dict(file='新脚本.json')),
                    dict(id=4, method='import', params=dict(path=str(imported))),
                    dict(id=5, method='save', params=dict(file='导入脚本.json')),
                    dict(id=6, method='set_duration', params=dict(duration_ms=300)),
                    dict(id=7, method='save'), dict(id=8, method='shutdown')]
        result = subprocess.run([sys.executable, '-B', '-E', '-s', '-X', 'utf8', '-c', code,
            str(ROOT/'agent'), '--root', str(self.root)],
            input=''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in requests),
            text=True, encoding='utf-8', capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], list(range(1, 8)))
        self.assertTrue(all(r['ok'] for r in replies), replies)
        self.assertFalse(replies[1]['result']['dirty'])
        self.assertTrue((self.root/'recordings/新脚本.json').is_file())
        saved = json.loads((self.root/'recordings/导入脚本.json').read_bytes())
        self.assertEqual(saved['duration_ms'], 300)
        self.assertEqual(saved['actions'], recording()['actions'])

    def test_simultaneous_saves_publish_one_complete_winner(self):
        target = self.root/'same-name.json'
        barrier, rename = threading.Barrier(2), os.rename
        def simultaneous(source, destination):
            barrier.wait(timeout=5)
            return rename(source, destination)
        payloads = [dict(writer=n, content=str(n)*100000) for n in range(2)]
        def save(value):
            try:
                studio.atomic_json(target, value, overwrite=False)
                return 'saved'
            except FileExistsError:
                return 'conflict'
        with patch.object(os, 'rename', side_effect=simultaneous), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, payloads))
        self.assertCountEqual(outcomes, ['saved', 'conflict'])
        self.assertIn(json.loads(target.read_bytes()), payloads)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_non_windows_backend_still_uses_exclusive_link(self):
        # POSIX rename would overwrite; validate dispatch without changing the
        # host platform or requiring the test volume itself to support links.
        with patch.object(library, 'os', SimpleNamespace(name='posix', link=lambda a, b: None)):
            with patch.object(library.os, 'link', side_effect=FileExistsError('existing file')) as link:
                with self.assertRaises(FileExistsError):
                    library.publish_new_file('prepared.tmp', 'existing.json')
                link.assert_called_once_with('prepared.tmp', 'existing.json')

    def test_concurrent_destination_creation_is_not_overwritten_by_any_writer(self):
        rename = os.rename
        for name, write in self.writers().items():
            with self.subTest(writer=name):
                target = self.root/(name+'.json')
                def competing_writer(source, destination):
                    self.assertEqual(Path(source).parent, target.parent)
                    json.loads(Path(source).read_bytes())  # Complete before publication.
                    target.write_bytes(b'other writer')
                    return rename(source, destination)
                with patch.object(os, 'rename', side_effect=competing_writer) as publish:
                    with self.assertRaises(FileExistsError):
                        write(target)
                publish.assert_called_once()
                self.assertEqual(target.read_bytes(), b'other writer')
                self.assertFalse(list(self.root.glob('*.tmp')))

    def test_publish_failure_preserves_unsaved_document(self):
        service = studio.StudioService(self.root)
        self.addCleanup(service.close)
        before = service.dispatch('new')
        with patch.object(os, 'rename', side_effect=PermissionError('read-only destination')) as publish:
            with self.assertRaisesRegex(PermissionError, 'read-only'):
                service.dispatch('save', dict(file='不可写.json'))
        publish.assert_called_once()
        self.assertEqual(service.snapshot(), before)
        self.assertFalse((self.root/'recordings/不可写.json').exists())
        self.assertFalse(list((self.root/'recordings').glob('*.tmp')))

    def test_failed_flush_publishes_nothing(self):
        target = self.root/'disk-full.json'
        with patch.object(os, 'fsync', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                studio.atomic_json(target, recording(), overwrite=False)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
