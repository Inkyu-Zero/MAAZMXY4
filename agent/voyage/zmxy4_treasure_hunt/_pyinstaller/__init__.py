"""向 PyInstaller 注册项目自带的数据收集 hook。"""

from pathlib import Path


def get_hook_dirs():
    return [str(Path(__file__).resolve().parent / "hooks")]
