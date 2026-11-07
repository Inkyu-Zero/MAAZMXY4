"""Policy for the box's native 1x/2x commands, separate from navigation."""

SPEED_KEYS = {1: 0x31, 2: 0x32}  # Number row; numpad keys are different commands.


class SpeedRejected(RuntimeError):
    """The box explicitly rejected the command without changing its speed."""


def validate_speed(value=1):
    if type(value) is not int or value not in SPEED_KEYS:
        raise ValueError('盒子倍速只支持 1 倍或 2 倍。')
    return value


def validate_recording(recording, speed):
    validate_speed(speed)
    if speed == 1:
        return
    for action in recording['actions']:
        if action.get('op') in ('key', 'keydown') and action.get('key') in SPEED_KEYS.values():
            raise ValueError('该脚本包含数字行 1/2，会改变盒子原生倍速；请移除这些操作后再启用 2 倍速。')


def verify_proof(proof, hwnd, sha256, identity, exe_path):
    import re
    if (not isinstance(proof,dict) or proof.get('state')!='ready' or type(proof.get('speed')) is not int
            or proof['speed']!=2 or any(type(proof.get(key)) is not int or proof[key]<=0
                for key in ('hwnd','pid','started_utc_ticks'))
            or not isinstance(sha256,str) or not re.fullmatch('[0-9a-f]{64}',sha256)):
        raise ValueError('盒子 2 倍速尚未确认，未加速脚本。')
    if (proof.get('hwnd')!=hwnd or proof.get('sha256')!=sha256
            or any(proof.get(key)!=identity.get(key) for key in ('pid','started_utc_ticks'))):
        raise ValueError('倍速确认与当前窗口或脚本不一致。')
    from pathlib import Path
    if str(Path(proof.get('exe_path','')).resolve()).casefold()!=str(Path(exe_path).resolve()).casefold():
        raise ValueError('倍速确认对应的盒子程序已变化。')
    return 2


class SpeedLease:
    """One task owns speed until completion, cancellation, or failed setup.

    The adapter validates the same process/window on every command and returns
    only after confirmation. No command or verification is used at default 1x.
    A failed reset retains ownership so callers cannot silently continue a queue.
    """
    def __init__(self, speed, apply):
        self.speed = validate_speed(speed)
        self.apply = apply
        self.may_be_enabled = False
        self.ready = speed == 1

    def start(self):
        if self.ready:
            return self.speed
        # Even a missing acknowledgement can mean the key was accepted.
        self.may_be_enabled = True
        try:self.apply(self.speed)
        except SpeedRejected:
            self.may_be_enabled=False
            raise
        self.ready = True
        return self.speed

    def close(self):
        if not self.may_be_enabled:
            return
        self.ready = False
        self.apply(1)
        self.may_be_enabled = False

    def __enter__(self):
        try:
            self.start()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()
