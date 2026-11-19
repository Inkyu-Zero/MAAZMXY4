"""无需初始化显示设备的发布包依赖检查。"""

import importlib
import json
from pathlib import Path
import sys
import traceback


WINRT_MODULES = (
    "winrt.runtime",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.graphics",
    "winrt.windows.graphics.capture",
    "winrt.windows.graphics.capture.interop",
    "winrt.windows.graphics.directx",
    "winrt.windows.graphics.directx.direct3d11",
    "winrt.windows.graphics.directx.direct3d11.interop",
)


def check_winrt_dependencies():
    # 保持动态导入，与 DXcam 的加载方式一致，防止静态导入掩盖 hook 漏包。
    for name in WINRT_MODULES:
        importlib.import_module(name)


def write_runtime_report(path):
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        check_winrt_dependencies()
    except Exception:
        report["error"] = traceback.format_exc()
    else:
        report["ok"] = True
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
