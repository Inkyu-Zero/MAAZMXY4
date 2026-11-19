import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))
import recording_folders as folders
import recording_store as store
from script_library import bind_task_recording, read_task_bindings, resolve_task_recording
from script_studio_service import StudioService


class RecordingFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'recordings'
        self.directory.mkdir()
        for name in ('one', 'two', 'other'):
            self.write(name)
        self.service = StudioService(self.root)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, name):
        value = dict(version=1, name=name, base=[1148, 720], duration_ms=200,
                     actions=[dict(t=0, op='keydown', key=65), dict(t=200, op='keyup', key=65)])
        (self.directory / (name + '.json')).write_text(json.dumps(value), encoding='utf8')

    def revision(self, name):
        return hashlib.sha256((self.directory / name).read_bytes()).hexdigest()

    def folder_revision(self):
        return self.service.list()['folder_revision']

    def create(self, name='关卡'):
        return self.service.dispatch('create_folder', dict(name=name, folder_revision=self.folder_revision()))['folder_id']

    def move(self, name, ident):
        return self.service.dispatch('move_to_folder', dict(file=name, folder_id=ident, folder_revision=self.folder_revision()))

    def deletion(self, ident):
        listing = self.service.list()
        return dict(folder_id=ident, folder_revision=listing['folder_revision'],
                    revisions={item['file']: item['revision'] for item in listing['items'] if item.get('folder_id') == ident})

    def test_move_and_folder_rename_preserve_script_hash_and_binding_across_reopen(self):
        bind_task_recording(self.directory, 'stage', 'one.json', expected_revision=self.revision('one.json'))
        original = {p.name: p.read_bytes() for p in self.directory.glob('*.json')}
        ident = self.create()
        self.move('one.json', ident)
        self.service.dispatch('rename_folder', dict(folder_id=ident, name='日常', folder_revision=self.folder_revision()))
        self.service.close()
        self.service = StudioService(self.root)
        listing = self.service.list()
        self.assertEqual(listing['folders'][0]['name'], '日常')
        self.assertEqual(next(item for item in listing['items'] if item['file'] == 'one.json')['folder_id'], ident)
        self.assertEqual(resolve_task_recording(self.directory, 'stage').name, 'one.json')
        for name, raw in original.items(): self.assertEqual((self.directory / name).read_bytes(), raw)
        self.move('one.json', None)
        self.assertEqual(folders.snapshot(self.directory)[0]['members'], {})

    def test_script_rename_keeps_folder_and_unknown_index_fields(self):
        ident = self.create()
        self.move('one.json', ident)
        index = self.directory / folders.INDEX
        value = json.loads(index.read_text(encoding='utf8'))
        value['future'] = {'keep': True}
        value['folders'][ident]['future'] = 7
        index.write_text(json.dumps(value), encoding='utf8')
        store.rename_recording(self.directory, 'one.json', 'renamed', self.revision('one.json'))
        value, _ = folders.state(self.directory)
        self.assertEqual(value['members'], {'renamed.json': ident})
        self.assertEqual(value['future'], {'keep': True})
        self.assertEqual(value['folders'][ident]['future'], 7)

    def test_delete_group_clears_editor_unbinds_all_and_never_reseeds_defaults(self):
        bundle = self.root / 'agent/recordings'
        bundle.mkdir(parents=True)
        raw = (self.directory / 'one.json').read_bytes()
        (bundle / 'one.json').write_bytes(raw)
        (bundle / 'manifest.json').write_text(json.dumps(dict(version=1, items=[
            dict(id='default', file='one.json', sha256=hashlib.sha256(raw).hexdigest())])), encoding='utf8')
        store.ensure_default_recordings(self.root)
        bind_task_recording(self.directory, 'first', 'one.json', expected_revision=self.revision('one.json'))
        bind_task_recording(self.directory, 'second', 'two.json', expected_revision=self.revision('two.json'))
        bind_task_recording(self.directory, 'kept', 'other.json', expected_revision=self.revision('other.json'))
        ident = self.create()
        for name in ('one.json', 'two.json'): self.move(name, ident)
        self.service.dispatch('load', {'file': 'one.json'})
        result = self.service.dispatch('delete_folder', self.deletion(ident))
        self.assertTrue(result['document']['empty'])
        self.assertEqual(set(result['deleted']), {'one.json', 'two.json'})
        self.assertEqual(read_task_bindings(self.directory), {'kept': 'other.json'})
        self.assertEqual(store.ensure_default_recordings(self.root), [])
        self.assertFalse((self.directory / 'one.json').exists())
        self.assertEqual(self.service.list()['folders'], [])
        self.assertTrue(all(Path(path).is_file() for path in result['recovery_files'].values()))

    def test_group_move_failure_rolls_back_files_indexes_and_bindings(self):
        ident = self.create()
        for name in ('one.json', 'two.json'): self.move(name, ident)
        bind_task_recording(self.directory, 'first', 'one.json', expected_revision=self.revision('one.json'))
        before = {p.name: p.read_bytes() for p in self.directory.glob('*.json')}
        replace = os.replace
        injected = []
        def fail_second(source, target):
            if Path(source).name == 'two.json' and Path(target).parent.name == '.trash':
                injected.append(True)
                raise OSError('injected second move failure')
            return replace(source, target)
        with patch.object(store.os, 'replace', side_effect=fail_second), self.assertRaisesRegex(OSError, 'injected'):
            self.service.dispatch('delete_folder', self.deletion(ident))
        self.assertEqual(injected, [True])
        for name, raw in before.items(): self.assertEqual((self.directory / name).read_bytes(), raw)
        self.assertEqual(read_task_bindings(self.directory), {'first': 'one.json'})

    def test_delete_confirmation_does_not_include_later_moves_or_edits(self):
        ident = self.create()
        self.move('one.json', ident)
        pending = self.deletion(ident)
        self.move('two.json', ident)
        with self.assertRaises(ValueError): self.service.dispatch('delete_folder', pending)
        pending = self.deletion(ident)
        (self.directory / 'one.json').write_text('{}', encoding='utf8')
        with self.assertRaises(ValueError): self.service.dispatch('delete_folder', pending)
        self.assertTrue((self.directory / 'one.json').exists())
        self.assertTrue((self.directory / 'two.json').exists())

    def test_bad_folder_index_is_advisory_for_existing_load_and_rename(self):
        (self.directory / folders.INDEX).write_text('{broken', encoding='utf8')
        self.assertIn('folders', self.service.list()['binding_errors'])
        self.assertEqual(self.service.dispatch('load', {'file': 'one.json'})['file'], 'one.json')
        store.rename_recording(self.directory, 'one.json', 'renamed', self.revision('one.json'))
        self.assertEqual((self.directory / folders.INDEX).read_text(encoding='utf8'), '{broken')

    def test_single_delete_removes_membership_and_empty_folder_can_be_deleted(self):
        ident = self.create()
        self.move('one.json', ident)
        store.delete_recording(self.directory, 'one.json', self.revision('one.json'))
        self.assertEqual(self.service.list()['folders'][0]['count'], 0)
        self.service.dispatch('delete_folder', self.deletion(ident))
        self.assertEqual(self.service.list()['folders'], [])

    def test_group_delete_preserves_unknown_fields_of_unaffected_bindings(self):
        index = self.directory / '.bindings.json'
        index.write_text(json.dumps({'version': 2, 'future': {'keep': True}, 'tasks': {
            'removed': {'file': 'one.json', 'mode': 'pipeline'},
            'kept': {'file': 'other.json', 'mode': 'pipeline', 'future': {'instance': 'kept'}}}}), encoding='utf8')
        ident = self.create()
        self.move('one.json', ident)
        self.service.dispatch('delete_folder', self.deletion(ident))
        remaining = json.loads(index.read_text(encoding='utf8'))
        self.assertEqual(remaining.get('future'), {'keep': True})
        self.assertEqual(remaining['tasks']['kept']['future'], {'instance': 'kept'})
        self.assertNotIn('removed', remaining['tasks'])

    def test_jsonl_worker_create_move_and_delete_over_real_process_boundary(self):
        with subprocess.Popen([sys.executable, '-B', '-u', '-X', 'utf8', str(ROOT / 'agent/script_studio_service.py'),
                               '--root', str(self.root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding='utf8') as worker:
            sequence = 0
            def call(method, params=None):
                nonlocal sequence
                sequence += 1
                worker.stdin.write(json.dumps(dict(id=sequence, method=method, params=params or {})) + '\n')
                worker.stdin.flush()
                response = json.loads(worker.stdout.readline())
                self.assertEqual(response['id'], sequence)
                self.assertTrue(response['ok'], response)
                return response['result']
            try:
                listing = call('list')
                ident = call('create_folder', dict(name='IPC', folder_revision=listing['folder_revision']))['folder_id']
                listing = call('list')
                call('move_to_folder', dict(file='one.json', folder_id=ident, folder_revision=listing['folder_revision']))
                listing = call('list')
                selected = next(item for item in listing['items'] if item['file'] == 'one.json')
                self.assertEqual(selected['folder_id'], ident)
                call('delete_folder', dict(folder_id=ident, folder_revision=listing['folder_revision'],
                                          revisions={'one.json': selected['revision']}))
                self.assertEqual(len(call('list')['items']), 2)
            finally:
                worker.stdin.write('{"method":"shutdown"}\n'); worker.stdin.flush()
                worker.wait(timeout=10)
            self.assertEqual(worker.returncode, 0, worker.stderr.read())


if __name__ == '__main__': unittest.main()
