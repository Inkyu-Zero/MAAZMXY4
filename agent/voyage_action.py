# -*- coding: utf-8 -*-
"""航海寻宝（Voyage）自动化入口。

MAA 的任务节点 `航海寻宝_入口` 是一个 `action: Custom`，name = `voyage_run`，
最终落到这个文件里的 VoyageRun。

真正干活的是 `agent/voyage/voyage.py`（照抄 zmxy4-treasure-hunt 的那套，
外加坐标对齐 / 白块识别 / 卡墙挪出等补丁）。这里只负责把它当子进程拉起来、
把它的输出转进 MAA 日志，并在用户点停止时把进程收掉、把按键松干净。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import shutil
import sys
import time
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

_HERE = Path(__file__).resolve().parent
_VOYAGE_DIR = _HERE / "voyage"

# MFA 的 agent 进程 stdout 在 Windows 上是 GBK，而子进程是按 UTF-8 读的、
# 里面还可能出现替换字符 U+FFFD（子进程自己的编码兜底产物）。直接 print 会抛
# UnicodeEncodeError → CustomAction 崩 → 任务「瞬间失败」。这里统一改成 UTF-8 +
# errors="replace"，顺带把 _log 也兜住。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


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

# 这套跑图脚本依赖 dxcam / onnxruntime / scipy，MFA 自带的 runtimes/python 里没有；
# 用它自己那个解释器会把 ImportError 变成「任务瞬间结束」，所以指定系统 Python。
def _python_candidates() -> list:
    """挑一个能跑图脚本的解释器（要 dxcam / onnxruntime / scipy）。

    **只用发布包自带的 Python**，不找系统解释器。理由：
    - `runtimes/python` 里带着 voyage-site（onnxruntime / scipy / pywin32）
    - 系统 Python 是干净的，拿它跑必然 ImportError 或 9009
    - Windows 上 `python.exe` 还常常是微软商店的占位别名，一执行就退 9009

    顺序：相对 agent/ 往上找的 `runtimes/python` → 本进程解释器。

    **发布包自带的 Python 排在最前**：用户机器上 `sys.executable` 可能
    根本不是 runtimes 里那个（21:35 那批日志里就是
    `D:\\Anaconda\\Install\\python.EXE`），拿 Anaconda 跑必然缺
    dxcam / onnxruntime / pywin32，子进程退出码 1，任务瞬间失败。
    """
    found = []
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:4]:
        exe = parent / "runtimes" / "python" / "python.exe"
        if exe.is_file():
            found.append(exe)
            break
    if sys.executable and os.path.isfile(sys.executable):
        found.append(Path(sys.executable))
    return found


def _pick_python() -> str:
    for p in _python_candidates():
        if p.exists():
            return str(p)
    return sys.executable


_LOG_FILE = None


def _log(msg: str) -> None:
    global _LOG_FILE
    _safe_print("[航海] " + msg)
    # agent 进程的 stdout 未必会被 MFA 收进 maafw.log，所以再单独落一份文件。
    try:
        if _LOG_FILE is None:
            d = _HERE.parent / "debug"
            d.mkdir(parents=True, exist_ok=True)
            # 固定写一份 debug/voyage.log（pipeline 的 focus 指向它，MFA 日志面板能直接看），
            # 同时另存带时间戳的那份，出问题可以翻历史。
            _LOG_FILE = d / ("voyage_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
        try:
            with open(_LOG_FILE.parent / "voyage.log", "a", encoding="utf-8") as fp:
                fp.write(msg + "\n")
        except Exception:
            pass
        with open(_LOG_FILE, "a", encoding="utf-8") as fp:
            fp.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _clear_keys() -> None:
    """兜底：把 WASD+J 全松一遍，避免进程被硬杀后在游戏里残留「一直按着」。"""
    try:
        u32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(256)
        targets: list[int] = []

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):
            u32.GetWindowTextW(hwnd, buf, 256)
            if "造梦" in buf.value and u32.IsWindowVisible(hwnd):
                targets.append(int(hwnd))
            return True

        u32.EnumWindows(CB(_cb), 0)
        for hwnd in targets:
            for ch in "WASDJ":
                u32.PostMessageW(hwnd, 0x101, ord(ch), 0)
    except Exception:
        pass


@AgentServer.custom_action("voyage_run")
class VoyageRun(CustomAction):
    """跑航海寻宝：交给 agent/voyage/voyage.py。"""

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
        # 回退到合并成单文件之前的那套：run_zmxy4.py + real_adapter.py +
        # zm_mapid.py + maa_white.py + zmxy4_treasure_hunt/ 包。
        # 合并版保留为 voyage.py.bak-merged，不再被调用。
        script = _VOYAGE_DIR / "run_zmxy4.py"
        if not script.exists():
            _log("缺少 %s" % script)
            return False

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        _log("启动 %s" % script)
        exe = _pick_python()
        _log("解释器 %s" % exe)
        proc = subprocess.Popen(
            [exe, "-u", "-X", "utf8", str(script)],
            cwd=str(_VOYAGE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        stopped = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    _safe_print(line)
                    try:
                        with open(_LOG_FILE, "a", encoding="utf-8") as fp:
                            fp.write(msg + "\n")
                    except Exception:
                        pass
                if context.tasker.stopping:
                    stopped = True
                    _log("收到停止请求，收尾")
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            time.sleep(0.2)
            _clear_keys()
            _log("已释放所有按键（%s）" % ("手动停止" if stopped else "进程结束"))

        # 子进程退出码是唯一的成败信息：0=挑战成功、2=挑战失败（两种都要
        # 交给 pipeline 的「收尾」节点点「返回地图」），手动停止按成功处理。
        if stopped:
            return True
        code = proc.returncode
        _log("子进程退出码 %s" % code)
        return code == 0
