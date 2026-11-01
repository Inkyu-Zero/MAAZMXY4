import os
import sys

# ⚠️ 必须在 import maa 之前设置：maa 包默认从 site-packages/maa/bin 加载原生 DLL，
# 为了不把那份 59 MB 的副本打两遍，已把它挪到 runtimes/win-x64/native。
# 目录不存在时（比如源码直跑）保持默认行为，不动环境变量。
_HERE = os.path.dirname(os.path.abspath(__file__))
_NATIVE = os.path.join(os.path.dirname(_HERE), "runtimes", "win-x64", "native")
if os.path.isdir(_NATIVE):
    os.environ.setdefault("MAAFW_BINARY_PATH", _NATIVE)

from maa.agent.agent_server import AgentServer
from maa.toolkit import Toolkit

import adventure
import battle_player
import battle_ui
import game_management_login
import loop_counter
import script_task

AgentServer.custom_recognition('battle_no_revive')(battle_ui.NoRevivePrompt)

# 可选任务扩展：模块存在就注册，缺失则静默跳过，不影响其它任务。
try:
    import adventure_drops_action  # noqa: F401
except Exception:  # noqa: BLE001
    pass

try:
    import jzld_action  # noqa: F401
except Exception:  # noqa: BLE001
    pass

try:
    import cursor_action  # noqa: F401
except Exception:  # noqa: BLE001
    pass


def main():
    Toolkit.init_option("./")

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by AgentIdentifier.")
        sys.exit(1)
        
    socket_id = sys.argv[-1]

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
