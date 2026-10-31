"""JSON-lines bridge for the native MFAAvalonia script studio.

The editor and capture state machines are migrated from ZaoMengScript. The
transport deliberately has no Qt dependency and keeps existing recordings in
the battle_player version-1 format. Importing this module never installs hooks.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4
from weakref import WeakKeyDictionary

from script_studio.model import RecordingDocument
from script_library import (bind_task_recording, unbind_task_recording, library_recording_path,
                            read_task_bindings, resolve_task_recording)
from task_catalog import task_catalog, execution_plan
from recording_store import (ensure_default_recordings, default_recordings,
                             duplicate_recording, rename_recording, delete_recording, binding_snapshot, set_file_bindings)

MAX_BYTES = 16 * 1024 * 1024
HISTORY_MAX_BYTES = 64 * 1024 * 1024
HISTORY_MAX_STATES = 100


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("录制文件不能超过 16 MiB。")
    return json.loads(raw.decode("utf-8-sig"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("不能使用非有限数值。"))), hashlib.sha256(raw).hexdigest()


def atomic_json(path, data, *, overwrite=True):
    raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("录制文件不能超过 16 MiB。")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=".studio-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Same-directory hard linking publishes a complete new file and
            # atomically refuses a name created by another editor meanwhile.
            os.link(temporary, path)
        return hashlib.sha256(raw).hexdigest()
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class StudioService:
    def __init__(self, root, capture_factory=None):
        self.root = Path(root).resolve()
        self.directory = self.root / "recordings"
        self.initialization_error = None
        self.initialize_library()
        self.document = None
        self.file = None
        self.revision = None
        self.dirty = False
        self.undo = []
        self.redo = []
        self._history_bytes = 0
        self._history_sequence = 0
        self._history_costs = WeakKeyDictionary()
        self._history_order = WeakKeyDictionary()
        self.capture = None
        self.capture_factory = capture_factory
        self.capture_finished = False
        self._closed = False

    def initialize_library(self):
        try:
            ensure_default_recordings(self.root)
            self.initialization_error = None
        except (OSError, ValueError) as error:
            # A damaged default registry or a busy library must not kill the
            # JSONL worker and prevent unrelated native tasks from executing.
            self.initialization_error = str(error)

    def path(self, name):
        return library_recording_path(self.directory, name)

    def open_document(self, document, *, file=None, revision=None, edited=False):
        """Commit a fully validated document switch, with isolated history."""
        self.document, self.file, self.revision = document, file, revision
        self._clear_history()
        self.dirty = edited
        return self.snapshot()

    def require_document(self):
        if self.document is None:
            raise ValueError("请先选择或录制脚本。")
        return self.document

    def require_idle(self):
        if self.capture is not None and self.capture.snapshot()["state"] not in {"idle", "stopped", "failed", "closed"}:
            raise ValueError("请先停止录制。")

    def require_clean(self):
        if self.dirty:
            raise ValueError("当前脚本有未保存的修改，请先保存或放弃修改。")

    def snapshot(self):
        result = self.require_document().snapshot()
        result.update(file=self.file, revision=self.revision, dirty=self.dirty,
                      can_undo=bool(self.undo), can_redo=bool(self.redo),
                      history_bytes=self._history_bytes, history_budget_bytes=HISTORY_MAX_BYTES)
        return result

    @staticmethod
    def _estimate_history_cost(document):
        # A state owns raw legacy data, absolute events, IDs and Python mapping
        # objects. JSON size alone severely underestimates many small events.
        # This conservative estimate bounds retained history, not process RSS
        # or the current document and temporary copies needed for an edit.
        raw = document.to_dict(strict=False)
        serialized = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        events = len(raw["actions"])
        markers = len(raw.get("editor", {}).get("markers", []))
        return 4096 + serialized * 8 + events * 1024 + markers * 512

    def _history_cost(self, document):
        try:
            return self._history_costs[document]
        except KeyError:
            cost = self._estimate_history_cost(document)
            self._history_costs[document] = cost
            return cost

    def _clear_history(self, stack=None):
        if stack is None:
            self.undo.clear()
            self.redo.clear()
            self._history_bytes = 0
        else:
            self._history_bytes -= sum(self._history_costs[document] for document in stack)
            stack.clear()

    def _pop_history(self, stack, index=-1):
        document = stack.pop(index)
        self._history_bytes -= self._history_costs[document]
        return document

    def _trim_history(self):
        while (self._history_bytes > HISTORY_MAX_BYTES
               or len(self.undo) + len(self.redo) > HISTORY_MAX_STATES):
            candidates = [stack for stack in (self.undo, self.redo) if stack]
            if not candidates:
                break
            oldest = min(candidates, key=lambda stack: self._history_order[stack[0]])
            self._pop_history(oldest, 0)

    def _push_history(self, stack, document):
        cost = self._history_cost(document)
        if cost > HISTORY_MAX_BYTES:
            # Keeping older states behind a missing immediate predecessor would
            # make Undo/Redo silently jump over an operation.
            self._clear_history(stack)
            self._trim_history()
            return
        self._history_sequence += 1
        self._history_order[document] = self._history_sequence
        stack.append(document)
        self._history_bytes += cost
        self._trim_history()

    def replace(self, document, *, edited=True):
        if edited and self.document is not None:
            self._clear_history(self.redo)
            self._push_history(self.undo, self.document)
        self.document = document
        self.dirty = edited
        return self.snapshot()

    def list(self):
        items = []
        if self.initialization_error: self.initialize_library()
        library_errors = {'defaults': self.initialization_error} if self.initialization_error else {}
        try: defaults = default_recordings(self.directory)
        except (OSError, ValueError) as error:
            defaults = {}
            library_errors['defaults'] = str(error)
        if self.directory.exists():
            for path in sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                if path.name.startswith("."):
                    continue
                try:
                    data, revision = read_json(self.path(path.name))
                    snap = RecordingDocument.from_dict(data).snapshot()
                    items.append(dict(file=path.name, name=snap["name"], duration_ms=snap["duration_ms"],
                                      event_count=len(snap["events"]), modified=path.stat().st_mtime,
                                      revision=revision, is_default=path.name in defaults,
                                      default_id=defaults.get(path.name),
                                      can_bind=bool(data.get('actions')) and not snap['diagnostics'] and min(snap['base']) == 720,
                                      description=data.get('description', ''), error="；".join(snap["diagnostics"])))
                except Exception as error:
                    items.append(dict(file=path.name, name=path.stem, duration_ms=0, event_count=0,
                                      modified=path.stat().st_mtime, revision=hashlib.sha256(path.read_bytes()).hexdigest(),
                                      is_default=path.name in defaults, can_bind=False, error=str(error)))
        selected = None
        try:
            value = read_json(self.directory / ".selected.json")[0]
            if isinstance(value, dict) and self.path(value.get("file")).is_file():
                selected = value["file"]
        except (OSError, ValueError, TypeError):
            pass
        bindings, binding_errors, tasks, binding_revision = {}, {}, [], None
        try:
            records, binding_revision = binding_snapshot(self.directory)
            bindings = {key: value['file'] for key, value in records.items()}
            tasks = task_catalog(self.root, bindings=records)
            for task_id in bindings:
                try:
                    resolve_task_recording(self.directory, task_id)
                except (OSError, ValueError) as error:
                    binding_errors[task_id] = str(error)
            for task in tasks:
                task['binding_error'] = binding_errors.get(task['id'])
            for item in items:
                item['bindings'] = [dict(id=task['id'], label=task['label'], mode=task['binding']['mode'])
                                    for task in tasks if (task.get('binding') or {}).get('file') == item['file']]
        except (OSError, ValueError) as error:
            binding_errors = {'catalog': str(error)}
        binding_errors.update(library_errors)
        return dict(items=items, selected_file=selected, root=str(self.directory),
                    task_bindings=bindings, binding_errors=binding_errors, tasks=tasks, binding_revision=binding_revision)

    def dispatch(self, method, params=None):
        if self._closed:
            raise RuntimeError("脚本工具服务已关闭。")
        params = params or {}
        if method == "list":
            return self.list()
        if method == 'execution_plan':
            return execution_plan(self.root, params.get('name'), params.get('entry'), params.get('overrides'),
                                  report_unbound=params.get('report_unbound', False))
        if method == 'unbind_task':
            self.require_idle()
            return unbind_task_recording(self.directory, params.get('task_id'), expected_file=params.get('expected_file'))
        if method in ('delete_file', 'duplicate_file', 'rename_file'):
            self.require_idle()
            filename = params.get('file')
            if filename == self.file: self.require_clean()
            if method == 'duplicate_file':
                return duplicate_recording(self.directory, filename, params.get('name'), params.get('revision'))
            if method == 'rename_file':
                result = rename_recording(self.directory, filename, params.get('name'), params.get('revision'))
                if self.file == filename:
                    data, revision = read_json(self.path(result['file']))
                    self.open_document(RecordingDocument.from_dict(data), file=result['file'], revision=revision)
                result['document'] = self.snapshot() if self.document is not None else dict(empty=True)
                return result
            result = delete_recording(self.directory, filename, params.get('revision'))
            if self.file == filename:
                self.document = None
                self.file, self.revision, self.dirty = None, None, False
                self._clear_history()
            result['document'] = self.snapshot() if self.document is not None else dict(empty=True)
            return result
        if method in ('bind_file', 'set_file_bindings'):
            self.require_idle()
            filename = params.get('file')
            if filename == self.file: self.require_clean()
            tasks = {item['id']: item for item in task_catalog(self.root)}
            targets = params.get('tasks') if method == 'set_file_bindings' else {params.get('task_id'):params.get('mode','pipeline')}
            if not isinstance(targets, dict): raise ValueError('任务选择格式无效。')
            for ident, mode in targets.items():
                if ident not in tasks: raise ValueError('请选择当前工程中的任务。')
                if mode not in tasks[ident]['modes']: raise ValueError('该任务没有战斗回放节点；如需替代整个任务，请明确选择此方式。')
            if not targets:
                revision = hashlib.sha256(self.path(filename).read_bytes()).hexdigest()
                if revision != params.get('revision'): raise ValueError('脚本已被其他窗口修改，请刷新后重试。')
                return set_file_bindings(self.directory, filename, {}, revision, params.get('binding_revision'))
            saved, revision = read_json(self.path(filename))
            if revision != params.get('revision'): raise ValueError('脚本已被其他窗口修改，请刷新后重试。')
            document = RecordingDocument.from_dict(saved)
            document.to_dict(strict=True)
            if not saved.get('actions') or document.diagnostics or min(document.base) != 720:
                raise ValueError('请先保存有完整操作的720基准脚本。')
            if method == 'set_file_bindings':
                return set_file_bindings(self.directory, filename, targets, revision, params.get('binding_revision'))
            return bind_task_recording(self.directory, params['task_id'], filename, expected_revision=revision, mode=params.get('mode','pipeline'))
        if method == 'capture_cancel':
            if self.capture is None or self.capture.snapshot()['state'] != 'countdown':
                raise ValueError('仅录制开始前可以取消准备。')
            self.capture.stop()
            self.capture_finished = True
            return dict(cancelled=True, document=self.snapshot() if self.document else dict(empty=True))
        if method == "snapshot":
            return self.snapshot()
        if method == "capture_status":
            return self.capture.snapshot() if self.capture else dict(state="idle", elapsed_ms=0, event_count=0, error="")
        if method == "capture_stop":
            if self.capture is None:
                raise ValueError("没有正在进行的录制。")
            payload = self.capture.stop()
            if not self.capture_finished:
                self.document = RecordingDocument.from_dict(payload)
                self.file, self.revision = None, None
                self._clear_history()
                self.dirty = True
                self.capture_finished = True
            return self.snapshot()
        if method in {"capture_pause", "capture_resume", "capture_mark"}:
            if self.capture is None:
                raise ValueError("没有正在进行的录制。")
            if method == "capture_pause":
                self.capture.pause()
            elif method == "capture_resume":
                self.capture.resume(countdown_seconds=3)
            else:
                self.capture.mark(params.get("label") or "阶段标记")
            return self.capture.snapshot()
        self.require_idle()
        if method in {"load", "import", "capture_start", "new"}:
            self.require_clean()
        if method == "load":
            file = params["file"]
            data, revision = read_json(self.path(file))
            document = RecordingDocument.from_dict(data)
            return self.open_document(document, file=file, revision=revision)
        if method == "import":
            data, _ = read_json(Path(params["path"]))
            document = (RecordingDocument.from_source_macro(data) if isinstance(data, dict) and data.get("schema") == "zaomeng.macro"
                        else RecordingDocument.from_dict(data))
            return self.open_document(document, edited=True)
        if method == "new":
            return self.open_document(RecordingDocument.from_dict(dict(version=1, name="新脚本", base=[1148, 720], actions=[])), edited=True)
        if method == "discard":
            if self.file:
                data, revision = read_json(self.path(self.file))
                return self.open_document(RecordingDocument.from_dict(data), file=self.file, revision=revision)
            self.document = None
            self.dirty = False
            self._clear_history()
            return dict(empty=True, dirty=False)
        if method == "capture_start":
            if self.capture is not None:
                self.capture.close()
            if self.capture_factory is None:
                from script_studio.capture import CaptureSession
                self.capture_factory = CaptureSession
            self.capture = self.capture_factory()
            self.capture_finished = False
            self.capture.start(int(params["hwnd"]), countdown_seconds=int(params.get("countdown_seconds", 3)))
            return self.capture.snapshot()
        document = self.require_document()
        if method == "set_duration":
            return self.replace(document.set_duration(params["duration_ms"]))
        if method == "edit":
            return self.replace(document.edit(params["id"], params["changes"]))
        if method == "insert":
            return self.replace(document.insert(params["changes"]))
        if method == "delete":
            return self.replace(document.delete(params["id"]))
        if method == "delete_many":
            return self.replace(document.delete_many(params["ids"]))
        if method == "move_many":
            return self.replace(document.move_many(params["ids"], params["delta_ms"]))
        if method == "marker":
            return self.replace(document.add_marker(params["at_ms"], params.get("label") or "阶段标记"))
        if method == "repair":
            return self.replace(document.repair_releases())
        if method in {"undo", "redo"}:
            source, target = (self.undo, self.redo) if method == "undo" else (self.redo, self.undo)
            if not source:
                raise ValueError("没有可以撤销或重做的修改。")
            restored = self._pop_history(source)
            self._push_history(target, document)
            self.document = restored
            self.dirty = True
            return self.snapshot()
        if method == "rename":
            data = document.to_dict(strict=False)
            data["name"] = params["name"].strip()
            return self.replace(RecordingDocument.from_dict(data))
        if method == "save":
            name = params.get("file") or self.file
            if not name:
                name = document.snapshot()["name"].strip() + ".json"
            path = self.path(name)
            existed = path.exists()
            if existed:
                if name != self.file or self.revision is None:
                    raise ValueError("同名脚本已存在，请使用另一个名称。")
                if read_json(path)[1] != self.revision:
                    raise ValueError("文件已被其他程序修改，未覆盖。请导出当前草稿后重新加载。")
            revision = atomic_json(path, document.to_dict(), overwrite=existed)
            self.file, self.revision = name, revision
            self.dirty = False
            return self.snapshot()
        if method == "export":
            target = Path(params["path"])
            resolved = target.resolve()
            if self.file and resolved == self.path(self.file).resolve():
                raise ValueError("导出目标是当前脚本，请使用保存。")
            in_library = (resolved.is_relative_to(self.directory.resolve())
                          or target.absolute().is_relative_to(self.directory.absolute()))
            if in_library and target.exists():
                raise ValueError("导出不能覆盖脚本库中的已有文件，请选择新路径或使用保存。")
            atomic_json(target, document.to_dict(), overwrite=not in_library)
            return dict(path=params["path"])
        if method == "select":
            self.require_clean()
            if not self.file:
                raise ValueError("请先保存脚本。")
            if read_json(self.path(self.file))[1] != self.revision:
                raise ValueError("文件已被其他程序修改，请重新加载后选择。")
            # @selected resolves this at playback time, so the UI does not need
            # to restart to see a newly selected recording.
            atomic_json(self.directory / ".selected.json", dict(file=self.file))
            return dict(file=self.file, name=document.snapshot()["name"])
        if method == "bind_task":
            self.require_clean()
            if not self.file or self.revision is None:
                raise ValueError("请先保存脚本。")
            task_id = params.get("task_id")
            task = next((item for item in task_catalog(self.root) if item['id'] == task_id), None)
            if task is None:
                raise ValueError("请选择当前工程中的具体任务。")
            mode = params.get('mode', task['modes'][0])
            if mode not in task['modes']:
                raise ValueError('该任务没有脚本回放节点，请明确选择替代任务执行。')
            saved, revision = read_json(self.path(self.file))
            if revision != self.revision:
                raise ValueError("文件已被其他程序修改，请重新加载后绑定。")
            saved_document = RecordingDocument.from_dict(saved)
            saved_document.to_dict()
            if not saved.get("actions"):
                raise ValueError("空白脚本不能绑定到任务。")
            if min(saved_document.base) != 720:
                raise ValueError("任务脚本必须使用短边720坐标，请先通过脚本库导入转换。")
            return bind_task_recording(self.directory, task_id, self.file, expected_revision=self.revision, mode=mode)
        if method == "split":
            left, right = document.split(params["at_ms"])
            paths = []
            for suffix, part in (("前段", left), ("后段", right)):
                stem = document.snapshot()["name"] + "-" + suffix
                name, number = stem + ".json", 2
                while self.path(name).exists():
                    name, number = f"{stem}-{number}.json", number + 1
                paths.append((self.path(name), part.to_dict()))
            created = []
            try:
                for path, data in paths:
                    atomic_json(path, data, overwrite=False)
                    created.append(path)
            except Exception:
                for path in created:
                    if path.exists():
                        path.unlink()
                raise
            return dict(files=[path.name for path, _ in paths])
        raise ValueError("不支持的脚本工具操作：" + method)

    def close(self):
        # A crash/host shutdown must not discard an unfinished recording. Drafts
        # are separate files and never overwrite a user's saved recording.
        if self._closed:
            return
        errors = []
        if self.capture is not None:
            try:
                if not self.capture_finished:
                    payload = self.capture.stop()
                    if payload.get("actions"):
                        atomic_json(self.recovery_path("录制"), payload, overwrite=False)
            except Exception as error:
                errors.append(error)
            try:
                self.capture.close()
            except Exception as error:
                errors.append(error)
        if self.dirty and self.document is not None:
            try:
                atomic_json(self.recovery_path("草稿"), self.document.to_dict(strict=False), overwrite=False)
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError("关闭脚本工具时未能完成清理：" + "；".join(str(error) for error in errors))
        self._closed = True

    def recovery_path(self, kind):
        return self.directory / ("恢复" + kind + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8] + ".json")


def main():
    # Redirected Windows Python streams otherwise use the system ANSI codepage,
    # while the Avalonia transport reads and writes UTF-8 JSON lines.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    service = StudioService(args.root)
    try:
        for line in sys.stdin:
            request_id = None
            try:
                if len(line.encode("utf-8")) > MAX_BYTES:
                    raise ValueError("请求过大。")
                request = json.loads(line)
                request_id = request.get("id")
                if request["method"] == "shutdown":
                    break
                with contextlib.redirect_stdout(sys.stderr):
                    result = service.dispatch(request["method"], request.get("params"))
                response = dict(id=request_id, ok=True, result=result)
            except Exception as error:
                response = dict(id=request_id, ok=False, error=str(error))
            print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            service.close()


if __name__ == "__main__":
    main()
