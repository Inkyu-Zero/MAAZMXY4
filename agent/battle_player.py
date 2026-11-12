"""局内战斗录制回放 —— 回放部分。

设计约定
--------
录制文件放在项目根目录的 ``recordings/`` 下，JSON 格式：

    {
      "version": 1,
      "name": "万世冥阁-剑阵流",
      "base": [1147, 720],          // 录制时的截图尺寸，仅作参考
      "actions": [
        {"t": 0,    "op": "key",   "key": 72},
        {"t": 800,  "op": "down",  "x": 640, "y": 400},
        {"t": 60,   "op": "move",  "x": 700, "y": 450},
        {"t": 60,   "op": "up"},
        {"t": 1200, "op": "click", "x": 640, "y": 400}
      ]
    }

* ``t`` 是**相对上一个动作的毫秒数**，第一个动作相对回放开始。
* 坐标一律是 **720p 基准坐标**（MaaFramework 截图短边 720，与 pipeline ROI 同一套），
  所以换分辨率、换 DPI 都不用改录制文件 —— 框架在内部会换算回原图。
* ``op`` 支持：``key`` / ``keydown`` / ``keyup`` / ``click`` / ``down`` / ``move`` / ``up``
  / ``text`` / ``scroll``。

回放是一次性的：把 ``actions`` 从头执行到尾就结束，不循环、不判断「打没打完」。
想打几波，就把几波的完整操作一起录下来。
"""

import ctypes
from contextlib import nullcontext
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from script_studio.model import MAX_FILE_BYTES, RecordingDocument
from script_library import resolve_recording_reference
from battle_outcomes import BattleOutcomeMonitor, UnrecognizedDialog
from window_geometry import controller_hwnd, WindowLease, matches_layout
import recording_playback as playback
from mouse_input import finish_mouse_operation
from background_mouse_guard import hold_background_mouse, MouseGuardError

try:
    from toast import toast as _toast
except Exception:  # 通知不可用时不能让回放挂掉

    def _toast(*_a, **_k):
        pass

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDING_DIR = os.path.join(_PROJECT_ROOT, "recordings")
# interface.json 的「回放文件」下拉里，这一项代表「别用列表里的，弹框让我自己挑」
# （必须和 tools/gen_recording_options.py 里的 MANUAL 保持一致）
MANUAL_CASE = "（手动选择文件…）"
# 「最新录制」伪 case：下拉里固定存在，运行时才去磁盘上找最新那条。
# 有了它，录完新战斗不用重启 MFA（interface.json 的 cases 是静态的，
# 重启前不会变），直接跑就能用上刚录的那份。
LATEST_CASE = "（最新录制）"
LATEST_TOKEN = "@latest"
SELECTED_CASE = "（脚本工具所选）"
SELECTED_TOKEN = "@selected"


def _ensure_tcl_env() -> None:
    """内嵌运行时的 tcl 不在默认搜索路径，tkinter 要靠这两个变量才找得到 init.tcl。"""
    if os.environ.get("TCL_LIBRARY"):
        return
    prefix = os.path.dirname(os.path.abspath(sys.executable))
    for name, sub in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        path = os.path.join(prefix, "tcl", sub)
        if os.path.isdir(path):
            os.environ[name] = path


LAST_FILE = os.path.join(RECORDING_DIR, ".last.json")


def _last_pick() -> str:
    try:
        with open(LAST_FILE, encoding="utf-8") as f:
            return str(json.load(f).get("file") or "")
    except Exception:
        return ""


def _remember_pick(path: str) -> None:
    try:
        os.makedirs(RECORDING_DIR, exist_ok=True)
        with open(LAST_FILE, "w", encoding="utf-8") as f:
            json.dump({"file": path}, f, ensure_ascii=False)
    except Exception:
        pass


def ask_recording_file() -> Optional[str]:
    """弹系统文件选择框挑一个录制 json，返回绝对路径；取消返回 None。

    和 `_ask_save_name` 同样的顾虑：Tk 必须在创建它的线程里用，而 MAA 的
    回调不在主线程，所以单开线程建窗口、结果用 Event 回传。
    """
    result: Dict[str, Optional[str]] = {"path": None}
    done = threading.Event()

    def worker() -> None:
        try:
            _ensure_tcl_env()
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            os.makedirs(RECORDING_DIR, exist_ok=True)
            path = filedialog.askopenfilename(
                title="选择要回放的录制文件",
                initialdir=RECORDING_DIR,
                initialfile=_last_pick() or "",
                filetypes=[("录制文件", "*.json"), ("所有文件", "*.*")],
                parent=root,
            )
            root.destroy()
            result["path"] = path or None
        except Exception as exc:  # noqa: BLE001 - 弹框失败要让回放给出明确原因
            print("[battle_play] 文件选择框失败：%s" % exc, flush=True)
            result["path"] = None
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    done.wait(timeout=300)
    return result["path"]


def newest_recording() -> Optional[str]:
    """recordings/ 里最近修改的那条录制（不含点开头的辅助文件）。"""
    try:
        items = []
        for fn in os.listdir(RECORDING_DIR):
            if fn.startswith(".") or not fn.lower().endswith(".json"):
                continue
            fp = os.path.join(RECORDING_DIR, fn)
            if not os.path.isfile(fp):
                continue
            items.append((os.path.getmtime(fp), fn[:-5]))
        if not items:
            return None
        items.sort(reverse=True)
        return items[0][1]
    except Exception:
        return None


def selected_recording() -> Optional[str]:
    """Resolve the live studio selection; a stale selection must never run another file."""
    selection = os.path.join(RECORDING_DIR, ".selected.json")
    if not os.path.exists(selection):
        return None
    try:
        with open(selection, encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except Exception as exc:
        raise ValueError("脚本工具选择记录损坏，请重新选择脚本") from exc
    if not isinstance(data, dict):
        raise ValueError("脚本工具选择记录无效，请重新选择脚本")
    name = data.get("file")
    if name is None or name == "":
        return None
    if (not isinstance(name, str) or name.startswith(".") or not name.lower().endswith(".json")
            or os.path.basename(name) != name or any(char in name for char in '<>:"/\\|?*')):
        raise ValueError("脚本工具选择文件无效，请在脚本库重新选择")
    path = os.path.join(RECORDING_DIR, name)
    if not os.path.isfile(path):
        raise FileNotFoundError("脚本工具所选文件已不存在：%s；请重新选择，未自动改用其他脚本" % name)
    return name


def recording_path(name: str) -> str:
    """名字 → 录制文件路径。

    支持三种写法：
    * ``示例-最小``      → ``recordings/示例-最小.json``
    * ``示例-最小.json`` → ``recordings/示例-最小.json``
    * 绝对路径           → 原样使用（文件选择框选外部文件时走这条）
    """
    if os.path.isabs(name):
        return name
    if name.lower().endswith(".json"):
        return os.path.join(RECORDING_DIR, name)
    return os.path.join(RECORDING_DIR, name + ".json")


def load_recording(name: str, *, expected_sha256=None, strict=False,
                   expected_base=None, expected_short_side=None, directory=None) -> Dict[str, Any]:
    path = str(resolve_recording_reference(directory or RECORDING_DIR, name)) if strict else recording_path(name)
    def warning(message):
        print("[battle_play] 警告：" + message, flush=True)
        _toast("录制兼容提示", message)
    return playback.read_recording(path, expected_sha256=expected_sha256, strict=strict,
        expected_base=expected_base, expected_short_side=expected_short_side, warning=warning)


# Retain the private timing aliases used by the native playback regression tests.
_sleep_until = playback._sleep_until
_wait_job = playback._wait_job


@AgentServer.custom_action("battle_play")
class BattlePlay(CustomAction):
    """把一条录制序列原样回放一遍。

    ``custom_action_param`` 支持：
        {"file": "万世冥阁-剑阵流", "speed": 1.0}
    """

    def run(self, context, argv) -> bool:  # noqa: ANN001 - MAA 的接口签名
        # Maa's ctypes callback requires a concrete bool; exceptions crossing
        # that boundary do not reliably become a failed native action.
        try:
            return self.run_managed(context, argv)
        except MouseGuardError:
            return False

    def run_managed(self, context, argv) -> bool:
        # The compact round runs in this Agent process and needs the typed
        # cleanup error in order to forbid an automatic recovery restart.
        raw = getattr(argv, "custom_action_param", None)
        # MAA 传过来的形态有三种：
        #   1) dict        —— pipeline 里直接写对象
        #   2) JSON 字符串  —— pipeline 里写 "{\"file\": \"xxx\", \"speed\": 1.0}"
        #   3) 纯文件名     —— pipeline 里写 "{回放文件}"，由 MFA 的 option 替换填充
        #
        # 注意（踩过的坑）：MFA 的占位符替换**只作用于字符串标量**，嵌套对象内部的
        # "{回放文件}" 会原样保留，于是读到的是 recordings\{回放文件}.json 然后报
        # 「找不到录制文件」。所以 interface.json 里 option 的 custom_action_param
        # 必须写成字符串，不能写成 {"file": "{回放文件}"}。
        param = {}
        if isinstance(raw, dict):
            param = raw
        elif isinstance(raw, str) and raw.strip():
            s = raw.strip()
            try:
                obj = json.loads(s)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                param = obj
            elif obj is not None:
                param = {"file": str(obj)}
            else:
                param = {"file": s}
        else:
            param = {}

        name = str(param.get("file") or param.get("name") or "").strip()
        strict_task = param.get("strict_task", False)
        if type(strict_task) is not bool:
            self._log("[battle_play] strict_task 必须为布尔值")
            return False
        if strict_task or name.startswith("@task:"):
            strict_task = True
            try:
                name = str(resolve_recording_reference(RECORDING_DIR, name))
            except (OSError, ValueError) as exc:
                self._log("[battle_play] %s" % exc)
                _toast("任务脚本无法回放", str(exc))
                return False
        if name in (SELECTED_TOKEN, SELECTED_CASE):
            try:
                selected = selected_recording()
            except (ValueError, FileNotFoundError) as exc:
                self._log("[battle_play] %s" % exc)
                _toast("无法回放所选脚本", str(exc))
                return False
            if selected is not None:
                name = selected
                self._log("[battle_play] 使用脚本工具所选：%s" % selected)
            else:
                name = LATEST_TOKEN
                self._log("[battle_play] 脚本工具尚未选择文件，使用最新录制")
        if name == LATEST_TOKEN:
            latest = newest_recording()
            if latest:
                name = latest
                self._log("[battle_play] 使用最新录制：%s" % latest)
            else:
                # 一个录制都还没有：退回文件选择框，别直接失败
                self._log("[battle_play] 还没有任何录制，改为手动选择文件")
                name = ""
        if not name or name in ("?", "？", "select", "选择", "选择文件", MANUAL_CASE):
            # 没写文件名（或写了 ?）就弹系统文件选择框，省得手打
            picked = ask_recording_file()
            if not picked:
                self._log("[battle_play] 没有选文件，取消回放")
                return False
            _remember_pick(picked)
            name = picked
            self._log("[battle_play] 已选择：%s" % picked)

        try:
            speed_value = param.get("speed", 1.0)
            if isinstance(speed_value, bool):
                raise ValueError
            speed = float(speed_value)
            if not math.isfinite(speed) or not 0.01 <= speed <= 100:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            self._log("[battle_play] 回放速度必须是 0.01～100 的有限数值")
            return False

        try:
            rec = load_recording(name, expected_sha256=param.get("expected_sha256"), strict=strict_task,
                                 expected_base=param.get("expected_base"),
                                 expected_short_side=param.get("expected_short_side"))
        except Exception as exc:  # 文件缺失 / 格式错误都在这里
            self._log("[battle_play] 录制文件有问题：%s" % exc)
            _toast("录制文件无法回放", str(exc))
            return False

        actions: List[Dict[str, Any]] = rec["actions"]
        ctrl = context.tasker.controller
        monitor = None
        hwnd = controller_hwnd(ctrl)
        try:
            if param.get('box_speed_proof') is not None:
                from box_speed import validate_recording
                from box_speed_native import verify_playback_proof
                validate_recording(rec,2)
                speed=verify_playback_proof(param['box_speed_proof'],hwnd,param.get('expected_sha256'))
            with WindowLease(hwnd, rec['base']) if hwnd else nullcontext() as lease:
                if lease:
                    # Normal tasks use native scaling and may inspect the game.
                    # Refresh after changing borders/size; input-only jobs bypass
                    # this native action and call the shared timeline directly.
                    if lease.changed:
                        _wait_job(ctrl.post_screencap())
                    frame = ctrl.cached_image
                    if frame is None or min(frame.shape[:2]) != 720 or not matches_layout(
                            (frame.shape[1],frame.shape[0]),rec['base']):
                        raise ValueError('播放前的窗口截图尚未匹配脚本比例，已停止输入。')
                if param.get('result_guard') is not None:
                    try:
                        monitor = BattleOutcomeMonitor(context, param['result_guard'])
                        if not context.override_next(argv.node_name, param['result_guard']['default_next']):
                            raise ValueError('无法恢复本轮的默认回放完成路线。')
                        monitor.start()
                    except Exception as error:
                        if monitor is not None:
                            try:
                                monitor.close()
                            except Exception as cleanup_error:
                                self._log('[battle_play] 监测清理失败：' + str(cleanup_error))
                        self._log('[battle_play] 战斗结果监测未启动：' + str(error))
                        return False
                with hold_background_mouse(ctrl):
                    completed = playback.play_actions(rec, ctrl, lambda: context.tasker.stopping,
                        speed=speed, monitor=monitor, log=self._log,
                        ensure=lease.ensure if lease else None,
                        clock=lease.clock if lease else time.monotonic, wait_until=_sleep_until,
                        cleanup_guard=lease.require_identity if lease else None,
                        after_playback=(lambda: finish_mouse_operation(context.tasker.controller)) if lease else None)
        except Exception as error:
            if monitor is not None:
                monitor.close()
            self._log('[battle_play] 回放已停止：' + str(error))
            if isinstance(error, MouseGuardError):
                raise
            return False
        if monitor is not None:
            if isinstance(monitor.error, UnrecognizedDialog):
                # play_actions has released every held input and joined the
                # monitor. Retry only this exceptional recognition state.
                try:
                    monitor.recheck_unknown_dialog()
                except Exception as error:
                    monitor.error = error
            if monitor.error is not None:
                self._log('[battle_play] 结果监测停止：' + str(monitor.error))
                # Preserve the exact failure even if releasing input dismisses
                # the dialog. Only an unrecognized modal gets its own handoff;
                # known blockers, ambiguous results and missing assets stay fatal.
                targets = param['result_guard'].get('error_next')
                # Transport/capture failures still use the normal failure
                # handler so the host can verify an actually lost window.
                if isinstance(monitor.error, ValueError) and targets and not context.tasker.stopping:
                    if isinstance(monitor.error, UnrecognizedDialog):
                        targets = ['Result_MonitorUnrecognizedDialog']
                    return bool(context.override_next(argv.node_name, targets))
                return False
            if monitor.branch is not None and not context.tasker.stopping:
                self._log('[battle_play] 已识别战斗结果或复活提示，释放输入后进入对应处理。')
                return bool(context.override_next(argv.node_name, monitor.branch))
        if not completed:
            return False

        self._log("[battle_play] 回放结束")
        return True

    @staticmethod
    def _log(msg: str) -> None:
        # agent 的 stdout 不会被 MAA 的 GUI 日志面板转发，
        # 但会进 debug/maafw.log 的 agent 输出段，排查够用。
        print(msg, flush=True)


def list_recordings() -> List[str]:
    """列出 recordings/ 下所有录制名（不含 .json），给录制工具和排查用。"""
    if not os.path.isdir(RECORDING_DIR):
        return []
    names = []
    for fn in sorted(os.listdir(RECORDING_DIR)):
        if (not fn.startswith(".") and fn.lower().endswith(".json")
                and os.path.isfile(os.path.join(RECORDING_DIR, fn))):
            names.append(fn[:-5])
    return names
