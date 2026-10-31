"""Pure recording edits, adapted from ZaoMengScript's macros/macro_editing.

The migrated pairing, neutral split and drag-retiming rules use absolute times
in memory. Disk files retain MAAZMXY4's relative ``actions/t`` and 720p ``base``.
Legacy atomic key/click/text/scroll actions and extra JSON fields are preserved;
loading never silently repairs input. No hooks, GUI, filesystem or Maa imports.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
from uuid import uuid4, uuid5, NAMESPACE_URL

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000
KNOWN_OPS = {"key", "keydown", "keyup", "click", "down", "move", "up", "text", "scroll"}
_KEY_OPS = {"key", "keydown", "keyup"}
_COORD_OPS = {"click", "down", "move"}


def _time(value, label="时间"):
    if (type(value) not in (int, float) or not 0 <= value <= MAX_DURATION_MS
            or not math.isfinite(value)):
        raise ValueError(f"{label}必须是非负有限毫秒数，最长 7 天")
    return value


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须是 {low}～{high} 的整数")
    return value


def _text(value, label, maximum=10000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{label}不能为空，且不能超过 {maximum} 字符")
    return value


def _json_size(value):
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("录制必须是有限数值组成的 JSON 数据") from exc
    if size > MAX_FILE_BYTES:
        raise ValueError("录制文件不能超过 16 MiB")


def _event(ident, at, payload, previous=None):
    return dict(id=ident, at_ms=at, payload=deepcopy(payload),
                original_at=at, original_previous=previous)


class RecordingDocument:
    """Immutable-by-convention edits; every public edit returns a deep copy.

    ``from_dict`` accepts incomplete legacy pairs with diagnostics. ``to_dict``
    refuses unsafe saves by default; use ``strict=False`` only for a backup or
    inspection, or explicitly call ``repair_releases`` before normal saving.
    """

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("录制必须是 JSON 对象")
        _json_size(data)
        raw = deepcopy(data)
        _text(raw.get("name", "未命名录制"), "录制名称", 100)
        base = raw.get("base", [1147, 720])
        if not isinstance(base, (list, tuple)) or len(base) != 2:
            raise ValueError("base 必须包含720基准的宽度和高度")
        for dimension in base:
            _integer(dimension, 1, 32768, "基准尺寸")
        actions = raw.get("actions")
        if not isinstance(actions, list) or len(actions) > MAX_EVENTS:
            raise ValueError("actions 必须是最多 100000 个动作的列表")
        editor = raw.get("editor", {})
        if not isinstance(editor, dict):
            raise ValueError("editor 必须是对象")
        ids = editor.get("event_ids", [])
        if not isinstance(ids, list):
            raise ValueError("editor.event_ids 必须是列表")
        events, used, at = [], set(), 0
        for index, action in enumerate(actions):
            if not isinstance(action, dict) or not isinstance(action.get("op"), str):
                raise ValueError(f"动作 {index+1} 必须是含 op 的对象")
            previous = at
            at += _time(action.get("t", 0), f"动作 {index+1} 间隔")
            _time(at, "总时长")
            ident = ids[index] if index < len(ids) else None
            if not isinstance(ident, str) or not ident or ident in used:
                # Deterministic IDs let a library refresh preserve selection even
                # before editor metadata has first been written to a legacy file.
                ident = str(uuid5(NAMESPACE_URL, f"maazmxy4:{index}:" + json.dumps(action, sort_keys=True)))
            used.add(ident)
            events.append(_event(ident, at, action, previous))
        markers = deepcopy(editor.get("markers", []))
        if not isinstance(markers, list) or len(markers) + len(events) > MAX_EVENTS:
            raise ValueError("动作与标记合计不能超过 100000 条")
        for marker in markers:
            if not isinstance(marker, dict):
                raise ValueError("标记必须是对象")
            _time(marker.get("at_ms"), "标记时间")
            _text(marker.get("label"), "标记", 256)
            ident = marker.get("id")
            if not isinstance(ident, str) or not ident or ident in used:
                ident = str(uuid4())
            marker["id"] = ident
            used.add(ident)
        duration = _time(raw.get("duration_ms", max([at] + [m["at_ms"] for m in markers])), "总时长")
        if duration < at or any(m["at_ms"] > duration for m in markers):
            raise ValueError("总时长不能小于最后一个动作或标记的时间")
        obj = cls.__new__(cls)
        obj._raw, obj._events, obj._markers = raw, events, markers
        obj.duration_ms = duration
        obj._edited = False
        end_ids = obj._end_marker_ids()
        for marker in obj._markers:
            if marker['id'] in end_ids and marker['at_ms'] != duration:
                marker.setdefault('recorded_at_ms', marker['at_ms'])
                marker['at_ms'] = duration
        return obj

    @classmethod
    def from_source_macro(cls, data):
        """Import zaomeng.macro v1, explicitly adapting pixels and wheel units."""
        if (not isinstance(data, dict) or data.get("schema") != "zaomeng.macro"
                or type(data.get("version")) is not int or data["version"] != 1):
            raise ValueError("需要 schema=zaomeng.macro、version=1 的源录制")
        _json_size(data)
        window, source_events = data.get("window"), data.get("events")
        if not isinstance(window, dict) or not isinstance(source_events, list) or len(source_events) > MAX_EVENTS:
            raise ValueError("源窗口或事件列表无效")
        width = _integer(window.get("width"), 1, 32768, "源窗口宽度")
        height = _integer(window.get("height"), 1, 32768, "源窗口高度")
        scale = 720 / min(width, height)
        base = [round(width*scale), round(height*scale)]
        actions, markers, metadata = [], [], []
        previous, last_source = 0, 0
        mapping = {"key_down": "keydown", "key_up": "keyup", "mouse_down": "down",
                   "mouse_up": "up", "mouse_move": "move", "wheel": "scroll"}
        for index, source in enumerate(source_events):
            if not isinstance(source, dict) or not isinstance(source.get("data"), dict):
                raise ValueError(f"源事件 {index+1} 无效")
            at, kind, payload = _time(source.get("t_ms")), source.get("kind"), deepcopy(source["data"])
            if at < last_source:
                raise ValueError("源事件必须按时间排序")
            last_source = at
            if kind == "marker":
                markers.append(dict(id=str(uuid4()), at_ms=at, label=_text(payload.get("label"), "标记", 256)))
                continue
            if not isinstance(kind, str) or kind not in mapping:
                raise ValueError(f"不支持的源事件 {kind}")
            action = dict(t=at-previous, op=mapping[kind])
            previous = at
            if kind.startswith("key_"):
                action["key"] = _integer(payload.get("vk"), 1, 255, "源键码")
            else:
                x = _integer(payload.get("x"), 0, width-1, "源 X 坐标")
                y = _integer(payload.get("y"), 0, height-1, "源 Y 坐标")
                action.update(x=min(base[0]-1, round(x*scale)), y=min(base[1]-1, round(y*scale)))
                if kind in {"mouse_down", "mouse_up"}:
                    button = payload.get("button")
                    if not isinstance(button, str) or button not in {"left", "right", "middle"}:
                        raise ValueError("源鼠标按钮无效")
                    action["button"] = button
                elif kind == "wheel":
                    delta = _integer(payload.get("delta"), -(2**31), 2**31-1, "源滚轮数值")
                    if delta == 0 or delta % 120:
                        raise ValueError("源滚轮 delta 必须是非零的 120 整倍数，不能静默改变高精度滚轮")
                    axis = payload.get("axis")
                    if axis not in {"horizontal", "vertical"}:
                        raise ValueError("源滚轮轴必须是 horizontal/vertical")
                    action.update(dx=delta//120 if axis == "horizontal" else 0,
                                  dy=delta//120 if axis == "vertical" else 0)
            actions.append(action)
            metadata.append(payload)
        duration = _time(data.get("duration_ms", last_source), "源总时长")
        if duration < last_source:
            raise ValueError("源总时长小于最后一个事件")
        raw = dict(version=1, name=data.get("name", "导入录制"), base=base, actions=actions,
                   duration_ms=duration, editor=dict(markers=markers,
                       source_macro={k: deepcopy(v) for k,v in data.items() if k != "events"}))
        if "notes" in data:
            raw["notes"] = deepcopy(data["notes"])
        result = cls.from_dict(raw)
        result._raw["editor"]["source_events"] = {
            event["id"]: payload for event,payload in zip(result._events, metadata)}
        # Imported data gets persistent IDs immediately so its retained scan,
        # extended and synthetic metadata remain associated after edits.
        result._edited = True
        result.to_dict(strict=False)
        return result

    @property
    def name(self):
        return self._raw.get("name", "未命名录制")

    @property
    def base(self):
        return list(self._raw.get("base", [1147, 720]))

    def _analysis(self):
        """Derive paired operations without rewriting any underlying action."""
        operations, diagnostics, keys, mice = [], [], {}, {}
        def operation(event, kind=None, end=None, ids=None, shared=False):
            payload, start = event["payload"], event["at_ms"]
            op = payload["op"]
            kind = kind or op
            labels = {"key_hold": "按住按键", "mouse_hold": "鼠标按住/拖拽", "key": "按键",
                      "click": "点击", "move": "鼠标移动", "text": "输入文字", "scroll": "滚轮"}
            result = dict(id=event["id"], kind=kind, op=op, label=labels.get(kind, op),
                          start_ms=start, duration_ms=0 if end is None else end-start,
                          event_ids=ids or [event["id"]])
            for field in ("key", "x", "y", "dx", "dy", "text", "button"):
                if field in payload:
                    result[field] = deepcopy(payload[field])
            if op in {"down", "up"}:
                result["button"] = payload.get("button", "left")
            if shared:
                result["shared_path"] = True
            operations.append(result)
        for event in self._events:
            payload, op = event["payload"], event["payload"]["op"]
            try:
                if op not in KNOWN_OPS:
                    raise ValueError(f"不支持的动作 {op}")
                if op in _KEY_OPS:
                    _integer(payload.get("key"), 1, 255, "键码")
                if op in _COORD_OPS or (op == "up" and ("x" in payload or "y" in payload)):
                    _integer(payload.get("x"), 0, self.base[0]-1, "X 坐标")
                    _integer(payload.get("y"), 0, self.base[1]-1, "Y 坐标")
                if op == "text":
                    _text(payload.get("text"), "输入文字", empty=True)
                if op == "scroll":
                    for field in ("dx", "dy"):
                        _integer(payload.get(field, 0), -(2**31), 2**31-1, "滚轮数值")
                if op in {"down", "up"}:
                    button = payload.get("button", "left")
                    if not isinstance(button, str) or button not in {"left", "right", "middle"}:
                        raise ValueError("鼠标按钮必须是 left/right/middle")
            except ValueError as exc:
                diagnostics.append(f"{event['id']}: {exc}")
            if op == "keydown":
                key = payload.get("key")
                # Invalid/unhashable legacy payloads still remain inspectable.
                if type(key) is not int:
                    operation(event)
                elif key in keys:
                    diagnostics.append(f"键 {key} 重复按下")
                    operation(event)
                else:
                    keys[key] = event
            elif op == "keyup":
                key = payload.get("key")
                down = keys.pop(key, None) if type(key) is int else None
                if down:
                    operation(down, "key_hold", event["at_ms"], [down["id"], event["id"]])
                else:
                    diagnostics.append(f"键 {key} 释放没有对应按下")
                    operation(event)
            elif op == "down":
                button = payload.get("button", "left")
                if not isinstance(button, str):
                    operation(event)
                elif button in mice:
                    diagnostics.append(f"鼠标 {button} 重复按下")
                    operation(event)
                else:
                    mice[button] = dict(event=event, ids=[event["id"]], shared=False)
            elif op == "move" and mice:
                for drag in mice.values():
                    drag["ids"].append(event["id"])
                    drag["shared"] |= len(mice) > 1
            elif op == "up":
                button = payload.get("button", "left")
                drag = mice.pop(button, None) if isinstance(button, str) else None
                if drag:
                    operation(drag["event"], "mouse_hold", event["at_ms"],
                              drag["ids"] + [event["id"]], drag["shared"])
                else:
                    diagnostics.append(f"鼠标 {button} 释放没有对应按下")
                    operation(event)
            else:
                operation(event)
        for key, down in keys.items():
            diagnostics.append(f"键 {key} 未释放")
            operation(down)
        for button, drag in mice.items():
            diagnostics.append(f"鼠标 {button} 未释放")
            operation(drag["event"], "mouse_unpaired", ids=drag["ids"], shared=drag["shared"])
        for marker in self._markers:
            operations.append(dict(id=marker["id"], kind="marker", op="marker", label=marker["label"],
                                   start_ms=marker["at_ms"], duration_ms=0, event_ids=[marker["id"]]))
        order = {event["id"]: i for i, event in enumerate(self._events)}
        operations.sort(key=lambda item: (item["start_ms"], order.get(item["id"], MAX_EVENTS)))
        return operations, diagnostics

    @property
    def diagnostics(self):
        return self._analysis()[1]

    def to_dict(self, strict=True):
        diagnostics = self.diagnostics
        if strict and diagnostics:
            raise ValueError("录制仍需修复：" + "；".join(diagnostics[:8]))
        raw, previous = deepcopy(self._raw), 0
        actions = []
        for event in self._events:
            action = deepcopy(event["payload"])
            # Preserve fractional legacy gaps exactly in an unchanged roundtrip.
            if event["at_ms"] == event["original_at"] and previous == event["original_previous"]:
                if "t" not in action and event["at_ms"] == previous:
                    pass
                else:
                    action["t"] = action.get("t", 0)
            else:
                action["t"] = event["at_ms"] - previous
            previous = event["at_ms"]
            actions.append(action)
        raw["actions"] = actions
        if self._edited or "duration_ms" in raw:
            raw["duration_ms"] = self.duration_ms
        if self._edited or "editor" in raw or self._markers:
            editor = deepcopy(raw.get("editor", {}))
            editor["event_ids"] = [event["id"] for event in self._events]
            editor["markers"] = deepcopy(self._markers)
            raw["editor"] = editor
        _json_size(raw)
        return raw

    def snapshot(self):
        operations, diagnostics = self._analysis()
        events = [dict(deepcopy(event["payload"]), id=event["id"], at_ms=event["at_ms"])
                  for event in self._events]
        for event in events:
            event.pop("t", None)
        return dict(name=self.name, base=self.base, duration_ms=self.duration_ms,
                    events=events, operations=operations, markers=deepcopy(self._markers),
                    diagnostics=diagnostics)

    def _operation(self, ident):
        for operation in self._analysis()[0]:
            if operation["id"] == ident or ident in operation["event_ids"]:
                return operation
        raise ValueError("操作已不存在，请刷新录制")

    def _finish(self, result, allow_new_diagnostics=False):
        end_ids = self._end_marker_ids()
        for marker in result._markers:
            if marker['id'] in end_ids:
                marker['at_ms'] = result.duration_ms
        result._markers.sort(key=lambda marker: marker['at_ms'])
        result._edited = True
        if len(result._events) + len(result._markers) > MAX_EVENTS:
            raise ValueError("动作与标记合计不能超过 100000 条")
        _time(result.duration_ms, "总时长")
        for event in result._events:
            _time(event["at_ms"])
        if not allow_new_diagnostics:
            added = Counter(result.diagnostics) - Counter(self.diagnostics)
            if added:
                raise ValueError("修改产生冲突：" + "；".join(list(added)[:5]))
        result.to_dict(strict=False)
        return result

    @staticmethod
    def _sort(events, moved_ids=(), start=0, end=0):
        moved_ids = set(moved_ids)
        def priority(event):
            if event["id"] not in moved_ids:
                return 0
            return -1 if event["at_ms"] == end and end > start else 1
        events.sort(key=lambda event: (event["at_ms"], priority(event)))

    def edit(self, operation_id, changes):
        if not isinstance(changes, dict):
            raise ValueError("修改参数必须是对象")
        operation = self._operation(operation_id)
        if operation.get("shared_path"):
            raise ValueError("此鼠标轨迹由多个按钮共用，不能单独修改")
        result = deepcopy(self)
        start = _time(changes.get("start_ms", changes.get("at_ms", operation["start_ms"])))
        if operation['kind'] == 'marker' and operation['id'] in self._end_marker_ids():
            result = self.set_duration(start)
            marker = next(m for m in result._markers if m['id'] == operation['id'])
            marker['anchor'] = 'script_end'
            marker['label'] = _text(changes.get('label', marker['label']), '标记', 256)
            return result
        hold = _time(changes.get("duration_ms", operation["duration_ms"]), "按住时长")
        _time(start+hold)
        if operation["kind"] == "marker":
            marker = next(m for m in result._markers if m["id"] == operation["id"])
            marker.update(at_ms=start, label=_text(changes.get("label", marker["label"]), "标记", 256))
            result._markers.sort(key=lambda m: m["at_ms"])
        else:
            ids = set(operation["event_ids"])
            edited = [e for e in result._events if e["id"] in ids]
            first, last = edited[0], edited[-1]
            if operation["kind"] not in {"key_hold", "mouse_hold"} and hold:
                raise ValueError("此原子动作没有按住时长，请插入成对操作")
            if "op" in changes and changes["op"] != first["payload"]["op"]:
                raise ValueError("请新增另一种操作，不能直接改变现有操作类型")
            if operation["kind"] == "key_hold":
                key = _integer(changes.get("key", first["payload"]["key"]), 1, 255, "键码")
                for event in edited:
                    if event["payload"]["key"] != key:
                        source = result._raw.get("editor", {}).get("source_events", {}).get(event["id"])
                        if isinstance(source, dict):
                            source.update(vk=key, scan=0, extended=False)
                    event["payload"]["key"] = key
            if operation["kind"] == "mouse_hold" and "button" in changes:
                button = changes["button"]
                if not isinstance(button, str) or button not in {"left", "right", "middle"}:
                    raise ValueError("鼠标按钮必须是 left/right/middle")
                first["payload"]["button"] = last["payload"]["button"] = button
            for field in ("key", "text", "dx", "dy"):
                if field in changes and operation["kind"] != "key_hold":
                    first["payload"][field] = deepcopy(changes[field])
            if "x" in changes or "y" in changes:
                if "x" not in first["payload"] or "y" not in first["payload"]:
                    raise ValueError("此操作没有可修改的坐标")
                _integer(first["payload"]["x"], 0, self.base[0]-1, "原 X 坐标")
                _integer(first["payload"]["y"], 0, self.base[1]-1, "原 Y 坐标")
                if "x" in changes:
                    _integer(changes["x"], 0, self.base[0]-1, "X 坐标")
                if "y" in changes:
                    _integer(changes["y"], 0, self.base[1]-1, "Y 坐标")
                dx = changes.get("x", first["payload"]["x"]) - first["payload"]["x"]
                dy = changes.get("y", first["payload"]["y"]) - first["payload"]["y"]
                for event in edited:
                    if "x" in event["payload"]:
                        event["payload"]["x"] += dx
                        event["payload"]["y"] += dy
            old_start, old_hold = operation["start_ms"], operation["duration_ms"]
            for event in edited:
                fraction = (event["at_ms"]-old_start)/old_hold if old_hold else 0
                event["at_ms"] = start + round(fraction*hold)
            if len(edited) > 1:
                last["at_ms"] = start+hold
            if hold:
                self._sort(result._events, ids, start, start+hold)
            elif len(edited) > 1:
                # Keep a zero-length down/path/up together, before a hold
                # starting at the same instant and after an ending release.
                rest = [e for e in result._events if e["id"] not in ids]
                index = next((i for i, e in enumerate(rest) if e["at_ms"] > start or
                              e["at_ms"] == start and e["payload"]["op"] in {"down", "keydown"}), len(rest))
                rest[index:index] = edited
                result._events = rest
            else:
                result._events.sort(key=lambda e: e["at_ms"])
        result.duration_ms = max(result.duration_ms, start+hold)
        if operation["kind"] == "mouse_hold" and result._operation(operation["id"]).get("shared_path"):
            raise ValueError("修改后的鼠标轨迹会由多个按钮共用，请先拆分录制")
        return self._finish(result)

    def insert(self, changes):
        if not isinstance(changes, dict):
            raise ValueError("新增参数必须是对象")
        kind = changes.get("kind", changes.get("op", "mouse_hold"))
        if kind == "marker":
            return self.add_marker(changes.get("at_ms", changes.get("start_ms", 0)), changes.get("label", "阶段"))
        start = _time(changes.get("start_ms", changes.get("at_ms", 0)))
        hold = _time(changes.get("duration_ms", 50 if kind == "mouse_hold" else 0), "按住时长")
        _time(start+hold)
        if kind not in KNOWN_OPS | {"key_hold", "mouse_hold"}:
            raise ValueError("不支持新增此类型操作")
        payload = deepcopy(changes.get("payload", {}))
        if not isinstance(payload, dict):
            raise ValueError("动作参数必须是对象")
        for field in ("key", "x", "y", "dx", "dy", "text", "button"):
            if field in changes:
                payload[field] = deepcopy(changes[field])
        result, additions = deepcopy(self), []
        if kind in {"key_hold", "mouse_hold"}:
            first, last = ("keydown", "keyup") if kind == "key_hold" else ("down", "up")
            payload["op"] = first
            additions.append(_event(str(uuid4()), start, payload))
            release = {"op": last}
            if kind == "key_hold":
                release["key"] = payload.get("key")
            elif "button" in payload:
                release["button"] = payload["button"]
            additions.append(_event(str(uuid4()), start+hold, release))
        else:
            if hold:
                raise ValueError("原子动作不支持按住时长")
            payload["op"] = kind
            additions.append(_event(str(uuid4()), start, payload))
        result._events.extend(additions)
        if hold or len(additions) == 1:
            self._sort(result._events, [e["id"] for e in additions], start, start+hold)
        else:
            result._events = result._events[:-2]
            index = next((i for i,e in enumerate(result._events) if e["at_ms"] > start or
                          e["at_ms"] == start and e["payload"]["op"] in {"down", "keydown"}), len(result._events))
            result._events[index:index] = additions
        result.duration_ms = max(result.duration_ms, start+hold)
        return self._finish(result)

    def delete(self, operation_id):
        operation = self._operation(operation_id)
        if operation.get("shared_path"):
            raise ValueError("此鼠标轨迹由多个按钮共用，不能单独删除")
        result = deepcopy(self)
        ids = set(operation["event_ids"])
        result._events = [e for e in result._events if e["id"] not in ids]
        result._markers = [m for m in result._markers if m["id"] not in ids]
        return self._finish(result)

    def _selection(self, operation_ids):
        if (not isinstance(operation_ids, list) or not operation_ids
                or len(operation_ids) > MAX_EVENTS
                or any(not isinstance(ident, str) for ident in operation_ids)):
            raise ValueError("请选择有效的操作列表")
        operations = self._analysis()[0]
        by_id = {operation['id']: operation for operation in operations}
        selected = set(operation_ids)
        if not selected.issubset(by_id):
            raise ValueError("部分操作已不存在，请刷新录制")
        result = [operation for operation in operations if operation['id'] in selected]
        events = {ident for operation in result for ident in operation['event_ids']}
        if any(events.intersection(operation['event_ids']) for operation in operations
               if operation['id'] not in selected):
            raise ValueError("鼠标轨迹由多个操作共用，请同时选中这些操作")
        return result, events

    def delete_many(self, operation_ids):
        """Delete complete holds and paths as one atomic edit, preserving duration."""
        _, ids = self._selection(operation_ids)
        result = deepcopy(self)
        result._events = [event for event in result._events if event['id'] not in ids]
        result._markers = [marker for marker in result._markers if marker['id'] not in ids]
        return self._finish(result)

    def move_many(self, operation_ids, delta_ms):
        """Translate the whole selection without changing relative timing or paths."""
        operations, ids = self._selection(operation_ids)
        if type(delta_ms) not in (int, float) or not math.isfinite(delta_ms):
            raise ValueError("移动距离必须是有限毫秒数")
        if min(operation['start_ms'] for operation in operations) + delta_ms < 0:
            raise ValueError("选中的操作不能移到时间轴起点之前")
        result = deepcopy(self)
        for item in [*result._events, *result._markers]:
            if item['id'] in ids:
                item['at_ms'] = _time(item['at_ms'] + delta_ms)
        # At a new boundary a moved release precedes a stationary press, while
        # a moved press follows a stationary release. Zero-length pairs retain
        # their original order, and all shared mouse events move exactly once.
        releases = {operation['event_ids'][-1] for operation in operations
                    if operation['kind'] in ('key_hold', 'mouse_hold') and operation['duration_ms'] > 0}
        result._events.sort(key=lambda event: (event['at_ms'],
                            -1 if event['id'] in releases else 1 if event['id'] in ids else 0))
        end_ids = self._end_marker_ids()
        last = max([0] + [item['at_ms'] for item in [*result._events, *result._markers]
                          if item['id'] not in end_ids])
        if ids.intersection(end_ids):
            result.duration_ms = _time(self.duration_ms + delta_ms)
            if last > result.duration_ms:
                raise ValueError("结束标记不能移到仍然存在的操作之前")
        else:
            result.duration_ms = max(self.duration_ms, last)
        return self._finish(result)

    def _end_marker_ids(self):
        # Legacy recorders wrote a stop marker (and sometimes a pause at the
        # same instant). These describe the endpoint, not a timed game input.
        ends = [m for m in self._markers if m.get('anchor') == 'script_end'
                or m['label'] in ('录制停止', '结束录制')]
        if not ends:
            return set()
        last_stop = max(m['at_ms'] for m in ends)
        return {m['id'] for m in self._markers if m in ends and m['at_ms'] == last_stop
                or m['label'] == '暂停录制' and m['at_ms'] == last_stop}

    def set_duration(self, duration_ms):
        duration = _time(duration_ms, "总时长")
        end_ids = self._end_marker_ids()
        last = max([0] + [event["at_ms"] for event in self._events]
                   + [marker["at_ms"] for marker in self._markers if marker['id'] not in end_ids])
        if duration < last:
            raise ValueError(f"总时长不能小于最后一个动作或标记的时间（{last / 1000:.3f} 秒）；请先移动或删除末尾操作。")
        result = deepcopy(self)
        result.duration_ms = duration
        return self._finish(result)

    def add_marker(self, at_ms, label):
        at_ms = _time(at_ms, "标记时间")
        label = _text(label, "标记", 256)
        result = deepcopy(self)
        result._markers.append(dict(id=str(uuid4()), at_ms=at_ms, label=label))
        result._markers.sort(key=lambda marker: marker["at_ms"])
        result.duration_ms = max(result.duration_ms, at_ms)
        return self._finish(result)

    def split(self, at_ms):
        at_ms = _time(at_ms, "拆分时间")
        if not 0 < at_ms < self.duration_ms or self.diagnostics:
            raise ValueError("只能拆分有效录制内部、所有键鼠已释放的位置")
        # A boundary within a shared timestamp may fall after a release but
        # before the next press. Preserve the original event order exactly.
        keys, buttons, candidates = set(), set(), []
        for index, event in enumerate(self._events):
            if event["at_ms"] > at_ms:
                break
            op, payload = event["payload"]["op"], event["payload"]
            if op == "keydown": keys.add(payload["key"])
            elif op == "keyup": keys.discard(payload["key"])
            elif op == "down": buttons.add(payload.get("button", "left"))
            elif op == "up": buttons.discard(payload.get("button", "left"))
            next_at = self._events[index+1]["at_ms"] if index+1 < len(self._events) else self.duration_ms
            if not keys and not buttons and event["at_ms"] <= at_ms <= next_at:
                candidates.append(index+1)
        candidates = [i for i in candidates if 0 < i < len(self._events)]
        if not candidates:
            raise ValueError("拆分点有键鼠尚未释放，或拆分后有一段没有操作")
        cut = candidates[-1]
        before, after = deepcopy(self), deepcopy(self)
        before._events, after._events = before._events[:cut], after._events[cut:]
        before.duration_ms, after.duration_ms = at_ms, self.duration_ms-at_ms
        before._markers = [m for m in before._markers if m["at_ms"] <= at_ms]
        after._markers = [m for m in after._markers if m["at_ms"] > at_ms]
        for event in after._events:
            event["at_ms"] -= at_ms
        for marker in after._markers:
            marker["at_ms"] -= at_ms
        before._raw["name"] = (self.name[:96] + " · 前段")[:100]
        after._raw["name"] = (self.name[:96] + " · 后段")[:100]
        return self._finish(before), self._finish(after)

    def repair_releases(self):
        """Explicitly append missing releases; never guess orphan or duplicate downs."""
        if any("未释放" not in diagnostic for diagnostic in self.diagnostics):
            raise ValueError("无法自动修复孤立释放、重复按下或无效动作，请手动修复冲突操作")
        keys, buttons = {}, set()
        for event in self._events:
            payload = event["payload"]
            op = payload["op"]
            if op == "keydown" and type(payload.get("key")) is int:
                keys[payload["key"]] = True
            elif op == "keyup" and type(payload.get("key")) is int:
                keys.pop(payload["key"], None)
            elif op == "down": buttons.add(payload.get("button", "left"))
            elif op == "up": buttons.discard(payload.get("button", "left"))
        result = deepcopy(self)
        for key in keys:
            result._events.append(_event(str(uuid4()), self.duration_ms,
                                        dict(op="keyup", key=key, synthetic=True, reason="显式修复未释放按键")))
        for button in sorted(buttons):
            result._events.append(_event(str(uuid4()), self.duration_ms,
                                        dict(op="up", button=button, synthetic=True, reason="显式修复未释放鼠标")))
        result = self._finish(result)
        if result.diagnostics:
            raise ValueError("无法自动修复孤立释放或重复按下，请手动删除冲突动作")
        return result
