"""Bounded graph navigation using the active Maa task's Context.

The host retains the task queue and controller. This module neither opens a
second Maa session nor sends device input outside native Pipeline actions.
"""
import time

from .graph import NavigationError, select_state, shortest_path


class GraphNavigator:
    def __init__(self, context, graph, *, capture, check, log=print, wait=time.sleep, clock=time.monotonic):
        self.context, self.graph = context, graph
        self.capture, self.check, self.log = capture, check, log
        self.wait, self.clock = wait, clock
        self.deadline = None
        self.steps, self.attempts = 0, {}
        self.nodes = {node['id']: node for node in graph['nodes']}
        self.obscures = {}
        for ident, node in self.nodes.items():
            hidden, pending = set(), list(node.get('obscures', []))
            while pending:
                child = pending.pop()
                if child not in hidden:
                    hidden.add(child)
                    pending.extend(self.nodes.get(child, {}).get('obscures', []))
            self.obscures[ident] = hidden
        # This changes evaluation order, not state-selection priority. Only a
        # positively identified overlay may suppress its declared backgrounds;
        # unrelated candidates still run and remain subject to ambiguity checks.
        self.probes = sorted(self.nodes.values(), key=lambda n:len(self.obscures[n['id']]), reverse=True)

    def _check(self):
        self.check()
        if self.deadline is not None and self.clock() >= self.deadline:
            raise TimeoutError('界面导航超过运行时限，已停止。')

    def _settle(self):
        end = self.clock() + self.graph.get('settle_ms', 300) / 1000
        while self.clock() < end:
            self._check()
            self.wait(min(.03, max(0, end - self.clock())))

    def locate(self):
        for attempt in range(self.graph['locate_attempts']):
            self._check()
            frame, hits, checked = self.capture(), set(), {}

            def hit(name):
                if name not in checked:
                    self._check()
                    detail = self.context.run_recognition(name, frame)
                    if detail is None:
                        raise ValueError('导航识别节点不可用：' + name)
                    checked[name] = bool(detail.hit)
                    self._check()
                    if checked[name]:
                        hits.add(name)
                return checked[name]

            for name, reason in self.graph.get('abort', {}).items():
                if hit(name):
                    raise NavigationError(reason, reason='blocked')
            hidden = set()
            for node in self.probes:
                if node['id'] in hidden:
                    continue
                if not all(hit(name) for name in node['recognition']):
                    continue
                if node.get('any') and not any(hit(name) for name in node['any']):
                    continue
                if any(hit(name) for name in node.get('exclude', [])):
                    continue
                hidden.update(self.obscures[node['id']])
            try:
                state = select_state(self.graph, hits)
            except NavigationError as error:
                if error.reason != 'unknown' or attempt + 1 == self.graph['locate_attempts']:
                    raise
                self.log('暂未识别当前界面，等待画面稳定后重新定位。')
                self._settle()
                continue
            self.log('当前界面：' + self.nodes[state]['label'])
            return state
        raise NavigationError('当前界面无法定位。', reason='unknown')

    def _execute(self, edge):
        self._check()
        self.log('正在导航：' + edge['label'])
        # Maa executes an entry without recognizing it. The wrapper makes the
        # first real edge node a recognized next, including a one-click edge.
        wrapper = 'Nav_InvokeEdge'
        # rate_limit 默认 1000ms：识别没命中就等满一秒再试，一个 8 秒的界面
        # 切换其实只是重试了 8 次（实测悬赏一轮 4 步 × 7~9 秒 = 30 秒）。压到
        # 200ms 后同样情况 1.6 秒就过，一轮省 20 秒以上。
        override = {wrapper: dict(recognition=dict(type='DirectHit'),
            action=dict(type='DoNothing'), next=[edge['entry']], post_delay=0,
            rate_limit=200,
            timeout=min(edge['timeout_ms'], max(1, int((self.deadline-self.clock())*1000))))}
        result = self.context.run_task(wrapper, override)
        self._check()
        if result is None or not result.status.succeeded:
            raise NavigationError('导航操作未确认：' + edge['label'], reason='transition_failed')

    def run(self):
        if self.deadline is None:
            self.deadline = self.clock() + self.graph['max_seconds']
        current = self.locate()
        while current not in self.graph['targets']:
            self._check()
            if self.steps >= self.graph['max_steps']:
                raise NavigationError('界面导航达到最大操作步数。', reason='budget')
            blocked = {edge['id'] for edge in self.graph['edges']
                       if self.attempts.get(edge['id'], 0) >= edge['max_attempts']}
            remaining = dict(self.graph, max_steps=self.graph['max_steps'] - self.steps)
            route = shortest_path(remaining, current, blocked)
            if not route:
                raise NavigationError('没有可执行的导航路径。', reason='unreachable')
            edge = route[0]
            self.attempts[edge['id']] = self.attempts.get(edge['id'], 0) + 1
            self.steps += 1
            try:
                self._execute(edge)
            except NavigationError as error:
                if error.reason != 'transition_failed':
                    raise
                self.log(str(error))
            self._settle()
            actual = self.locate()
            if actual != edge['target']:
                self.log('跳转后按实际界面重新规划：' + self.nodes[actual]['label'])
            current = actual
        self.log('已到达任务界面：' + self.nodes[current]['label'])
        return current
