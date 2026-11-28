"""开发期热重载：改 agent/*.py 后自动重连，不用重启 MFA。

用法：在 main.py 里靠环境变量开启，避免影响正常使用。

    set MAA_DEV_RELOAD=1
    MAA造梦西游4.exe

原理：agent 是 MFA 拉起的独立进程，socket_id 由 MFA 分配并作为最后一个参数
传进来。这里起一个后台线程盯着 agent/*.py 的 mtime，一变就：

    AgentServer.shut_down()  ->  importlib.reload(所有自定义模块)  ->  start_up(同一个 socket_id)

MAA 侧看到的是「Agent 进程断开又连上」，不需要重开 MFA，也不用等它重新
加载 resource（`resource/` 只在 MFA 启动时读一次，改 pipeline 才需要重开）。
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from pathlib import Path

# 这些模块里有 @AgentServer.custom_action / custom_recognition，reload 之后
# 装饰器会重新执行一遍并把新实例写进 holder。
RELOADABLE = (
    "adventure",
    "adventure_logic",
    "battle_player",
    "battle_ui",
    "cursor_action",
    "fuguo_action",
    "game_management_login",
    "jzld_action",
    "loop_counter",
    "script_task",
    "view_scale",
    "voyage_action",
    "window_geometry",
)

_AGENT_DIR = Path(__file__).resolve().parent
_POLL_SECONDS = 1.0


def _snapshot() -> dict[str, float]:
    """记录 agent/ 下所有 py 文件的 mtime。"""
    result = {}
    for path in _AGENT_DIR.rglob("*.py"):
        try:
            result[str(path)] = path.stat().st_mtime
        except OSError:
            pass
    return result


def _reload_all() -> None:
    """把自定义模块重新 import 一遍。

    先清空 holder 里的旧实例，否则同名注册会堆在一起、旧实例还攥着已释放的
    controller。
    """
    from maa.agent.agent_server import AgentServer

    AgentServer._custom_action_holder.clear()       # noqa: SLF001
    AgentServer._custom_recognition_holder.clear()  # noqa: SLF001

    for name in RELOADABLE:
        module = sys.modules.get(name)
        if module is None:
            continue
        try:
            importlib.reload(module)
        except Exception as error:                  # noqa: BLE001
            print("[重载] %s 失败：%s" % (name, error), flush=True)
    print("[重载] 已重新加载 %d 个模块" % len(RELOADABLE), flush=True)


def watch(socket_id: str) -> None:
    """后台线程：文件变了就断开重连。"""
    from maa.agent.agent_server import AgentServer

    last = _snapshot()
    while True:
        time.sleep(_POLL_SECONDS)
        current = _snapshot()
        if current == last:
            continue
        changed = [
            os.path.basename(p)
            for p, m in current.items()
            if last.get(p) != m
        ]
        last = current
        print("[重载] 检测到改动：%s" % ", ".join(sorted(changed)[:5]), flush=True)

        try:
            AgentServer.shut_down()
        except Exception as error:                  # noqa: BLE001
            print("[重载] 断开失败：%s" % error, flush=True)

        _reload_all()

        try:
            AgentServer.start_up(socket_id)
            print("[重载] 已重连，可以继续跑任务", flush=True)
        except Exception as error:                  # noqa: BLE001
            print("[重载] 重连失败：%s" % error, flush=True)
            return


def start(socket_id: str) -> None:
    """MAA_DEV_RELOAD=1 时开一个后台线程。"""
    if os.environ.get("MAA_DEV_RELOAD") != "1":
        return
    thread = threading.Thread(target=watch, args=(socket_id,), daemon=True, name="dev-reload")
    thread.start()
    print("[重载] 已开启（每 %.0f 秒检查一次 agent/*.py）" % _POLL_SECONDS, flush=True)
