"""Observe result screens on the existing Maa controller during a recording.

The worker performs recognition only. The playback thread alone redirects the
Pipeline, after releasing every held key/button. No second input owner is made.
"""
import threading
import sys
import time


class BattleOutcomeMonitor:
    def __init__(self, context, policy):
        if not isinstance(policy, dict) or set(policy) - {'failed_next', 'succeeded_next', 'default_next', 'interval_ms', 'revive_next'}:
            raise ValueError('战斗结果监测配置无效。')
        for field in ('failed_next', 'succeeded_next', 'default_next', *(['revive_next'] if 'revive_next' in policy else [])):
            value = policy.get(field)
            if not isinstance(value, list) or not value or len(value) > 20 or any(not isinstance(n, str) or not n for n in value):
                raise ValueError('战斗结果监测缺少有效的后续节点。')
        interval = policy.get('interval_ms', 400)
        if type(interval) is not int or not 200 <= interval <= 2000:
            raise ValueError('战斗结果采样间隔必须为200～2000毫秒。')
        self.context, self.policy = context.clone(), policy
        self.interval = interval / 1000
        self.branch = None
        self.error = None
        self._stop = threading.Event()
        # Agent reverse requests share a single transport.  The recorder and
        # monitor must hold this same reentrant lock through each complete RPC
        # (including job.wait/status), or replies can be consumed by the wrong
        # thread.  It is reentrant so a guarded dispatch can recheck stopping.
        self.rpc_lock = threading.RLock()
        self._thread = None

    @property
    def interrupted(self):
        return self.branch is not None or self.error is not None

    def probe(self):
        with self.rpc_lock:
            if self._stop.is_set() or self.interrupted:
                return
            try:
                if self.context.tasker.stopping:
                    return
                controller = self.context.tasker.controller
                job = controller.post_screencap().wait()
                if not getattr(job, 'succeeded', False):
                    raise RuntimeError('战斗结果截图失败，已停止回放。')
                frame = controller.cached_image
                if frame is None or getattr(frame, 'ndim', 0) != 3 or frame.shape[2] < 3:
                    raise RuntimeError('战斗结果截图无效，已停止回放。')
                frame = frame.copy()
                if 'revive_next' in self.policy:
                    revive = self.context.run_recognition('Result_RevivePrompt', frame)
                    if revive is None:
                        print('[battle_outcomes] 缺少复活弹窗识别资源，跳过该检查', file=sys.stderr, flush=True)
                    elif revive.hit:
                        # This is an interruption, not yet a confirmed defeat.
                        # Playback owns key release and the native guarded click.
                        self.branch = list(self.policy['revive_next'])
                        return
                confirmation = self.context.run_recognition('Nav_CommonConfirm', frame)
                if confirmation is None:
                    print('[battle_outcomes] 缺少确认浮窗识别资源，跳过该检查', file=sys.stderr, flush=True)
                elif confirmation.hit:
                    raise ValueError('检测到确认浮窗，已停止回放；请处理浮窗后再继续。')
                failed = self.context.run_recognition('Result_Failed', frame)
                succeeded = self.context.run_recognition('Result_Succeeded', frame)
                if failed is None or succeeded is None:
                    print('[battle_outcomes] 缺少战斗结果识别资源，跳过结果判定', file=sys.stderr, flush=True)
                    return
                if failed.hit and succeeded.hit:
                    raise ValueError('战斗结果识别存在歧义，已停止回放。')
                if failed.hit:
                    self.branch = list(self.policy['failed_next'])
                elif succeeded.hit:
                    self.branch = list(self.policy['succeeded_next'])
            except Exception as error:
                self.error = error

    def start(self):
        # Check before the first recorded action, including an already visible
        # defeat screen. Later samples do not block the keyboard event timeline.
        self.probe()
        if self.interrupted or self.context.tasker.stopping:
            return
        def work():
            while not self._stop.wait(self.interval):
                self.probe()
                if self.interrupted:
                    return
        self._thread = threading.Thread(target=work, name='battle-outcome-recognition', daemon=True)
        self._thread.start()

    def request_stop(self):
        self._stop.set()

    def close(self):
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError('战斗结果识别尚未退出，任务已停止，禁止开始下一轮。')
