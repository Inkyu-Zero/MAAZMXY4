# -*- coding: utf-8 -*-
"""航海寻宝（Voyage）自动化入口。

MAA 的任务节点 `航海寻宝_入口` 是一个 `action: Custom`，name = `voyage_run`，
最终落到这个文件里的 VoyageRun。

真正干活的是 `agent/voyage/run_zmxy4.py`（照抄 zmxy4-treasure-hunt 的那套，
外加坐标对齐 / 白块识别 / 卡墙挪出等补丁）。这里只负责把它当子进程拉起来、
把它的输出转进 MAA 日志，并在用户点停止时把进程收掉、把按键松干净。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from threading import Thread
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from window_geometry import controller_hwnd

_HERE = Path(__file__).resolve().parent
_VOYAGE_DIR = _HERE / "voyage"


def _safe_print(text: str) -> None:
    """把子进程输出转进 MFA 日志；编码不允许时退化成可编码的形式，绝不抛异常。"""
    try:
        print(text, flush=True)
        return
    except UnicodeEncodeError:
        pass
    try:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)
    except Exception:  # noqa: BLE001
        pass

_LOG_FILE = _HERE.parent / "debug" / "voyage.log"


def _log(msg: str) -> None:
    _safe_print("[航海] " + msg)
    try:
        _LOG_FILE.parent.mkdir(exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(msg + "\n")
    except OSError:
        pass


def _window_pid(hwnd):
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value


def _clear_keys(hwnd, pid):
    if _window_pid(hwnd) == pid:
        for key in "WASDJ":
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x101, ord(key), 0)


@AgentServer.custom_action("voyage_run")
class VoyageRun(CustomAction):
    """跑航海寻宝：交给 agent/voyage/run_zmxy4.py。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # 包一层 try：回调里抛异常会让 MFA 直接把这个任务判结束，
        # 日志上看起来就是「任务瞬间完成 / Action is null」，极难排查。
        try:
            return self._run_impl(context, argv)
        except Exception as e:  # noqa: BLE001
            import traceback
            _log("异常中断：%s" % e)
            _log(traceback.format_exc()[:3000])
            return False

    def _run_impl(self, context: Context, argv: CustomAction.RunArg) -> bool:
        script = _VOYAGE_DIR / "run_zmxy4.py"
        if not script.exists():
            _log("缺少 %s" % script)
            return False

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        _log("启动 %s" % script)
        hwnd = controller_hwnd(context.tasker.controller)
        pid = _window_pid(hwnd) if hwnd else 0
        if not pid or context.tasker.stopping:
            _log("无法启动航海，请确认小助手已绑定当前造梦盒子窗口。")
            return False
        exe = sys.executable
        _log("解释器 %s" % exe)
        proc = subprocess.Popen(
            [exe, "-s", "-u", "-X", "utf8", str(script), str(hwnd), str(pid)],
            cwd=str(_VOYAGE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        def read_output():
            with proc.stdout as stream:
                for line in stream:
                    _log(line.rstrip())

        reader = Thread(target=read_output, daemon=True)
        reader.start()
        try:
            while proc.poll() is None:
                if context.tasker.stopping:
                    _log("收到停止请求，结束航海")
                    return False
                try:
                    proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
            if proc.returncode:
                _log(f"航海子进程失败，退出码 {proc.returncode}。请查看上方错误及航海日志。")
            return proc.returncode == 0 and not context.tasker.stopping
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.join()
            _clear_keys(hwnd, pid)
