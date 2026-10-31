"""Atomic multi-selection edits, pairing and undo; no game input or personal files."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from script_studio.model import RecordingDocument
from script_studio_service import StudioService


def document():
    return RecordingDocument.from_dict(dict(version=1, name='批量测试', base=[1148, 720], duration_ms=1000,
        actions=[dict(t=100, op='keydown', key=65, custom='keep'), dict(t=100, op='keyup', key=65),
                 dict(t=100, op='keydown', key=65), dict(t=100, op='keyup', key=65),
                 dict(t=100, op='click', x=20, y=30)],
        editor={'markers': [{'at_ms': 600, 'label': '阶段'}]}))


class BatchTimelineTests(unittest.TestCase):
    def test_move_adjacent_holds_is_atomic_and_preserves_offsets_metadata_and_ids(self):
        original = document()
        operations = original.snapshot()['operations']
        # Moving one at a time would overlap the other A-key hold.
        ids = [operation['id'] for operation in operations[:2]]
        moved = original.move_many(ids, 200)
        self.assertEqual(moved.diagnostics, [])
        old_events, new_events = original.snapshot()['events'], moved.snapshot()['events']
        old = {event['id']: event for event in old_events}
        selected = {ident for operation in operations[:2] for ident in operation['event_ids']}
        for event in new_events:
            self.assertEqual(event['at_ms'], old[event['id']]['at_ms'] + (200 if event['id'] in selected else 0))
        self.assertEqual(original.to_dict()['actions'][0]['custom'], 'keep')
        self.assertEqual(moved.to_dict()['actions'][0]['custom'], 'keep')

    def test_delete_holds_click_and_marker_keeps_unselected_actions_and_duration(self):
        original = document()
        operations = original.snapshot()['operations']
        selected = [operation['id'] for operation in operations if operation['kind'] != 'key_hold']
        result = original.delete_many(selected)
        self.assertEqual(len(result.snapshot()['operations']), 2)
        self.assertEqual(result.duration_ms, 1000)
        self.assertFalse(result.diagnostics)

    def test_invalid_selection_conflict_and_negative_boundary_leave_original_intact(self):
        original = document(); before = deepcopy(original.to_dict())
        first = original.snapshot()['operations'][0]['id']
        for ids, delta in [([first], -101), ([first], float('nan')), (['missing'], 1), ([first], 250)]:
            with self.assertRaises(ValueError): original.move_many(ids, delta)
            self.assertEqual(original.to_dict(), before)
        for ids in ([], first, [first, 'missing']):
            with self.assertRaises(ValueError): original.delete_many(ids)

    def test_shared_mouse_path_moves_once_only_when_all_owners_are_selected(self):
        original = RecordingDocument.from_dict(dict(version=1, name='共享轨迹', base=[1148,720], actions=[
            dict(t=100,op='down',button='left',x=10,y=10), dict(t=10,op='down',button='right',x=10,y=10),
            dict(t=10,op='move',x=20,y=20), dict(t=10,op='up',button='left'), dict(t=10,op='up',button='right')]))
        operations = original.snapshot()['operations']
        ids = [operation['id'] for operation in operations]
        with self.assertRaises(ValueError): original.move_many(ids[:1], 50)
        moved = original.move_many(ids, 50)
        self.assertEqual([e['at_ms'] for e in moved.snapshot()['events']], [e['at_ms']+50 for e in original.snapshot()['events']])
        self.assertFalse(moved.diagnostics)
        self.assertEqual(original.delete_many(ids).snapshot()['events'], [])

    def test_end_marker_tracks_group_move_and_rejects_truncation(self):
        original = document().add_marker(1000, '录制停止')
        ids = [operation['id'] for operation in original.snapshot()['operations']]
        moved = original.move_many(ids, 200)
        self.assertEqual(moved.duration_ms, 1200)
        end = next(operation['id'] for operation in original.snapshot()['operations'] if operation.get('label') == '录制停止')
        with self.assertRaises(ValueError): original.move_many([end], -900)

    def test_service_batch_is_one_undo_step_and_rejected_edit_adds_no_history(self):
        with tempfile.TemporaryDirectory() as folder:
            service = StudioService(Path(folder))
            try:
                before = service.open_document(document())
                ids = [operation['id'] for operation in before['operations'][:2]]
                moved = service.dispatch('move_many', {'ids': ids, 'delta_ms': 200})
                self.assertEqual(len(service.undo), 1)
                with self.assertRaises(ValueError): service.dispatch('delete_many', {'ids': ['missing']})
                self.assertEqual(len(service.undo), 1)
                self.assertEqual(service.dispatch('undo')['operations'], before['operations'])
                self.assertEqual(service.dispatch('redo')['operations'], moved['operations'])
                service.dispatch('delete_many', {'ids': ids})
                self.assertEqual(service.dispatch('undo')['operations'], moved['operations'])
            finally: service.close()


if __name__ == '__main__': unittest.main()
