"""检查仓库内的 Python 运行时是否完整可用。

脚本目录要能脱离系统 Python 独立运行，所以 `interface.json` 里的
`agent.child_exec` 指向 `runtimes/python/python.exe`。那个目录是本地
整理出来的（不入库、不进 git），一旦被误删或被清理工具扫掉，表现是
「MAA 启动后 agent 连不上」，日志里没有任何 Python 报错——很难查。

所以留一个自检入口：

    python tools/runtime_check.py

任何一项失败都会明确说明缺什么、怎么补。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(ROOT, "runtimes", "python")
PY_EXE = os.path.join(PY_DIR, "python.exe")
SP = os.path.join(PY_DIR, "Lib", "site-packages")

# 内嵌 Python 必须包含的文件（原 update_engine/runtime.py，更新机制剥离后内联在这里）。
PYTHON_REQUIRED = (
    'python.exe', 'pythonw.exe', 'python3.dll', 'python312.dll', 'vcruntime140.dll', 'DLLs',
    'Lib/os.py', 'Lib/site-packages/maa/__init__.py', 'Lib/site-packages/numpy/__init__.py',
    'Lib/site-packages/strenum/__init__.py', 'Lib/site-packages/pynput/__init__.py',
    'Lib/site-packages/PIL/__init__.py', 'Lib/site-packages/PIL/_imaging.pyd',
    'Lib/site-packages/six.py', 'Lib/site-packages/MaaAgentBinary', 'Lib/tkinter/__init__.py',
    'DLLs/_tkinter.pyd', 'DLLs/tcl86t.dll', 'DLLs/tk86t.dll',
    'tcl/tcl8.6/init.tcl', 'tcl/tk8.6/tk.tcl',
)
REQUIRED = list(PYTHON_REQUIRED)

# 导入检查（在子进程里跑，避免污染本进程）
PROBE = (
    "import sys, numpy, strenum, tkinter, pynput, six\n"
    "from PIL import Image\n"
    "tkinter.Tcl()\n"
    "from maa.agent.agent_server import AgentServer\n"
    "from maa.toolkit import Toolkit\n"
    "from maa.custom_action import CustomAction\n"
    "from maa.custom_recognition import CustomRecognition\n"
    "print('OK', sys.version.split()[0], numpy.__version__)\n"
)


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for base, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return total


def _mb(n: int) -> str:
    return "%.1f MB" % (n / 1024 / 1024)


def required_path_exists(root, relative):
    """Accept both wheel naming conventions for the pinned CPython 3.12 x64 runtime."""
    path = Path(root) / relative
    candidates = [path]
    if relative == 'Lib/site-packages/PIL/_imaging.pyd':
        candidates.append(path.with_name('_imaging.cp312-win_amd64.pyd'))
    is_directory = relative in ('DLLs', 'Lib/site-packages/MaaAgentBinary')
    return any(not item.is_symlink() and (item.is_dir() if is_directory else item.is_file())
               for item in candidates)


def check_files() -> list:
    return [relative for relative in REQUIRED if not required_path_exists(PY_DIR, relative)]


def check_probe() -> tuple:
    """返回 (ok, 说明)。"""
    try:
        r = subprocess.run(
            [PY_EXE, "-E", "-s", "-c", PROBE],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    except FileNotFoundError:
        return False, "解释器不存在：%s" % PY_EXE
    except subprocess.TimeoutExpired:
        return False, "导入检查超时（>120s）"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (tail[-1] if tail else "退出码 %d" % r.returncode)
    return True, r.stdout.strip()


def check_interface() -> tuple:
    """interface.json 的 child_exec 是否指向本仓库的解释器。"""
    want = "./runtimes/python/python.exe"
    results = []
    for p in (os.path.join(ROOT, "assets", "interface.json"),
              os.path.join(ROOT, "interface.json")):
        if not os.path.exists(p):
            continue
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as exc:  # noqa: BLE001
            results.append((os.path.basename(p), "无法解析：%s" % exc))
            continue
        got = (d.get("agent") or {}).get("child_exec")
        args = (d.get("agent") or {}).get("child_args") or []
        if got != want:
            results.append((p, "child_exec 是 %r，应为 %r" % (got, want)))
        elif not ("-E" in args and "-s" in args):
            results.append((p, "child_args 缺 -E -s（会被系统用户级 site-packages 污染）：%r" % (args,)))
    return results


def main() -> int:
    print("仓库根：", ROOT)
    print("运行时：", PY_DIR)
    if not os.path.isdir(PY_DIR):
        print("\n[失败] runtimes/python 不存在。")
        print("  这是本地整理出来的运行时，不在 git 里。重建方法见 docs/zh_cn/生存大冒险-使用说明.md。")
        return 1
    print("体积：  ", _mb(_size(PY_DIR)))
    print()

    ok = True

    missing = check_files()
    if missing:
        ok = False
        print("[失败] 缺少以下条目：")
        for m in missing:
            print("   -", m)
    else:
        print("[通过] 必需文件齐全")

    good, msg = check_probe()
    if good:
        print("[通过] 导入检查：", msg)
    else:
        ok = False
        print("[失败] 导入检查：", msg)

    problems = check_interface()
    if problems:
        ok = False
        for name, why in problems:
            print("[失败] interface 配置：", name)
            print("       ", why)
    else:
        print("[通过] interface.json 的 child_exec / child_args 正确")

    print()
    print("结论：", "全部通过" if ok else "有问题，见上面")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
