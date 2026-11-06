"""Saved-recording validation and timeline shared by Agent and input-only tasks.

No Maa imports or registration: the studio uses native controllers while the
Agent uses reverse IPC. Importing the engine must not select either runtime.
"""
import ctypes
from contextlib import nullcontext
import hashlib
import json
import os
import re
import time
from typing import Any, Dict
from script_studio.model import MAX_FILE_BYTES, RecordingDocument

def read_recording(path, *, expected_sha256=None, strict=False, expected_base=None,
                   expected_short_side=None, warning=None):
    if not os.path.exists(path):
        raise FileNotFoundError("找不到录制文件：%s" % path)
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ValueError("录制文件不能超过 16 MiB")
    with open(path, "rb") as f:
        raw = f.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("录制文件不能超过 16 MiB")
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ValueError("录制版本校验值无效")
        if hashlib.sha256(raw).hexdigest() != expected_sha256.lower():
            raise ValueError("录制在本轮准备后已改变，本轮未回放；请重新开始以读取保存后的版本")
    rec = json.loads(raw.decode("utf-8-sig"))
    document = RecordingDocument.from_dict(rec)
    if not rec.get("actions"):
        raise ValueError("录制文件里没有 actions：%s" % path)
    if expected_base is not None:
        if (not isinstance(expected_base, (list, tuple)) or len(expected_base) != 2
                or any(type(value) is not int or value < 1 for value in expected_base)):
            raise ValueError("任务预期录制尺寸无效")
        if list(document.base) != list(expected_base):
            raise ValueError("录制基准尺寸与任务当前截图不一致，请重新导入或录制")
    short_side = 720 if strict and expected_short_side is None else expected_short_side
    if short_side is not None:
        if type(short_side) is not int or not 1 <= short_side <= 32768:
            raise ValueError("任务预期录制短边无效")
        if min(document.base) != short_side:
            raise ValueError("任务脚本必须使用短边%d坐标，不能直接回放原尺寸录制" % short_side)
    if strict:
        document.to_dict()  # New task bindings never silently repair input pairs.
    # Legacy recorder files can end while a key/button is still held. Preserve
    # their timing and report this, while the player guarantees a final release.
    pair_warnings = []
    for diagnostic in document.diagnostics:
        if re.fullmatch(r"(?:键 \d+|鼠标 (?:left|right|middle)) (?:未释放|重复按下|释放没有对应按下)", diagnostic):
            pair_warnings.append(diagnostic)
        else:
            raise ValueError(diagnostic)
    for action in rec["actions"]:
        if action["op"] == "scroll" and ("x" in action or "y" in action):
            raise ValueError("此脚本含有带坐标的滚轮动作，当前后台输入无法可靠保留滚轮位置；整次回放未开始，请在脚本工具调整")
    if pair_warnings:
        message = "旧录制存在键鼠配对问题，结束/停止时会补释放；建议在脚本工具修复：" + "；".join(pair_warnings[:4])
        if warning is not None:
            warning(message)
    return rec


_TIMER_BOOSTED = [False]


def _boost_timer() -> None:
    """把系统计时器精度提到 1ms。

    Windows 默认的 `time.sleep` 粒度是 ~15.6ms，请求 26ms 实际可能睡 31ms，
    一次就多出一个 tick —— 这就是「回放忽快忽慢」的另一半原因。
    调一次 timeBeginPeriod(1) 就能把粒度降到 1ms（很多游戏自己也会调）。
    """
    if _TIMER_BOOSTED[0]:
        return
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        _TIMER_BOOSTED[0] = True
    except Exception:
        pass


def _sleep_until(target: float, should_stop=None, clock=None) -> bool:
    """睡到 monotonic 的 target 时刻。

    不能直接用 `time.sleep(gap)`：那样每个动作的实际间隔是
    「sleep 的 gap + 执行这个动作花的时间」，而执行耗时是变化的
    （按键 1~2ms、点击 5~15ms），误差会累积 —— 表现就是「回放忽快忽慢」。

    Windows 上 `time.sleep` 的粒度约 15.6ms，短间隔会被拉长，所以最后
    2ms 改成自旋，把误差压到 1ms 以内。
    """
    _boost_timer()
    # 先睡，最后 1.5ms 自旋。自旋窗口刻意压得很小：机器忙的时候
    # 长时间自旋反而会被调度器抢占，误差更大。
    while True:
        if should_stop is not None and should_stop():
            return False
        remain = target - (clock or time.monotonic)()
        if remain <= 0:
            return True
        if remain > 0.0015:
            time.sleep(min(0.02, remain - 0.0015))


_BUTTON_CONTACTS = {"left": 0, "right": 1, "middle": 2}


def _wait_job(job) -> None:
    result = job.wait()
    if result is False or getattr(result, "succeeded", True) is False:
        raise RuntimeError("Maa 输入作业执行失败")


def _dispatch(ctrl, act: Dict[str, Any], active_buttons=None) -> None:
    """执行单个动作。所有 job 都 wait，保证顺序。"""
    op = act["op"]
    if op == "key":
        _wait_job(ctrl.post_click_key(int(act["key"])))
    elif op == "keydown":
        _wait_job(ctrl.post_key_down(int(act["key"])))
    elif op == "keyup":
        _wait_job(ctrl.post_key_up(int(act["key"])))
    elif op == "click":
        _wait_job(ctrl.post_click(int(act["x"]), int(act["y"])))
    elif op == "down":
        contact = _BUTTON_CONTACTS[act.get("button", "left")]
        _wait_job(ctrl.post_touch_down(int(act["x"]), int(act["y"]), contact=contact))
    elif op == "move":
        # Moves belong to every held mouse button; legacy standalone moves keep
        # their original left-contact behavior.
        for contact in sorted(active_buttons or {0}):
            _wait_job(ctrl.post_touch_move(int(act["x"]), int(act["y"]), contact=contact))
    elif op == "up":
        contact = _BUTTON_CONTACTS[act.get("button", "left")]
        if "x" in act and "y" in act:
            # New capture/import files retain the final drag endpoint even when
            # the preceding move was rate-limited. Old coordinate-free up stays unchanged.
            _wait_job(ctrl.post_touch_move(int(act["x"]), int(act["y"]), contact=contact))
        _wait_job(ctrl.post_touch_up(contact=contact))
    elif op == "text":
        _wait_job(ctrl.post_input_text(str(act["text"])))
    elif op == "scroll":
        _wait_job(ctrl.post_scroll(int(act.get("dx", 0)), int(act.get("dy", 0))))


def play_actions(rec, ctrl, should_stop, *, speed=1.0, monitor=None, log=print,
                 ensure=None, clock=None, wait_until=None, cleanup_guard=None, after_playback=None):
    """Shared timeline and input cleanup; no capture unless an explicit monitor is supplied."""
    clock = clock or time.monotonic
    wait_until = wait_until or _sleep_until
    actions = rec["actions"]
    log(
        "[battle_play] 回放「%s」：%d 个动作，倍率 %.2f"
        % (rec.get("name") or "已绑定脚本", len(actions), speed)
    )
    # 第一个动作的 t 现在可能是非 0（录制开始后先等了一会儿），
    # 回放会照等，所以先把它报出来，免得用户以为卡住。
    _lead = float(actions[0].get("t", 0)) / 1000.0 / speed
    action_duration = sum(float(a.get("t", 0)) for a in actions)
    _total = float(rec.get("duration_ms", action_duration)) / 1000.0 / speed
    if _lead >= 0.5:
        log("[battle_play] 起手等待 %.1f 秒（按录制时的空档重现）" % _lead)
    log("[battle_play] 回放总时长 %.1f 秒" % _total)

    started = clock()
    # 绝对时刻调度：每个动作的目标时刻 = 起点 + 累计时间偏移。
    # 某个动作执行得慢，下一个动作就少睡一点，整体不会越跑越慢。
    offset, held_keys, held_buttons, loose_contacts = 0.0, set(), set(), set()
    # Outcome recognition and playback both issue reverse requests over
    # the same Agent channel.  Serialize the complete requests, not only
    # the monitor's own calls, to keep their replies matched.
    rpc_lock = monitor.rpc_lock if monitor is not None else nullcontext()
    last_geometry_probe = float('-inf')
    def stopping():
        nonlocal last_geometry_probe
        try:
            if monitor is not None and monitor.interrupted:
                return True
            with rpc_lock:
                if should_stop():
                    return True
                if ensure is not None and time.monotonic() - last_geometry_probe >= .1:
                    ensure()
                    last_geometry_probe = time.monotonic()
                return monitor is not None and monitor.interrupted
        except Exception:
            raise
    completed, dispatch_error, cleanup_errors = False, None, []
    try:
        for i, act in enumerate(actions):
            offset += float(act.get("t", 0)) / 1000.0 / speed
            if stopping() or not wait_until(started + offset, stopping, clock) or stopping():
                log('[battle_play] 结果监测要求停止后续动作' if monitor is not None and monitor.interrupted
                          else '[battle_play] 收到停止任务指令，中止回放')
                break
            # Remember a press before submission: a failing native job may
            # have pressed the device before reporting failure.
            op = act["op"]
            if monitor is not None and op in ('click', 'down', 'up'):
                # Recorded return clicks must not dismiss a defeat before
                # its result is observed and incorrectly count that round.
                monitor.probe()
                if stopping():
                    break
            transient_key = int(act["key"]) if op == "key" else None
            had_key = transient_key in held_keys
            had_left = 0 in held_buttons
            press_key = int(act["key"]) if op in ("keydown", "key") else None
            press_contact = (_BUTTON_CONTACTS[act.get("button", "left")] if op == "down" else
                             0 if op == "click" else None)
            key_was_held = press_key in held_keys
            contact_was_held = press_contact in held_buttons
            if op in ("keydown", "key"):
                held_keys.add(int(act["key"]))
            if op in ("down", "click"):
                held_buttons.add(_BUTTON_CONTACTS[act.get("button", "left")] if op == "down" else 0)
            try:
                # Check once more immediately before dispatch, after local
                # bookkeeping, so cancellation never schedules an old action.
                with rpc_lock:
                    if stopping():
                        if not key_was_held:
                            held_keys.discard(press_key)
                        if not contact_was_held:
                            held_buttons.discard(press_contact)
                        break
                    if op == "move" and not held_buttons:
                        # The legacy Win32 touch-move API carries a left-button
                        # flag even for recorded hover movement. Clear it on exit.
                        loose_contacts.add(0)
                    if ensure is not None:
                        ensure()  # Unthrottled immediately before each input.
                    _dispatch(ctrl, act, held_buttons)
            except Exception as exc:
                log("[battle_play] 第 %d 个动作（%s）执行失败：%s" % (i, op, exc))
                dispatch_error = exc
                break
            if op == "keyup":
                held_keys.discard(int(act["key"]))
            elif op == "key" and not had_key:
                held_keys.discard(transient_key)
            elif op == "up":
                held_buttons.discard(_BUTTON_CONTACTS[act.get("button", "left")])
                loose_contacts.discard(_BUTTON_CONTACTS[act.get("button", "left")])
            elif op == "click" and not had_left:
                held_buttons.discard(0)
        else:
            # The editor preserves the empty interval after the final event.
            completed = wait_until(started + _total, stopping, clock) and not stopping()
    finally:
        if monitor is not None:
            monitor.request_stop()
        # Cleanup does not consult stopping: cancellation must never prevent
        # releases, and one failed release must not skip the remaining ones.
        for key in sorted(held_keys):
            try:
                with rpc_lock:
                    if cleanup_guard is not None:
                        cleanup_guard()
                    _wait_job(ctrl.post_key_up(key))
            except Exception as exc:
                cleanup_errors.append("释放键 %d：%s" % (key, exc))
        for contact in sorted(held_buttons | loose_contacts):
            try:
                with rpc_lock:
                    if cleanup_guard is not None:
                        cleanup_guard()
                    _wait_job(ctrl.post_touch_up(contact=contact))
            except Exception as exc:
                cleanup_errors.append("释放鼠标 %d：%s" % (contact, exc))
        if monitor is not None:
            try:
                monitor.close()
            except Exception as error:
                cleanup_errors.append(str(error))
        # A recording owns the entire gesture/timeline. Park only once, after
        # every held input and the monitor have been released, including stop.
        if after_playback is not None and not cleanup_errors:
            try:
                if cleanup_guard is not None:
                    cleanup_guard()
                after_playback()
            except Exception as error:
                cleanup_errors.append('回放结束后移开悬停：' + str(error))
    if cleanup_errors:
        log("[battle_play] 输入清理未完成：%s" % "；".join(cleanup_errors))
        raise RuntimeError('输入清理未完成：' + '；'.join(cleanup_errors))
    if dispatch_error is not None:
        raise RuntimeError('脚本输入失败：' + str(dispatch_error)) from dispatch_error

    return completed


