"""Clipboard edits preserve paired input across tracks and one-step history."""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from test_timeline_batch import document
from script_studio.model import RecordingDocument
from script_studio_service import StudioService


class TimelineClipboardTests(unittest.TestCase):
    def test_real_stdio_copy_paste_undo_and_redo_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);library=root/'recordings';library.mkdir()
            source=document();(library/'clipboard.json').write_text(json.dumps(source.to_dict(),ensure_ascii=False),encoding='utf8')
            ids=[op['id'] for op in source.snapshot()['operations']]
            requests=[dict(id=1,method='load',params=dict(file='clipboard.json')),
                dict(id=2,method='copy_many',params=dict(ids=ids)),
                dict(id=3,method='paste_many',params=dict(clipboard=source.copy_many(ids),at_ms=1000)),
                dict(id=4,method='undo'),dict(id=5,method='redo'),dict(id=6,method='shutdown')]
            process=subprocess.run([sys.executable,'-B','-E','-s',str(Path(__file__).resolve().parents[1]/'agent/script_studio_service.py'),'--root',str(root)],
                input=''.join(json.dumps(request,ensure_ascii=False)+'\n' for request in requests),capture_output=True,text=True,encoding='utf8',timeout=15)
            self.assertEqual(process.returncode,0,process.stderr)
            responses=[json.loads(line) for line in process.stdout.splitlines()]
            self.assertTrue(all(response['ok'] for response in responses),responses)
            self.assertEqual(responses[1]['result']['format'],'maazmxy4.timeline')
            self.assertEqual(len(responses[2]['result']['selected_ids']),4)
            self.assertEqual(responses[3]['result']['operations'],responses[0]['result']['operations'])
            self.assertEqual(responses[4]['result']['operations'],responses[2]['result']['operations'])

    def test_multitrack_fragment_anchors_earliest_event_at_playhead_and_gets_new_ids(self):
        original = document()
        operations = original.snapshot()['operations']
        packet = original.copy_many([op['id'] for op in operations])
        before = deepcopy(packet)
        pasted, selected = original.paste_many(packet, 1000)
        new = [op for op in pasted.snapshot()['operations'] if op['id'] in selected]
        self.assertEqual([op['start_ms'] for op in new], [1000,1200,1400,1500])
        self.assertEqual([op['duration_ms'] for op in new], [100,100,0,0])
        self.assertEqual(pasted.duration_ms,1500)
        self.assertFalse(set(selected) & {op['id'] for op in operations})
        self.assertEqual(packet,before)
        self.assertEqual(len(original.snapshot()['operations']),4)
        self.assertEqual([e['custom'] for e in pasted.to_dict()['actions'] if 'custom' in e],['keep','keep'])

    def test_key_release_boundary_and_zero_length_pairs_keep_order(self):
        original = document()
        packet = original.copy_many([original.snapshot()['operations'][0]['id']])
        pasted,_ = original.paste_many(packet,200)
        self.assertFalse(pasted.diagnostics)
        at200=[event['op'] for event in pasted.snapshot()['events'] if event['at_ms']==200]
        self.assertEqual(at200,['keyup','keydown'])
        instant = RecordingDocument.from_dict(dict(version=1,name='instant',actions=[dict(t=0,op='keydown',key=65),dict(t=0,op='keyup',key=65)]))
        copied=instant.copy_many([instant.snapshot()['operations'][0]['id']])
        result,_=instant.paste_many(copied,0)
        self.assertFalse(result.diagnostics)
        self.assertEqual([e['op'] for e in result.snapshot()['events']],['keydown','keyup','keydown','keyup'])

    def test_copy_shared_drag_requires_all_owners_and_preserves_path(self):
        original=RecordingDocument.from_dict(dict(version=1,name='drag',base=[1148,720],actions=[
            dict(t=100,op='down',button='left',x=10,y=10),dict(t=10,op='down',button='right',x=10,y=10),
            dict(t=10,op='move',x=20,y=30,extra='keep'),dict(t=10,op='up',button='left'),dict(t=10,op='up',button='right')]))
        ids=[op['id'] for op in original.snapshot()['operations']]
        with self.assertRaises(ValueError):original.copy_many(ids[:1])
        pasted,selected=original.paste_many(original.copy_many(ids),500)
        self.assertEqual(len(selected),2)
        self.assertEqual(len(pasted.snapshot()['events']),10)
        self.assertEqual([e['at_ms'] for e in pasted.snapshot()['events'][-5:]],[500,510,520,530,540])
        self.assertEqual(pasted.snapshot()['events'][-3]['extra'],'keep')

    def test_clipboard_end_marker_is_a_point_and_destination_endpoint_still_tracks_duration(self):
        original=document().add_marker(1000,'录制停止')
        packet=original.copy_many([op['id'] for op in original.snapshot()['operations']])
        pasted,selected=original.paste_many(packet,1100)
        ends=[m for m in pasted.snapshot()['markers'] if m['label']=='录制停止']
        self.assertEqual(len(ends),2)
        copied=next(m for m in ends if m['id'] in selected)
        self.assertEqual(copied['anchor'],'point')
        self.assertEqual(copied['at_ms'],2000)
        extended=pasted.set_duration(2500)
        self.assertEqual(next(m for m in extended.snapshot()['markers'] if m['id']==copied['id'])['at_ms'],2000)

    def test_cross_document_mouse_layout_conflict_and_malformed_payload_are_atomic(self):
        original=document();before=original.to_dict()
        operations=original.snapshot()['operations']
        keys=original.copy_many([operations[0]['id']]);mouse=original.copy_many([operations[2]['id']])
        other=RecordingDocument.from_dict(dict(version=1,name='other',base=[1280,720],actions=[]))
        self.assertTrue(other.paste_many(keys,0)[1])
        with self.assertRaises(ValueError):other.paste_many(mouse,0)
        for packet,at in [(keys,150),(keys,-1),(keys,float('nan')),({},0),({**keys,'version':2},0)]:
            with self.assertRaises(ValueError):original.paste_many(packet,at)
            self.assertEqual(original.to_dict(),before)
        invalid=deepcopy(keys);invalid['recording']['actions'].pop()
        with self.assertRaises(ValueError):original.paste_many(invalid,1000)

    def test_service_copy_is_read_only_paste_is_one_history_entry_and_undo_redo_preserve_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            service=StudioService(Path(folder))
            try:
                before=service.open_document(document())
                ids=[op['id'] for op in before['operations']]
                packet=service.dispatch('copy_many',{'ids':ids})
                self.assertFalse(service.undo)
                after=service.dispatch('paste_many',{'clipboard':packet,'at_ms':1000})
                self.assertEqual(len(service.undo),1)
                self.assertEqual(len(after['selected_ids']),4)
                self.assertEqual(service.dispatch('undo')['operations'],before['operations'])
                self.assertEqual(service.dispatch('redo')['operations'],after['operations'])
                count=len(service.undo)
                with self.assertRaises(ValueError):service.dispatch('paste_many',{'clipboard':packet,'at_ms':150})
                self.assertEqual(len(service.undo),count)
            finally:service.close()
