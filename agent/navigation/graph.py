"""Pure screen identification and route planning for the assistant.

This module only interprets data. Maa supplies recognition hits and executes
edge pipelines; the runner owns cancellation, timeouts and retry accounting.
There are no device, Qt or Maa imports here.
"""
from collections import deque
from copy import deepcopy
import heapq
import itertools
import math


class NavigationError(ValueError):
    """A valid graph could not identify a screen or supply a usable route."""

    def __init__(self, message, *, reason, candidates=()):
        super().__init__(message)
        self.reason = reason
        self.candidates = tuple(candidates)


def _text(value, label, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label}应为 1～{limit} 字符的非空文本。")


def _integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}应为 {minimum}～{maximum} 的整数。")


def _names(value, label, *, nonempty=False):
    if not isinstance(value, list) or len(value) > 5000 or nonempty and not value:
        raise ValueError(f"{label}需要有效的{'非空' if nonempty else ''}列表。")
    for name in value:
        _text(name, label)
    if len(set(value)) != len(value):
        raise ValueError(f"{label}不能包含重复引用。")


def _validate(nav, pipeline=None):
    if not isinstance(nav, dict) or type(nav.get("version")) is not int or nav["version"] != 1:
        raise ValueError("不支持的界面导航图版本。")
    allowed = {"version", "targets", "nodes", "edges", "max_steps", "max_seconds", "settle_ms", "locate_attempts", "abort"}
    if set(nav) - allowed:
        raise ValueError("界面导航图包含不支持的字段；定位不能使用优先级。")
    for key, minimum, maximum in (("max_steps", 1, 1000), ("max_seconds", 1, 3600),
                                  ("settle_ms", 0, 10000), ("locate_attempts", 1, 20)):
        _integer(nav.get(key), key, minimum, maximum)
    _names(nav.get("targets"), "导航目标", nonempty=True)
    nodes, edges = nav.get("nodes"), nav.get("edges")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 1000:
        raise ValueError("导航图需要 1～1000 个界面节点。")
    if not isinstance(edges, list) or len(edges) > 5000:
        raise ValueError("导航图操作边需要列表，最多 5000 条。")
    if pipeline is not None and not isinstance(pipeline, dict):
        raise ValueError("导航图需要有效的 Pipeline 节点字典。")
    ids = set()
    abort = nav.get('abort', {})
    if not isinstance(abort, dict) or len(abort) > 100:
        raise ValueError('导航中止条件无效。')
    for name, message in abort.items():
        _text(name, '中止识别项')
        _text(message, '中止提示', 500)
    detectors = set(abort)
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"id", "label", "recognition", "any", "exclude", "obscures"}:
            raise ValueError("导航界面节点字段无效；不支持隐式优先级。")
        _text(node.get("id"), "界面标识")
        _text(node.get("label"), "界面名称", 200)
        if node["id"] in ids:
            raise ValueError("导航界面标识不能重复。")
        ids.add(node["id"])
        _names(node.get("recognition"), "界面必要识别条件", nonempty=True)
        for key in ("any", "exclude", "obscures"):
            _names(node.get(key, []), "界面 " + key)
        if set(node["recognition"]) & set(node.get("exclude", [])):
            raise ValueError("界面必要识别条件与排除条件不能矛盾。")
        if node.get("any") and set(node["any"]).issubset(node.get("exclude", [])):
            raise ValueError("界面备选识别条件不能全部被排除。")
        detectors.update(node["recognition"] + node.get("any", []) + node.get("exclude", []))
    if not set(nav["targets"]).issubset(ids):
        raise ValueError("导航目标引用了不存在的界面。")
    for node in nodes:
        if not set(node.get("obscures", [])).issubset(ids):
            raise ValueError("遮挡关系引用了不存在的界面。")
    # Kahn's algorithm keeps malformed deep graphs away from recursion limits.
    obscures = {node["id"]: node.get("obscures", []) for node in nodes}
    incoming = {key: 0 for key in ids}
    for children in obscures.values():
        for child in children:
            incoming[child] += 1
    pending = deque(key for key, count in incoming.items() if count == 0)
    visited = 0
    while pending:
        key = pending.popleft()
        visited += 1
        for child in obscures[key]:
            incoming[child] -= 1
            if incoming[child] == 0:
                pending.append(child)
    if visited != len(nodes):
        raise ValueError("界面遮挡关系不能包含环或自遮挡。")
    edge_ids = set()
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) - {"id", "source", "target", "label", "entry", "cost", "timeout_ms", "max_attempts"}:
            raise ValueError("导航操作边字段无效。")
        for key in ("id", "source", "target", "entry"):
            _text(edge.get(key), "操作边 " + key)
        _text(edge.get("label"), "操作名称", 200)
        if edge["id"] in edge_ids:
            raise ValueError("导航操作边标识不能重复。")
        edge_ids.add(edge["id"])
        if edge["source"] not in ids or edge["target"] not in ids:
            raise ValueError("导航操作边引用了不存在的界面。")
        cost = edge.get("cost")
        if type(cost) not in (float, int) or not math.isfinite(cost) or not 0 < cost <= 1000000:
            raise ValueError("导航边代价应为大于 0 且不超过 1000000 的有限数值。")
        _integer(edge.get("timeout_ms"), "操作边超时", 1, 120000)
        _integer(edge.get("max_attempts"), "操作边尝试次数", 1, 2)
        if pipeline is not None and edge["entry"] not in pipeline:
            raise ValueError("导航操作边入口不存在于 Pipeline。")
    if pipeline is not None:
        for detector in detectors:
            node = pipeline.get(detector)
            if (not isinstance(node, dict) or not isinstance(node.get("action"), dict)
                    or node["action"].get("type") != "DoNothing"):
                raise ValueError(f"导航识别节点必须存在且使用只读 DoNothing 动作：{detector}")


def validate_navigation(nav, pipeline):
    """Validate a complete navigation snapshot without changing either input."""
    if not isinstance(pipeline, dict):
        raise ValueError("导航图需要有效的 Pipeline 节点字典。")
    _validate(nav, pipeline)


def select_state(nav, hitnames):
    """Identify one visible state from recognition hits from the same frame.

    Every ``recognition`` must hit, at least one optional ``any`` must hit,
    and no ``exclude`` may hit. Only explicit transitive ``obscures`` relations
    can resolve simultaneous matches. List order never breaks a tie.
    """
    _validate(nav)
    if not isinstance(hitnames, (set, frozenset)) or any(not isinstance(name, str) for name in hitnames):
        raise ValueError("同帧识别结果需要节点名称集合。")
    candidates = [node["id"] for node in nav["nodes"]
                  if set(node["recognition"]).issubset(hitnames)
                  and (not node.get("any") or hitnames.intersection(node["any"]))
                  and not hitnames.intersection(node.get("exclude", []))]
    if not candidates:
        raise NavigationError("当前界面尚未识别；没有发送导航操作。", reason="unknown")
    by_id = {node["id"]: node for node in nav["nodes"]}
    hidden = set()
    # Reachability traverses unmatched intermediate overlays too: a dialog can
    # cover a panel whose identity is occluded while that panel's background hits.
    for candidate in candidates:
        pending = list(by_id[candidate].get("obscures", []))
        while pending:
            key = pending.pop()
            if key not in hidden:
                hidden.add(key)
                pending.extend(by_id[key].get("obscures", []))
    remaining = [candidate for candidate in candidates if candidate not in hidden]
    if len(remaining) != 1:
        labels = "、".join(by_id[key]["label"] for key in remaining)
        raise NavigationError("当前界面存在歧义，无法确定：" + labels, reason="ambiguous", candidates=remaining)
    return remaining[0]


def shortest_path(nav, start, blocked_edges=()):
    """Return a deterministic cheapest route to any target within max_steps.

    Positive edge costs make ordinary navigation cycles harmless. Search state
    includes the step count so a cheap long route cannot suppress a feasible
    shorter route under the step budget. Runtime owns the wall-clock budget.
    """
    _validate(nav)
    ids = {node["id"] for node in nav["nodes"]}
    if not isinstance(start, str) or start not in ids:
        raise NavigationError("当前界面不在导航图中。", reason="invalid_start")
    if not isinstance(blocked_edges, (tuple, list, set, frozenset)) or any(not isinstance(key, str) for key in blocked_edges):
        raise ValueError("禁用操作边应为边标识集合或列表。")
    blocked = set(blocked_edges)
    if not blocked.issubset({edge["id"] for edge in nav["edges"]}):
        raise ValueError("禁用操作边引用了不存在的边。")
    targets = set(nav["targets"])
    if start in targets:
        return []
    adjacent = {key: [] for key in ids}
    for edge in nav["edges"]:
        if edge["id"] not in blocked:
            adjacent[edge["source"]].append(edge)
    counter = itertools.count()
    queue = [(0, next(counter), start, 0)]
    best = {(start, 0): 0}
    previous = {}
    # A positive-cost optimum never needs to revisit a node.
    step_limit = min(nav["max_steps"], len(ids) - 1)
    while queue:
        cost, _, node, steps = heapq.heappop(queue)
        if cost != best.get((node, steps)):
            continue
        if node in targets:
            path = []
            cursor = (node, steps)
            while cursor != (start, 0):
                cursor, edge = previous[cursor]
                path.append(edge)
            return deepcopy(path[::-1])
        if steps >= step_limit:
            continue
        for edge in adjacent[node]:
            state = edge["target"], steps + 1
            candidate = cost + edge["cost"]
            if candidate < best.get(state, math.inf):
                best[state] = candidate
                previous[state] = ((node, steps), edge)
                heapq.heappush(queue, (candidate, next(counter), state[0], state[1]))
    # Distinguish missing connectivity from an otherwise reachable route that
    # cannot fit in the allowed number of navigation operations.
    reached, pending = {start}, deque([start])
    while pending:
        for edge in adjacent[pending.popleft()]:
            if edge["target"] in targets:
                raise NavigationError("到达目标所需操作超过导航步数限制。", reason="budget")
            if edge["target"] not in reached:
                reached.add(edge["target"])
                pending.append(edge["target"])
    raise NavigationError("当前界面到任务目标没有可用路径，请检查导航适配。", reason="unreachable")
