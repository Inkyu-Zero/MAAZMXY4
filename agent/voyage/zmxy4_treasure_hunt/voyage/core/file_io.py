"""文件替换时容忍 Windows 短暂的文件占用，不删除已有目标文件。"""

from pathlib import Path
import time


def replace_with_retry(source: Path, target: Path):
    """原子替换；Windows 访问/共享冲突最多重试 6 次，失败保留临时文件。"""
    delays = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)
    for attempt in range(len(delays) + 1):
        try:
            source.replace(target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == len(delays):
                raise
            time.sleep(delays[attempt])
