"""把系统 Python 里的解释器与依赖整理进仓库的 runtimes/python/。

为什么要这么干：脚本要脱离系统 Python 独立运行，`interface.json` 的
`agent.child_exec` 指向 `runtimes/python/python.exe`。那份运行时是本地
整理出来的（不进 git），一旦被清理工具扫掉，表现是「MAA 启动后 agent
连不上」，日志里连 Python 报错都没有。

本机正常情况下不需要跑这个脚本。只有在 runtimes/python 丢了、或者换了
一台电脑时才需要执行一次：

    python tools/setup_runtime.py          # 从本机 Python 3.12 重建
    python tools/setup_runtime.py --check  # 只做自检

依赖的系统 Python 上必须有 maafw / numpy / strenum / pynput / Pillow。
缺的话先装：  py -3.12 -m pip install maafw numpy strenum pynput Pillow
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(ROOT, "runtimes", "python")

# 解释器本体
TOP_FILES = [
    "python.exe", "python3.dll", "python312.dll", "pythonw.exe",
    "vcruntime140.dll", "vcruntime140_1.dll",
]

# 不裁剪 tkinter（agent 侧可能用到）；只跳过测试、文档和开发工具。
LIB_EXCLUDE = {
    "site-packages", "__pycache__", "test", "tests", "idlelib",
    "turtledemo", "ensurepip", "venv", "pydoc_data", "lib2to3", "distutils",
}

# 必须一起带走的第三方包（maa 的运行时依赖）
SITE_PACKAGES = [
    "maa", "MaaAgentBinary", "numpy", "numpy.libs", "strenum", "pynput", "PIL", "pillow.libs",
]
SITE_FILES = ["typing_extensions.py", "six.py"]


def find_system_python() -> str | None:
    """返回系统 Python 3.12 的安装目录（sys.prefix）。"""
    probes = [
        ["py", "-3.12", "-c", "import sys; print(sys.prefix)"],
        ["python", "-c", "import sys; print(sys.prefix)"],
        ["python3", "-c", "import sys; print(sys.prefix)"],
    ]
    for cmd in probes:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            prefix = (r.stdout or "").strip().splitlines()
            if prefix and os.path.isdir(prefix[-1]):
                return prefix[-1]
    return None


def has_maa(prefix: str) -> bool:
    r = subprocess.run(
        [os.path.join(prefix, "python.exe"), "-c",
         "import maa, numpy, strenum; print('ok')"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    return r.returncode == 0


def copy_tree(src: str, dst: str, skip: set | None = None) -> int:
    """复制目录，返回复制的文件数。"""
    n = 0
    skip = skip or set()
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip]
        rel = os.path.relpath(base, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in files:
            try:
                shutil.copy2(os.path.join(base, f), os.path.join(target, f))
                n += 1
            except OSError as exc:
                print("  跳过 %s：%s" % (f, exc))
    return n


def build(prefix: str) -> int:
    if os.path.isdir(PY_DIR):
        print("runtimes/python 已存在，先删除重建…")
        shutil.rmtree(PY_DIR)
    os.makedirs(os.path.join(PY_DIR, "Lib", "site-packages"), exist_ok=True)

    print("1) 解释器本体")
    for f in TOP_FILES:
        src = os.path.join(prefix, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(PY_DIR, f))
            print("   +", f)

    print("2) DLLs")
    n = copy_tree(os.path.join(prefix, "DLLs"), os.path.join(PY_DIR, "DLLs"))
    print("   %d 个文件" % n)
    # 保留 Tcl/Tk，维持已有 Python 组件的运行时兼容性。
    n = copy_tree(os.path.join(prefix, "tcl"), os.path.join(PY_DIR, "tcl"))
    print("   Tcl/Tk: %d 个文件" % n)

    print("3) 标准库（跳过 %s）" % "、".join(sorted(LIB_EXCLUDE)))
    n = copy_tree(os.path.join(prefix, "Lib"), os.path.join(PY_DIR, "Lib"), LIB_EXCLUDE)
    print("   %d 个文件" % n)

    print("4) 第三方包")
    sp_src = os.path.join(prefix, "Lib", "site-packages")
    sp_dst = os.path.join(PY_DIR, "Lib", "site-packages")
    for pkg in SITE_PACKAGES:
        src = os.path.join(sp_src, pkg)
        if os.path.isdir(src):
            n = copy_tree(src, os.path.join(sp_dst, pkg))
            print("   + %-18s %d 个文件" % (pkg, n))
        else:
            print("   ! %s 不存在（系统 Python 里没装？）" % pkg)
    for f in SITE_FILES:
        src = os.path.join(sp_src, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(sp_dst, f))
            print("   +", f)
    # dist-info 保留版本信息
    for name in os.listdir(sp_src):
        if name.endswith(".dist-info") and any(
            name.lower().startswith(p.lower() + "-")
            for p in ("maafw", "MaaAgentBinary", "numpy", "strenum", "pynput", "six", "pillow")
        ):
            copy_tree(os.path.join(sp_src, name), os.path.join(sp_dst, name))
            print("   +", name)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做自检，不重建")
    args = ap.parse_args()

    if args.check:
        return subprocess.call([sys.executable, os.path.join(ROOT, "tools", "runtime_check.py")])

    prefix = find_system_python()
    if not prefix:
        print("找不到系统 Python 3.12。请先安装，或手工把 Python 目录放到 runtimes/python。")
        return 1
    print("系统 Python：", prefix)
    if not has_maa(prefix):
        print("这个 Python 里 import maa / numpy / strenum 失败。")
        print("请先执行：  py -3.12 -m pip install maa")
        return 1

    build(prefix)
    print("\n重建完成，开始自检…\n")
    return subprocess.call([sys.executable, os.path.join(ROOT, "tools", "runtime_check.py")])


if __name__ == "__main__":
    sys.exit(main())
