"""Offline file/JSONL integration checks, using only temporary directories."""

from copy import deepcopy
import gc
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import weakref

AGENT = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT))
import script_studio_service as bridge


def legacy(name="原脚本"):
    return dict(version=1, name=name, base=[1152, 720], duration_ms=1000,
                recorded_at="2026-10-25", custom={"preserve": [1, 2]}, actions=[
                    dict(t=100, op="keydown", key=74),
                    dict(t=200, op="keyup", key=74),
                    dict(t=100, op="click", x=20, y=30)])


def source_macro():
    return dict(schema="zaomeng.macro", version=1, id="legacy-source", name="源录制",
                created_at="2026-10-25T10:00:00+08:00", duration_ms=500,
                window=dict(title="Game", class_name="Game", width=1200, height=750),
                events=[dict(t_ms=10, kind="key_down", data=dict(vk=65, scan=30, extended=False)),
                        dict(t_ms=110, kind="key_up", data=dict(vk=65, scan=30, extended=False)),
                        dict(t_ms=120, kind="mouse_down", data=dict(x=600, y=375, button="right")),
                        dict(t_ms=220, kind="mouse_up", data=dict(x=600, y=375, button="right")),
                        dict(t_ms=250, kind="marker", data=dict(label="阶段1"))])


class FakeCapture:
    def __init__(self):
        self.state = "idle"
        self.payload = legacy("新录制")
        self.stop_calls = 0
        self.close_calls = 0
        self.fail_stop = False
        self.fail_close = False
        self.save_requested = False

    def start(self, hwnd, countdown_seconds=3):
        self.hwnd, self.countdown = hwnd, countdown_seconds
        self.state = "recording"

    def snapshot(self):
        return dict(state=self.state, elapsed_ms=1000, event_count=len(self.payload["actions"]),
                    hwnd=getattr(self, "hwnd", 0), error="", save_requested=self.save_requested)

    def pause(self):
        self.state = "paused"

    def resume(self, countdown_seconds=0):
        self.countdown = countdown_seconds
        self.state = "resume_countdown" if countdown_seconds else "recording"

    def mark(self, label):
        self.payload.setdefault("editor", {}).setdefault("markers", []).append(dict(at_ms=250, label=label))

    def stop(self):
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError("fake capture stop failure")
        self.state = "stopped"
        return deepcopy(self.payload)

    def close(self):
        self.close_calls += 1
        self.state = "closed"
        if self.fail_close:
            raise RuntimeError("fake capture close failure")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # StudioService resolves its root; fault targets must use the same form
        # when Windows TEMP contains a short-path alias.
        self.root = Path(self.temporary.name).resolve()
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.file = self.recordings / "原脚本.json"
        self.write(self.file, legacy())
        self.original = self.file.read_bytes()
        self.service = bridge.StudioService(self.root)

    def tearDown(self):
        try:
            self.service.close()
        finally:
            self.temporary.cleanup()

    @staticmethod
    def write(path, data):
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def load(self, file="原脚本.json"):
        return self.service.dispatch("load", dict(file=file))

    def test_duration_edit_undo_redo_and_save_preserve_events(self):
        self.load()
        self.service.dispatch('set_duration', {'duration_ms': 400})
        self.assertEqual(self.service.document.duration_ms, 400)
        self.service.dispatch('undo')
        self.assertEqual(self.service.document.duration_ms, 1000)
        self.service.dispatch('redo')
        self.assertEqual(self.service.document.duration_ms, 400)
        with self.assertRaises(ValueError):
            self.service.dispatch('set_duration', {'duration_ms': 399})
        self.service.dispatch('save')
        saved = json.loads(self.file.read_text(encoding='utf8'))
        self.assertEqual(saved['duration_ms'], 400)
        self.assertEqual(saved['actions'], legacy()['actions'])

    def test_duration_edit_moves_stop_markers_through_save_and_history(self):
        raw = legacy()
        raw['editor'] = {'markers': [{'at_ms': 1000, 'label': '暂停录制'},
                                     {'at_ms': 1000, 'label': '录制停止'}]}
        self.write(self.file, raw)
        self.load()
        self.service.dispatch('set_duration', {'duration_ms': 450})
        self.service.dispatch('undo')
        self.assertEqual(self.service.document.duration_ms, 1000)
        self.service.dispatch('redo')
        self.service.dispatch('save')
        self.load()
        snapshot = self.service.document.snapshot()
        self.assertEqual(snapshot['duration_ms'], 450)
        self.assertEqual([m['at_ms'] for m in snapshot['markers']], [450, 450])
        self.assertEqual(self.service.document.to_dict()['actions'], raw['actions'])

    def edit_key(self, key=75):
        snapshot = self.service.dispatch("snapshot")
        operation = next(op for op in snapshot["operations"] if op["kind"] == "key_hold")
        return self.service.dispatch("edit", dict(id=operation["id"], changes=dict(key=key, start_ms=150, duration_ms=250)))

    def test_legacy_load_edit_save_preserves_unknown_fields_and_updates_revision(self):
        loaded = self.load()
        self.assertEqual(loaded["revision"], hashlib.sha256(self.original).hexdigest())
        changed = self.edit_key()
        self.assertTrue(changed["dirty"])
        saved = self.service.dispatch("save")
        raw, revision = bridge.read_json(self.file)
        self.assertEqual(raw["custom"], {"preserve": [1, 2]})
        self.assertEqual([a.get("key") for a in raw["actions"][:2]], [75, 75])
        self.assertEqual(saved["revision"], revision)
        self.assertFalse(saved["dirty"])
        self.assertEqual(raw["actions"][0]["t"], 150)

    def test_external_modification_rejects_save_and_keeps_draft(self):
        self.load()
        before = self.edit_key()
        self.write(self.file, legacy("外部修改"))
        external = self.file.read_bytes()
        with self.assertRaisesRegex(ValueError, "其他程序"):
            self.service.dispatch("save")
        self.assertEqual(self.file.read_bytes(), external)
        self.assertEqual(self.service.snapshot(), before)

    def test_atomic_replace_failure_preserves_original_and_unsaved_editor(self):
        self.load()
        before = self.edit_key()
        with patch.object(bridge.os, "replace", side_effect=OSError("fake disk failure")):
            with self.assertRaisesRegex(OSError, "fake disk failure"):
                self.service.dispatch("save")
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertEqual(self.service.snapshot(), before)
        self.assertEqual(list(self.recordings.glob(".studio-*.tmp")), [])

    def test_new_name_publication_is_exclusive_and_failure_keeps_draft(self):
        self.service.dispatch("new")
        before = self.service.snapshot()
        destination = self.recordings / "新脚本.json"
        real_link = bridge.os.link

        def competing_writer(source, target):
            self.write(target, legacy("别人先保存"))
            return real_link(source, target)

        with patch.object(bridge.os, "link", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                self.service.dispatch("save", dict(file="新脚本.json"))
        self.assertEqual(bridge.read_json(destination)[0]["name"], "别人先保存")
        self.assertEqual(self.service.snapshot(), before)

    def test_same_name_save_as_is_rejected_without_overwrite(self):
        self.service.dispatch("new")
        with self.assertRaisesRegex(ValueError, "同名"):
            self.service.dispatch("save", dict(file="原脚本.json"))
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertTrue(self.service.dirty)

    def test_invalid_load_preserves_previous_identity_and_history(self):
        self.load()
        self.edit_key()
        self.service.dispatch("save")
        before = self.service.snapshot()
        self.write(self.recordings / "坏文件.json", dict(name="坏", actions="not a list"))
        with self.assertRaises(ValueError):
            self.load("坏文件.json")
        self.assertEqual(self.service.snapshot(), before)

    def test_failed_discard_does_not_clear_dirty_draft(self):
        self.load()
        before = self.edit_key()
        self.file.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.dispatch("discard")
        self.assertEqual(self.service.snapshot(), before)

    def test_import_starts_separate_history_and_adapts_source_coordinates(self):
        self.load()
        self.edit_key()
        self.service.dispatch("save")
        original = self.file.read_bytes()
        source = self.root / "源格式.json"
        self.write(source, source_macro())
        imported = self.service.dispatch("import", dict(path=str(source)))
        self.assertIsNone(imported["file"])
        self.assertFalse(imported["can_undo"])
        self.assertTrue(imported["dirty"])
        self.assertEqual(imported["base"], [1152, 720])
        mouse = next(op for op in imported["operations"] if op["kind"] == "mouse_hold")
        self.assertEqual((mouse["x"], mouse["y"], mouse["button"]), (576, 360, "right"))
        self.assertEqual(imported["markers"][0]["label"], "阶段1")
        with self.assertRaises(ValueError):
            self.service.dispatch("undo")
        self.service.dispatch("save", dict(file="导入.json"))
        self.assertEqual(self.file.read_bytes(), original)

    def test_load_and_new_clear_history_but_undo_redo_work_within_document(self):
        self.load()
        self.edit_key(75)
        self.assertEqual(self.service.dispatch("undo")["events"][0]["key"], 74)
        self.assertEqual(self.service.dispatch("redo")["events"][0]["key"], 75)
        self.service.dispatch("save")
        self.write(self.recordings / "第二份.json", legacy("第二份"))
        second = self.load("第二份.json")
        self.assertFalse(second["can_undo"])
        self.assertFalse(second["can_redo"])
        new = self.service.dispatch("new")
        self.assertFalse(new["can_undo"])
        self.assertFalse(new["events"])

    def test_dirty_state_blocks_document_switch_until_discard(self):
        self.load()
        before = self.edit_key()
        for method, params in (("load", dict(file="原脚本.json")), ("new", {}),
                               ("import", dict(path=str(self.file))), ("capture_start", dict(hwnd=42))):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "未保存"):
                self.service.dispatch(method, params)
        self.assertEqual(self.service.snapshot(), before)
        discarded = self.service.dispatch("discard")
        self.assertFalse(discarded["dirty"])
        self.assertEqual(discarded["events"][0]["key"], 74)

    def test_select_only_writes_pointer_without_touching_interface_or_recording(self):
        interface = self.root / "interface.json"
        interface.write_text('{"task": ["existing"]}', encoding="utf-8")
        original_interface = interface.read_bytes()
        self.load()
        selected = self.service.dispatch("select")
        self.assertEqual(selected["file"], "原脚本.json")
        self.assertEqual(bridge.read_json(self.recordings / ".selected.json")[0], dict(file="原脚本.json"))
        self.assertEqual(self.service.dispatch("list")["selected_file"], "原脚本.json")
        self.assertEqual(interface.read_bytes(), original_interface)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_select_rejects_dirty_or_externally_modified_file(self):
        self.load()
        self.edit_key()
        with self.assertRaisesRegex(ValueError, "未保存"):
            self.service.dispatch("select")
        self.service.dispatch("discard")
        self.write(self.file, legacy("外部新内容"))
        with self.assertRaisesRegex(ValueError, "其他程序"):
            self.service.dispatch("select")
        self.assertFalse((self.recordings / ".selected.json").exists())

    def test_malformed_or_traversing_selected_pointer_is_ignored(self):
        for data in ([], None, dict(file="../outside.json"), dict(file="不存在.json")):
            with self.subTest(data=data):
                self.write(self.recordings / ".selected.json", data)
                self.assertIsNone(self.service.dispatch("list")["selected_file"])

    def test_bad_library_entry_is_reported_without_modifying_it(self):
        bad = self.recordings / "损坏.json"
        bad.write_bytes(b"invalid json")
        items = self.service.dispatch("list")["items"]
        error = next(item for item in items if item["file"] == "损坏.json")
        self.assertTrue(error["error"])
        self.assertEqual(bad.read_bytes(), b"invalid json")
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_malicious_and_windows_device_file_names_are_rejected(self):
        names = ("../outside.json", "..\\outside.json", "C:\\outside.json", "/tmp/out.json",
                 ".selected.json", "name.json:stream", "a\x00.json", "a\n.json", "CON.json",
                 "aux.json", "LPT9.json", "COM1.test.json", "foo?.json", "foo.txt", "")
        for name in names:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.service.path(name)

    def test_symlink_cannot_escape_library(self):
        outside = self.root / "outside.json"
        self.write(outside, legacy("外部文件"))
        link = self.recordings / "linked.json"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"此环境不允许创建符号链接：{error}")
        with self.assertRaisesRegex(ValueError, "目录以外"):
            self.service.dispatch("load", dict(file=link.name))
        self.assertTrue(next(item for item in self.service.list()["items"] if item["file"] == link.name)["error"])

    def test_resolved_path_guard_rejects_external_targets_without_native_symlinks(self):
        original_resolve = Path.resolve
        candidate = self.recordings / "linked.json"
        injected = []

        def resolve(path, *args, **kwargs):
            if path == candidate:
                injected.append(path)
                return self.root / "outside.json"
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(ValueError, "目录以外"):
                self.service.path("linked.json")
        self.assertEqual(injected, [candidate], "external-path injection was not exercised")

    def test_capture_fake_state_is_exclusive_and_stop_imports_once(self):
        capture = FakeCapture()
        self.service.capture_factory = lambda: capture
        self.load()
        self.assertEqual(self.service.dispatch("capture_start", dict(hwnd=42, countdown_seconds=1))["state"], "recording")
        self.assertEqual(capture.hwnd, 42)
        self.assertEqual(capture.countdown, 1)
        with self.assertRaisesRegex(ValueError, "停止录制"):
            self.service.dispatch("rename", dict(name="不能修改"))
        self.assertEqual(self.service.dispatch("capture_pause")["state"], "paused")
        self.assertEqual(self.service.dispatch("capture_resume")["state"], "resume_countdown")
        self.assertEqual(capture.countdown, 3)
        self.service.dispatch("capture_mark", dict(label="阶段A"))
        stopped = self.service.dispatch("capture_stop")
        self.assertEqual(stopped["name"], "新录制")
        self.assertIsNone(stopped["file"])
        self.assertTrue(stopped["dirty"])
        self.service.dispatch("rename", dict(name="修改录制名"))
        self.assertEqual(self.service.dispatch("capture_stop")["name"], "修改录制名")
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_discard_take_in_every_capture_state_preserves_saved_files_and_allows_restart(self):
        before = self.load()
        self.write(self.recordings / '.bindings.json', {'version': 1, 'bindings': {}})
        files = {p.name: p.read_bytes() for p in self.recordings.iterdir()}
        for state in ('countdown', 'recording', 'paused', 'resume_countdown', 'stopped', 'failed', 'closed'):
            with self.subTest(state=state):
                capture = FakeCapture()
                self.service.capture_factory = lambda: capture
                self.service.dispatch('capture_start', dict(hwnd=42))
                capture.state = state
                capture.save_requested = True  # F10 racing with explicit discard must not save.
                self.assertEqual(self.service.dispatch('capture_discard'), before)
                self.assertEqual(capture.close_calls, 1)
                self.assertIsNone(self.service.capture)
                self.assertTrue(self.service.capture_finished)
                self.assertEqual(self.service.dispatch('capture_status')['state'], 'idle')
                with self.assertRaisesRegex(ValueError, '没有正在'):
                    self.service.dispatch('capture_stop')
        self.service.close()
        self.assertEqual({p.name: p.read_bytes() for p in self.recordings.iterdir()}, files)

    def test_discard_unsaved_capture_draft_clears_edits_history_and_shutdown_recovery(self):
        self.service.capture_factory = FakeCapture
        self.load()
        self.service.dispatch('capture_start', dict(hwnd=42))
        draft = self.service.dispatch('capture_stop')
        self.assertTrue(draft['capture_draft'])
        renamed = self.service.dispatch('rename', dict(name='不保留这一段'))
        self.assertTrue(renamed['capture_draft'])
        self.assertTrue(renamed['can_undo'])
        self.assertTrue(self.service.dispatch('capture_discard')['empty'])
        self.assertFalse(self.service.dirty)
        self.assertFalse(self.service.undo)
        self.assertFalse(self.service.redo)
        self.assertFalse(self.service.capture_draft)
        self.service.dispatch('capture_start', dict(hwnd=42))
        self.service.dispatch('capture_discard')
        self.service.close()
        self.assertEqual(list(self.recordings.iterdir()), [self.file])
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_saved_captures_and_unrelated_drafts_cannot_be_discarded_as_a_take(self):
        self.service.capture_factory = FakeCapture
        self.service.dispatch('capture_start', dict(hwnd=42))
        self.service.dispatch('capture_stop')
        saved = self.service.dispatch('save', dict(file='保存的录制.json'))
        self.assertFalse(saved['capture_draft'])
        for operation in ('saved', 'edited_saved', 'new', 'import'):
            with self.subTest(operation=operation):
                if operation == 'edited_saved':
                    self.service.dispatch('rename', dict(name='待保存的编辑'))
                elif operation in ('new', 'import'):
                    self.service.dispatch('discard')
                    self.service.dispatch(operation, dict(path=str(self.file)))
                before = self.service.snapshot()
                with self.assertRaisesRegex(ValueError, '没有可放弃'):
                    self.service.dispatch('capture_discard')
                self.assertEqual(self.service.snapshot(), before)
        self.service.dispatch('discard')
        self.assertTrue((self.recordings / '保存的录制.json').is_file())

    def test_failed_discard_keeps_capture_for_retry_without_claiming_success(self):
        self.service.capture_factory = FakeCapture
        before = self.load()
        self.service.dispatch('capture_start', dict(hwnd=42))
        capture = self.service.capture
        for failure in ('fail_stop', 'fail_close'):
            with self.subTest(failure=failure):
                setattr(capture, failure, True)
                with self.assertRaisesRegex(RuntimeError, 'fake capture'):
                    self.service.dispatch('capture_discard')
                self.assertIs(self.service.capture, capture)
                self.assertFalse(self.service.capture_finished)
                self.assertEqual(self.service.snapshot(), before)
                setattr(capture, failure, False)
        self.assertEqual(self.service.dispatch('capture_discard'), before)

    def test_discard_releases_real_capture_worker_with_fake_hooks_and_no_input(self):
        from script_studio.capture import CaptureSession
        from test_script_studio_capture import FakeClock, FakeWindows, FakeHooks
        hooks = FakeHooks()
        session = CaptureSession(window_api=FakeWindows(), listener_factory=hooks, clock=FakeClock())
        self.service.capture_factory = lambda: session
        self.service.dispatch('capture_start', dict(hwnd=42, countdown_seconds=0))
        hooks.key(65)
        hooks.mouse(700, 425)
        session._command('barrier')
        self.assertTrue(self.service.dispatch('capture_discard')['empty'])
        self.assertEqual(session.snapshot()['state'], 'closed')
        self.assertFalse(session._worker.is_alive())
        self.assertTrue(all(item.stopped and item.joined and not item.is_alive() for item in hooks.listeners))
        self.assertFalse(self.service.capture_draft)
        self.service.close()
        self.assertEqual(list(self.recordings.iterdir()), [self.file])

    def test_cancel_initial_countdown_keeps_loaded_script(self):
        capture = FakeCapture()
        self.service.capture_factory = lambda: capture
        self.load()
        before = self.service.snapshot()
        self.service.dispatch('capture_start', dict(hwnd=42,countdown_seconds=3))
        capture.state = 'countdown'
        result = self.service.dispatch('capture_cancel')
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.service.snapshot(), before)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_close_recovers_unfinished_capture_and_releases_it(self):
        capture = FakeCapture()
        self.service.capture_factory = lambda: capture
        self.service.dispatch("capture_start", dict(hwnd=42))
        self.service.close()
        files = list(self.recordings.glob("恢复录制-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(bridge.read_json(files[0])[0]["actions"], capture.payload["actions"])
        self.assertEqual(capture.close_calls, 1)
        self.assertEqual(self.file.read_bytes(), self.original)
        self.service.close()
        self.assertEqual(len(list(self.recordings.glob("恢复录制-*.json"))), 1)

    def test_close_preserves_dirty_draft_even_when_capture_cleanup_fails(self):
        self.load()
        self.edit_key()
        capture = FakeCapture()
        capture.fail_stop = capture.fail_close = True
        self.service.capture = capture
        with self.assertRaisesRegex(RuntimeError, "fake capture"):
            self.service.close()
        drafts = list(self.recordings.glob("恢复草稿-*.json"))
        self.assertEqual(len(drafts), 1)
        self.assertEqual(bridge.read_json(drafts[0])[0]["actions"][0]["key"], 75)
        self.assertEqual(self.file.read_bytes(), self.original)
        capture.fail_stop = capture.fail_close = False

    def test_recovery_names_do_not_overwrite_other_editors_drafts(self):
        self.load()
        self.edit_key()
        second = bridge.StudioService(self.root)
        second.dispatch("new")
        with patch.object(bridge.time, "strftime", return_value="same-second"):
            self.service.close()
            second.close()
        files = list(self.recordings.glob("恢复草稿-same-second-*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual({bridge.read_json(file)[0]["name"] for file in files}, {"原脚本", "新脚本"})

    def test_export_failure_does_not_mark_document_clean(self):
        self.load()
        before = self.edit_key()
        target = self.root / "export.json"
        target.write_bytes(b"previous export")
        with patch.object(bridge.os, "replace", side_effect=OSError("fake export failure")):
            with self.assertRaises(OSError):
                self.service.dispatch("export", dict(path=str(target)))
        self.assertEqual(target.read_bytes(), b"previous export")
        self.assertEqual(self.service.snapshot(), before)

    def test_export_to_current_file_is_rejected_without_changing_revision_or_dirty_state(self):
        self.load()
        before = self.edit_key()
        target = self.recordings / ".." / "recordings" / self.file.name
        with self.assertRaisesRegex(ValueError, "当前脚本，请使用保存"):
            self.service.dispatch("export", dict(path=str(target)))
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertEqual(self.service.snapshot(), before)
        saved = self.service.dispatch("save")
        self.assertFalse(saved["dirty"])
        self.assertEqual(bridge.read_json(self.file)[1], saved["revision"])

    def test_export_cannot_overwrite_other_library_files_or_selection_pointer(self):
        self.load()
        self.edit_key()
        before = self.service.snapshot()
        other = self.recordings / "其他录制.json"
        self.write(other, legacy("其他录制"))
        selected = self.recordings / ".selected.json"
        self.write(selected, dict(file="其他录制.json"))
        nested = self.recordings / "folder" / "深层录制.json"
        nested.parent.mkdir()
        self.write(nested, legacy("深层录制"))
        for target in (other, selected, nested):
            original = target.read_bytes()
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "不能覆盖脚本库"):
                self.service.dispatch("export", dict(path=str(target)))
            self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.service.snapshot(), before)

    def test_export_to_new_library_path_or_existing_external_file_keeps_editor_state(self):
        self.load()
        before = self.edit_key()
        library_copy = self.recordings / "导出副本.json"
        external = self.root / "外部导出.json"
        external.write_text("old export", encoding="utf-8")
        for target in (library_copy, external):
            with self.subTest(target=target):
                self.service.dispatch("export", dict(path=str(target)))
                self.assertEqual(bridge.read_json(target)[0]["actions"][0]["key"], 75)
                self.assertEqual(self.service.snapshot(), before)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_split_creates_two_files_and_rolls_back_only_own_partial_output(self):
        self.load()
        real_atomic = bridge.atomic_json
        calls = []

        def fail_second(path, data, **kwargs):
            calls.append(Path(path))
            if len(calls) == 2:
                self.write(path, legacy("并发新文件"))
                raise FileExistsError("racing writer")
            return real_atomic(path, data, **kwargs)

        with patch.object(bridge, "atomic_json", side_effect=fail_second):
            with self.assertRaises(FileExistsError):
                self.service.dispatch("split", dict(at_ms=350))
        self.assertFalse(calls[0].exists())
        self.assertEqual(bridge.read_json(calls[1])[0]["name"], "并发新文件")
        self.assertEqual(self.file.read_bytes(), self.original)
        result = self.service.dispatch("split", dict(at_ms=350))
        self.assertEqual(len(result["files"]), 2)
        self.assertTrue(all((self.recordings / file).is_file() for file in result["files"]))

    def test_jsonl_eof_recovers_dirty_document_and_keeps_stdout_machine_readable(self):
        requests = [dict(id=1, method="load", params=dict(file="原脚本.json")),
                    dict(id=2, method="rename", params=dict(name="EOF草稿")),
                    dict(id=3, method="unsupported")]
        stdin = "\n".join(json.dumps(request, ensure_ascii=False) for request in requests) + "\ninvalid-json\n"
        process = subprocess.run([sys.executable, str(AGENT / "script_studio_service.py"), "--root", str(self.root)],
                                 input=stdin, text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, None])
        self.assertEqual([response["ok"] for response in responses], [True, True, False, False])
        drafts = list(self.recordings.glob("恢复草稿-*.json"))
        self.assertEqual(len(drafts), 1)
        self.assertEqual(bridge.read_json(drafts[0])[0]["name"], "EOF草稿")
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_nonfinite_or_oversize_json_rejected_before_loading(self):
        source = self.root / "nan.json"
        source.write_text('{"name":"bad","actions":[],"duration_ms":NaN}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "非有限"):
            self.service.dispatch("import", dict(path=str(source)))
        with patch.object(bridge, "MAX_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "16 MiB"):
                bridge.read_json(self.file)

    def test_history_budget_evicts_old_states_and_preserves_undo_redo_order(self):
        self.load()
        with patch.object(bridge, "HISTORY_MAX_BYTES", 250), \
                patch.object(self.service, "_estimate_history_cost", return_value=100):
            for name in ("阶段1", "阶段2", "阶段3"):
                self.service.dispatch("rename", dict(name=name))
            self.assertEqual([doc.name for doc in self.service.undo], ["阶段1", "阶段2"])
            self.assertEqual(self.service.snapshot()["history_bytes"], 200)
            self.assertEqual(self.service.dispatch("undo")["name"], "阶段2")
            self.assertEqual(self.service.dispatch("undo")["name"], "阶段1")
            with self.assertRaisesRegex(ValueError, "撤销"):
                self.service.dispatch("undo")
            self.assertEqual(self.service.dispatch("redo")["name"], "阶段2")
            changed = self.service.dispatch("rename", dict(name="新分支"))
            self.assertFalse(changed["can_redo"])
            self.assertEqual(changed["history_bytes"], 200)
            self.assertEqual(self.service.dispatch("undo")["name"], "阶段2")

    def test_oversize_history_state_is_not_retained_but_current_document_is_preserved(self):
        self.load()
        with patch.object(bridge, "HISTORY_MAX_BYTES", 50), \
                patch.object(self.service, "_estimate_history_cost", return_value=100):
            changed = self.service.dispatch("rename", dict(name="仍能编辑"))
            self.assertEqual(changed["name"], "仍能编辑")
            self.assertTrue(changed["dirty"])
            self.assertFalse(changed["can_undo"])
            self.assertFalse(changed["can_redo"])
            self.assertEqual(changed["history_bytes"], 0)
            saved = self.service.dispatch("save")
            self.assertEqual(saved["name"], "仍能编辑")
            self.assertEqual(bridge.read_json(self.file)[0]["name"], "仍能编辑")

    def test_oversize_predecessor_clears_older_undo_to_avoid_skipping_a_state(self):
        self.load()
        with patch.object(bridge, "HISTORY_MAX_BYTES", 250), \
                patch.object(self.service, "_estimate_history_cost", side_effect=lambda doc: 400 if doc.name == "大状态" else 100):
            self.service.dispatch("rename", dict(name="大状态"))
            self.assertTrue(self.service.snapshot()["can_undo"])
            changed = self.service.dispatch("rename", dict(name="大状态之后"))
            self.assertFalse(changed["can_undo"])
            self.assertEqual(changed["history_bytes"], 0)
            self.assertEqual(changed["name"], "大状态之后")

    def test_budget_prunes_oldest_across_both_history_directions(self):
        self.load()
        with patch.object(bridge, "HISTORY_MAX_BYTES", 1000), \
                patch.object(self.service, "_estimate_history_cost", return_value=100):
            for name in ("阶段1", "阶段2", "阶段3"):
                self.service.dispatch("rename", dict(name=name))
            self.service.dispatch("undo")
            with patch.object(bridge, "HISTORY_MAX_BYTES", 150):
                self.service._trim_history()
                self.assertFalse(self.service.undo)
                self.assertEqual([doc.name for doc in self.service.redo], ["阶段3"])
                self.assertEqual(self.service.snapshot()["history_bytes"], 100)
                self.assertEqual(self.service.dispatch("redo")["name"], "阶段3")

    def test_history_keeps_the_100_state_cap_even_under_a_large_byte_budget(self):
        self.load()
        with patch.object(bridge, "HISTORY_MAX_BYTES", 10_000_000), \
                patch.object(self.service, "_estimate_history_cost", return_value=1):
            for index in range(105):
                self.service.dispatch("rename", dict(name=f"阶段{index}"))
            self.assertEqual(len(self.service.undo), 100)
            self.assertEqual(self.service.undo[0].name, "阶段4")
            self.assertEqual(self.service.snapshot()["history_bytes"], 100)

    def test_history_cost_is_cached_per_document_without_reserializing_all_states(self):
        self.load()
        with patch.object(self.service, "_estimate_history_cost", wraps=self.service._estimate_history_cost) as estimate:
            first_cost = self.service._history_cost(self.service.document)
            self.assertEqual(first_cost, self.service._history_cost(self.service.document))
            self.service.dispatch("rename", dict(name="新名称"))
            self.assertEqual(estimate.call_count, 1)
            self.service.dispatch("undo")
            self.assertEqual(estimate.call_count, 2)
            self.service.dispatch("redo")
            self.assertEqual(estimate.call_count, 2)

    def test_cost_cache_does_not_keep_discarded_documents_alive(self):
        self.load()
        previous = self.service.document
        reference = weakref.ref(previous)
        self.service._history_cost(previous)
        self.service.dispatch("rename", dict(name="改动"))
        self.service.dispatch("discard")
        del previous
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(len(self.service._history_costs), 0)
        self.assertEqual(len(self.service._history_order), 0)
        self.assertEqual(self.service.snapshot()["history_bytes"], 0)

    def test_document_switch_resets_history_accounting(self):
        self.load()
        self.service.dispatch("rename", dict(name="阶段1"))
        self.service.dispatch("rename", dict(name="阶段2"))
        self.assertGreater(self.service.snapshot()["history_bytes"], 0)
        self.service.dispatch("save")
        fresh = self.service.dispatch("new")
        self.assertEqual(fresh["history_bytes"], 0)
        self.assertFalse(fresh["can_undo"])
        self.assertFalse(fresh["can_redo"])


if __name__ == "__main__":
    unittest.main()
