"""Observe result screens on the existing Maa controller during a recording.

The worker performs recognition only. The playback thread alone redirects the
Pipeline, after releasing every held key/button. No second input owner is made.
"""
import threading
import time
from navigation.recognition import modal_present


class UnrecognizedDialog(ValueError):
    """An unknown modal, after checking the same frame for known blockers."""
    reason = 'unrecognized_dialog'

    def __init__(self, message, frame=None):
        super().__init__(message)
        self.frame = frame


def save_unknown_frame(frame):
    """Save the actual decision frame, never request a diagnostic screenshot."""
    if frame is None or not hasattr(frame, 'dtype'):
        return
    try:
        from pathlib import Path
        from datetime import datetime
        from uuid import uuid4
        import os
        from PIL import Image
        directory = Path(os.environ.get('MAAZMXY4_TEST_LOG_DIR',
            Path(__file__).resolve().parents[1] / 'debug')) / 'battle-outcomes'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8] + '-unknown.png')
        Image.fromarray(frame[:, :, :3][:, :, ::-1]).save(path)
        print('[battle_outcomes] 未识别弹窗判定帧：' + str(path), flush=True)
    except Exception as error:
        print('[battle_outcomes] 判定帧保存失败：' + str(error), flush=True)


class BattleOutcomeMonitor:
    def __init__(self, context, policy):
        if not isinstance(policy, dict) or set(policy) - {'failed_next', 'succeeded_next', 'default_next', 'interval_ms', 'revive_next', 'error_next'}:
            raise ValueError('战斗结果监测配置无效。')
        for field in ('failed_next', 'succeeded_next', 'default_next', *(n for n in ('revive_next','error_next') if n in policy)):
            value = policy.get(field)
            if not isinstance(value, list) or not value or len(value) > 20 or any(not isinstance(n, str) or not n for n in value):
                raise ValueError('战斗结果监测缺少有效的后续节点。')
        interval = policy.get('interval_ms', 2000)
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
                failure = None
                for attempt in range(3):
                    if self._stop.is_set() or self.context.tasker.stopping:
                        return
                    try:
                        controller = self.context.tasker.controller
                        job = controller.post_screencap().wait()
                        if not getattr(job, 'succeeded', False):
                            raise RuntimeError('战斗结果截图失败。')
                        frame = controller.cached_image
                        if frame is None or getattr(frame, 'ndim', 0) != 3 or frame.shape[2] < 3:
                            raise RuntimeError('战斗结果截图无效。')
                        frame = frame.copy()
                        break
                    except Exception as error:
                        failure = error
                        if attempt < 2 and self._stop.wait(.2):
                            return
                else:
                    raise RuntimeError('战斗结果连续三次截图失败，已停止回放。') from failure
                # One shared modal check on ordinary frames. Only an actual
                # modal needs semantic revival recognition and a decline route.
                if modal_present(self.context, frame):
                    if 'revive_next' in self.policy:
                        revive = self.context.run_recognition('Result_RevivePrompt', frame)
                        if revive is None:
                            raise ValueError('缺少复活弹窗识别资源。')
                        if revive.hit:
                            self.branch = list(self.policy['revive_next'])
                            return
                    if 'revive_next' not in self.policy:
                        raise ValueError('检测到确认浮窗，已停止回放；请处理浮窗后再继续。')
                    blocked = self.context.run_recognition('Nav_BlockedDialog', frame)
                    if blocked is None:
                        raise ValueError('缺少阻断提示识别资源，已停止回放。')
                    if blocked.hit:
                        raise ValueError('检测到验证码、登录或阻断提示，已停止回放。')
                    sell = self.context.run_recognition('Nav_See_profile_sell', frame)
                    if sell is None:
                        raise ValueError('缺少已知确认浮窗识别资源，已停止回放。')
                    if sell.hit:
                        raise ValueError('检测到出售确认浮窗，已停止回放。')
                    raise UnrecognizedDialog('确认浮窗暂未识别，已暂停回放。', frame)
                failed = self.context.run_recognition('Result_Failed', frame)
                succeeded = self.context.run_recognition('Result_Succeeded', frame)
                if failed is None or succeeded is None:
                    raise ValueError('缺少战斗结果识别资源，请更新完整客户端。')
                if failed.hit and succeeded.hit:
                    raise ValueError('战斗结果识别存在歧义，已停止回放。')
                if failed.hit:
                    self.branch = list(self.policy['failed_next'])
                elif succeeded.hit:
                    self.branch = list(self.policy['succeeded_next'])
            except Exception as error:
                self.error = error

    def recheck_unknown_dialog(self, wait=time.sleep):
        """Playback owner calls this only after keys/buttons and worker cleanup.

        At most two fresh frames; a vanished dialog cannot resume a partially
        played recording or silently count the round as successful.
        """
        if not isinstance(self.error, UnrecognizedDialog):
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('结果监测尚未退出，不能重判弹窗。')
        save_unknown_frame(self.error.frame)
        self.error.frame = None
        for _ in range(2):
            if self.context.tasker.stopping:
                return
            wait(.2)
            if self.context.tasker.stopping:
                return
            sample = BattleOutcomeMonitor(self.context, self.policy)
            sample.probe()
            sample.close()
            if sample.branch is not None:
                self.branch, self.error = sample.branch, None
                return
            if sample.error is not None and not isinstance(sample.error, UnrecognizedDialog):
                self.error = sample.error
                return
        self.error = UnrecognizedDialog('确认浮窗连续重判后仍无法识别，已停止回放。')

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
