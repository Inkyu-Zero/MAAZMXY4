"""收集 DXcam 动态加载的 WinRT 投影、原生扩展和运行库。"""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


# winrt.windows 是无 __init__.py 的命名空间，单独扫描 winrt 会漏掉它。
hiddenimports = collect_submodules("winrt", on_error="raise")
hiddenimports += collect_submodules("winrt.windows", on_error="raise")
binaries = collect_dynamic_libs("winrt")
