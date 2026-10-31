#!/usr/bin/env python3
"""构建发布包（CI 与本地共用）。

完整客户端：
    python tools/build_release.py --client --launcher <官方解压目录/MAA造梦西游4.exe>
    python tools/build_release.py --verify-client <解压目录>

完整包从 assets/resource、agent、assets/interface.json
取资源，再合并明确指定的前端和 Python。不要压缩正在使用的客户端目录。

用法：
    python tools/build_release.py --version 1.1 --out dist

产物：
    dist/SurvivalAdventure-v{version}-slim.zip    纯脚本包（约 40 KB）

完整包要求先构建前端和准备运行时；不下载、不修改系统依赖。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

# ---------------------------------------------------------------------------
# 文件收集（原 tools/build_update.py 里的 collect 系列，更新机制剥离后内联到这里）
# ---------------------------------------------------------------------------

# 发布版 interface.json 优先取 assets/ 下那份（干净的对外配置），
# 根目录那份是自用副本，可能多出私有任务。
ITEMS = ("resource", "agent")
INTERFACE_CANDIDATES = ("assets/interface.json", "interface.json")

# 绝不进发布包的文件：私有插件、备份、字节码、私有 pipeline。
EXCLUDE_NAMES = {
    "adventure_order.py",         # 题序规则（自用）
    "adventure_local_hook.py",    # 本地钩子（自用）
    "adventure_drops.py",         # 掉落预判（自用）
    "adventure_drops_action.py",  # 掉落预判的 CustomRecognition 注册（自用）
    "掉落预判.json",               # 上面那个插件的 pipeline
}
EXCLUDE_DIRS = {"__pycache__", ".git", "recordings"}


def is_local_instructions(name):
    """Local agent instructions are never distributed as client data."""
    return name.casefold() == "agents.md"


def _skip(name):
    if name in EXCLUDE_NAMES or is_local_instructions(name) or name in ('.gitignore', '.gitattributes', '.gitkeep'):
        return True
    if ".bak" in name or name.endswith(".pyc") or name.endswith(".pyo"):
        return True
    return False


def _pick_dir(root, item):
    """优先用 assets/ 下的规范源，没有再退回根目录的运行副本。"""
    for cand in (os.path.join("assets", item), item):
        p = os.path.join(root, cand)
        if os.path.exists(p):
            return p
    return None


def collect(root):
    """返回 [(绝对路径, zip 内相对路径)]。"""
    files = []
    for item in ITEMS:
        p = _pick_dir(root, item)
        if not p:
            raise SystemExit("缺少 %s，确认在包根目录运行" % item)
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for name in filenames:
                if _skip(name):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, p).replace("\\", "/")
                files.append((full, item + "/" + rel))

    # Root recordings are personal. agent/recordings contains only the reviewed
    # manifest and its declared public defaults; never sweep that folder whole.
    defaults = os.path.join(_pick_dir(root, 'agent'), 'recordings')
    manifest = os.path.join(defaults, 'manifest.json')
    if os.path.isfile(manifest):
        with open(manifest, encoding='utf8') as stream:
            bundle = json.load(stream)
        if bundle.get('version') != 1 or not isinstance(bundle.get('items'), list):
            raise ValueError('公共录制清单格式无效。')
        files.append((manifest, 'agent/recordings/manifest.json'))
        seen = set()
        for item in bundle['items']:
            name = item.get('file', '')
            if (not name.endswith('.json') or name.startswith('.') or name == 'manifest.json'
                    or any(char in name for char in '/\\:') or name in seen):
                raise ValueError('公共录制清单路径无效。')
            seen.add(name)
            path = os.path.join(defaults, name)
            if not os.path.isfile(path) or hashlib.sha256(Path(path).read_bytes()).hexdigest() != item.get('sha256'):
                raise ValueError('公共录制与清单哈希不一致：' + name)
            files.append((path, 'agent/recordings/' + name))

    for cand in INTERFACE_CANDIDATES:
        p = os.path.join(root, cand)
        if os.path.isfile(p):
            files.append((p, "interface.json"))
            break
    else:
        raise SystemExit("缺少 interface.json（assets/ 或根目录都没有）")
    return files

from runtime_check import REQUIRED as AGENT_RUNTIME_REQUIRED, required_path_exists

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER_NAME = 'MAA造梦西游4.exe'
# Tests may supply a fixture digest. Production requires a source/build receipt.
LAUNCHER_SHA256 = None
FRONTEND_ROOT_FILES = (
    'MFAAvalonia.exe', 'MFAAvalonia.dll', 'MFAAvalonia.deps.json',
    'MFAAvalonia.runtimeconfig.json', 'libloader.dll', 'script-studio-build.json',
    'LICENSE-MFAAvalonia.txt',
)
FRONTEND_REQUIRED = FRONTEND_ROOT_FILES + (
    'libs/MFAAvalonia.Core.dll', 'runtimes/win-x64/native/MaaFramework.dll',
)
PYTHON_REQUIRED = tuple(Path(name).as_posix() for name in AGENT_RUNTIME_REQUIRED)


def require_paths(root, names, label):
    for name in names:
        if not required_path_exists(root, name):
            raise ValueError(label + '缺少：' + name)


def validate_launcher(launcher):
    path = Path(launcher)
    if not path.is_file() or path.is_symlink():
        raise ValueError('缺少官方启动器：' + str(path))
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if LAUNCHER_SHA256 is not None:
        if actual != LAUNCHER_SHA256:
            raise ValueError('官方启动器 SHA-256 不匹配。')
        return
    receipt_path = path.with_name('launcher-build.json')
    if not receipt_path.is_file():
        raise ValueError('启动器 SHA-256 不匹配或缺少构建记录，请运行 tools/build_launcher.ps1。')
    receipt = json.loads(receipt_path.read_text(encoding='utf8'))
    if (receipt.get('schema') != 1 or receipt.get('sha256') != actual
            or receipt.get('source_sha256') != hashlib.sha256((Path(ROOT)/'tools/launcher/Program.cs').read_bytes()).hexdigest()
            or receipt.get('icon_sha256') != hashlib.sha256((Path(ROOT)/'新图标.ico').read_bytes()).hexdigest()):
        raise ValueError('启动器 SHA-256 不匹配或不是当前源码/图标构建。')


def validate_client_resources(root):
    """Reject mixed releases whose UI advertises routes absent from the bundle."""
    import sys
    sys.path.insert(0, str(Path(ROOT) / 'agent'))
    from task_catalog import _configuration, task_catalog, node_links, read_document
    interface, nodes = _configuration(Path(root).resolve())
    canonical = read_document(Path(ROOT) / 'assets/interface.json')
    tasks = {(t['name'], t['entry']): t for t in task_catalog(root, bindings={})}
    checked = []
    for definition in canonical.get('task', []):
        if '战斗录制内容' not in definition.get('option', []): continue
        task = tasks.get((definition['name'], definition['entry']))
        if task is None: raise ValueError('发布任务入口与源码不一致：' + definition['name'])
        if not task['playback_nodes']: raise ValueError('任务缺少回放节点：' + task['name'])
        for playback in task['playback_nodes']:
            route = task['playback_routes'][playback].get('recorded_return_next')
            if not isinstance(route, list) or not route:
                raise ValueError('发布资源缺少录制返回确认：' + playback + '；请使用 assets/resource 构建整包。')
            targets = list(node_links(nodes, {'next': route}))
            if not targets or any(target not in nodes or not nodes[target].get('recognition') for target in targets):
                raise ValueError('录制返回确认节点不存在或缺少识别：' + playback)
        checked.append(task['name'])
    if interface.get('task') != canonical.get('task') or interface.get('option') != canonical.get('option'):
        raise ValueError('发布任务或选项与 assets/interface.json 不一致，不能使用个人运行配置打包。')
    if interface.get('version') != canonical.get('version'):
        raise ValueError('发布资源版本与 assets/interface.json 不一致。')
    return checked


def validate_client(root):
    """Check the extracted client, direct launcher and bundled Python runtime."""
    root = Path(root)
    validate_launcher(root / LAUNCHER_NAME)
    require_paths(root, FRONTEND_REQUIRED + ('agent/main.py',), '完整客户端')
    require_paths(root / 'runtimes/python', PYTHON_REQUIRED, '内嵌 Python')
    return validate_client_resources(root)


def validate_frontend_source(root, frontend):
    """Reject old custom frontends even when the resource version was bumped."""
    manifest = json.loads((root/'frontend/upstream.json').read_text(encoding='utf8'))
    built = json.loads((frontend/'script-studio-build.json').read_text(encoding='utf8'))
    for key in ('repository', 'commit', 'archive_sha256', 'patches'):
        if built.get(key) != manifest.get(key):
            raise ValueError('前端构建与当前源码不一致：' + key)
    if not built.get('source_sha256') or not built.get('binary_sha256'):
        raise ValueError('前端构建缺少源码及二进制校验记录，请重新构建。')
    # The patch application algorithm is part of the build input. An old
    # builder could produce duplicate UI from the same manifest and overlays.
    sources = ['frontend/upstream.json', 'tools/build_frontend.ps1', *manifest['patches']]
    sources += [p.relative_to(root).as_posix() for p in (root/manifest['overlay_directory']).rglob('*') if p.is_file()]
    for name in sources:
        if built['source_sha256'].get(name) != hashlib.sha256((root/name).read_bytes()).hexdigest():
            raise ValueError('前端源码在构建后发生变化：' + name)
    for name in ('MFAAvalonia.dll', 'libs/MFAAvalonia.Core.dll'):
        if built['binary_sha256'].get(name) != hashlib.sha256((frontend/name).read_bytes()).hexdigest():
            raise ValueError('前端二进制与构建记录不一致：' + name)


def client_files(root, frontend, python_runtime, launcher):
    """Assemble from canonical sources and explicit binaries, never a used client."""
    root, frontend, python_runtime = map(Path, (root, frontend, python_runtime))
    # The update and full packages share the exact same canonical resources.
    files = [(Path(source), relative) for source, relative in collect(str(root))]
    validate_launcher(launcher)
    files.append((Path(launcher), LAUNCHER_NAME))
    if Path(launcher).with_name('launcher-build.json').is_file():
        files.append((Path(launcher).with_name('launcher-build.json'), 'launcher-build.json'))
    require_paths(frontend, FRONTEND_REQUIRED, '前端构建')
    validate_frontend_source(root, frontend)
    for name in FRONTEND_ROOT_FILES: files.append((frontend / name, name))
    for folder in ('libs', 'runtimes/win-x64/native'):
        files.extend((path, path.relative_to(frontend).as_posix())
                     for path in (frontend / folder).rglob('*') if path.is_file())
    require_paths(python_runtime, PYTHON_REQUIRED, '内嵌 Python')
    files.extend((path, 'runtimes/python/' + path.relative_to(python_runtime).as_posix())
                 for path in python_runtime.rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'))
    for name in ('DependencySetup_依赖库安装_win.bat', 'LICENSE', 'NOTICE', 'README.md',
                 'docs/zh_cn/脚本工具-使用说明.md', 'docs/zh_cn/更新协议与发布规范.md',
                 'docs/zh_cn/游戏数据库与导航素材.md', 'docs/zh_cn/游戏管理.md',
                 'docs/zh_cn/个人配置备份与迁移.md',
                 'docs/zh_cn/2026-10-31更新日志.md',
                 'docs/zh_cn/通用导航迁移与失败重试说明.md', 'docs/zh_cn/2.3.9更新说明.md',
                 'docs/zh_cn/2.4.1-rc.1更新说明.md'):
        files.append((root / name, name))
    files = [(source, relative) for source, relative in files
             if not is_local_instructions(Path(relative).name)]
    batch = (root / 'DependencySetup_依赖库安装_win.bat').read_bytes()
    if b'\n' in batch.replace(b'\r\n', b''):
        raise ValueError('依赖安装批处理必须使用 CRLF 换行。')
    for source, _ in files:
        if not source.is_file() or source.is_symlink(): raise ValueError('发布文件缺失或为链接：' + str(source))
    return files


def build_client(root, frontend, python_runtime, destination, launcher, *, client_version=None,
                 repository='Inkyu-Zero/MAAZMXY4', channel='stable', manifest_urls=None):
    """Create a verified full client ZIP without overwriting any existing output."""
    import tempfile
    files = client_files(root, frontend, python_runtime, launcher)
    resource_version = json.loads((Path(root)/'assets/interface.json').read_text(encoding='utf8'))['version']
    client_version = str(client_version or resource_version)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Validate the exact assembled resources, not root's optional runtime copy.
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        import shutil
        for source, relative in files:
            if relative == 'interface.json' or relative.startswith('resource/'):
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        validate_client_resources(stage)
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        for source, relative in files:
            archive.write(source, relative)
        # MaaFramework requires the optional plugin directory to exist.
        archive.writestr('runtimes/win-x64/native/plugins/', b'')
    return destination

# (仓库内源路径, 包内路径)
FILES = [
    ("agent/adventure.py", "agent/adventure.py"),
    ("agent/adventure_logic.py", "agent/adventure_logic.py"),
    ("assets/data/adventure.json", "assets/data/adventure.json"),
    ("assets/data/adventure_ui.json", "assets/data/adventure_ui.json"),
    ("assets/resource/base/pipeline/生存大冒险.json", "resource/base/pipeline/生存大冒险.json"),
    ("assets/resource/base/image/继续冒险.png", "resource/base/image/继续冒险.png"),
]

ANNOUNCEMENT = """# 关于本项目

本项目基于 **MaaFramework** 开发，作者 **InkyuZero**（GitHub：[Inkyu-Zero](https://github.com/Inkyu-Zero/MAAZMXY4)）。

**本项目完全免费**，仅供学习交流使用。

---

## ⚠️ 如果你是付费购买的

**请立刻申请退款！** 本项目从未收费，任何向你收费的人都与作者无关。

发现倒卖也欢迎向作者举报。

---

- 项目地址：https://github.com/Inkyu-Zero/MAAZMXY4
"""

INSTALL_DOC = """# 造梦西游4 · 生存大冒险 自动答题（纯脚本包）

**作者：InkyuZero** ｜ 项目地址：https://github.com/Inkyu-Zero/MAAZMXY4

本项目基于 **MaaFramework** 开发，**完全免费**。如果你是付费购买的，请及时申请退款。

---

## 前置要求

| 项 | 要求 |
| --- | --- |
| MAA 本体 | 造梦西游4 的 MFAAvalonia 整合包（本包不含本体） |
| Python | 3.9+，需 `pip install MaaFw`（版本要与本体里的 MaaFramework 一致） |
| 游戏 | 造梦盒子，窗口保持 **720p 默认大小** |

> MaaFw 与 MaaFramework 版本必须匹配：MaaFW 自 v5.5.0 起改变了 IPC 地址，
> 跨版本会导致 Agent 连接失败。

---

## 安装步骤

1. **复制文件**到你的 MAA 整合包根目录，保持相同结构：

   ```
   agent/adventure.py
   agent/adventure_logic.py
   resource/base/pipeline/生存大冒险.json
   resource/base/image/继续冒险.png
   resource/announcement/关于本项目.md
   assets/data/adventure.json
   assets/data/adventure_ui.json
   ```

2. **注册 Agent 模块**：编辑 `agent/main.py`，在已有 import 后加一行：

   ```python
   import adventure
   ```

3. **追加任务**：把 `interface_patch.json` 里的 `task` 合并进 `interface.json` 的 `task` 数组；
   若没有 `agent` 字段，把 `agent` 也一并加上。

4. **安装依赖**：

   ```powershell
   py -m pip install MaaFw
   ```

5. **重启 MFAAvalonia**，任务列表里就会出现「生存大冒险·自动答题」。

---

## 常见问题

**连接窗口时发生错误**
关闭程序后编辑 `config/instances/default.json`，把 `Win32ControlScreenCapType`
改成 `ScreenDC` 或 `DXGI_DesktopDup_Window`（改前必须关程序）。

**Agent 启动失败 / 没有 adventure.log**
确认 `pip install MaaFw` 成功，并检查版本是否与本体匹配。
日志位置：`debug/adventure/adventure.log`。

**答题时停住不动**
脚本遇到识别不出的题目会主动停下（不胡乱点击）。看
`debug/adventure/collected.jsonl` 里记录的题干，补进 `assets/data/adventure.json` 即可。

---

## 声明

本项目完全免费，禁止任何形式的倒卖与付费分发。
"""


def build_interface_patch() -> dict:
    """从 assets/interface.json 抽出任务与 agent 配置，生成安装补丁。"""
    path = os.path.join(ROOT, "assets", "interface.json")
    with open(path, encoding="utf-8") as f:
        iface = json.load(f)
    task = next((t for t in iface.get("task", []) if t.get("name") == "生存大冒险·自动答题"), None)
    if task is None:
        raise SystemExit("assets/interface.json 里找不到任务「生存大冒险·自动答题」")
    return {
        "_说明": [
            "把下面的 task 追加到你自己的 interface.json 的 task 数组里；",
            "如果你的 interface.json 还没有 agent 字段，把 agent 也加上。",
            "注意 agent.child_exec：官方包里是 runtimes\\python\\python.exe（包内自带运行时，"
            "解压即用）；",
            "这种「补丁包」不带运行时，请把 child_exec 改成 py，并先 pip install maa（见上文第 4 步）。",
        ],
        "task": [task],
        "agent": _agent_for_patch(iface.get("agent")),
    }


def _agent_for_patch(agent):
    """补丁包里的 agent 配置要把 child_exec 从包内运行时改回系统 Python。"""
    if not isinstance(agent, dict):
        return agent
    out = dict(agent)
    if str(out.get("child_exec", "")).replace("\\", "/").startswith("runtimes/"):
        out["child_exec"] = "py"
    return out



def main() -> int:
    ap = argparse.ArgumentParser(description="构建项目完整客户端或生存大冒险脚本包")
    ap.add_argument("--version", default=None, help="版本号，例如 1.1；默认从 tag 或 interface.json 推断")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    ap.add_argument("--client", action="store_true", help="构建含脚本工作台的完整 Windows 客户端")
    ap.add_argument("--frontend", default="dist/script-studio", help="已编译的前端目录")
    ap.add_argument("--python-runtime", default="runtimes/python", help="已准备好的内嵌 Python 目录")
    ap.add_argument("--launcher", help="build_launcher.ps1 生成的启动器，旁边须有 launcher-build.json")
    ap.add_argument("--repository", default="Inkyu-Zero/MAAZMXY4", help="完整客户端更新仓库；fork 候选必须显式指定 fork")
    ap.add_argument("--channel", choices=('stable', 'preview'), default='stable')
    ap.add_argument("--manifest-url", action='append', help="此渠道实际版本清单地址，可多次指定")
    ap.add_argument("--verify-client", help="检查解压后的启动器、前端、Python 和任务资源")
    args = ap.parse_args()

    if args.verify_client:
        checked = validate_client(args.verify_client)
        print(f'完整客户端入口与依赖齐备，已验证 {len(checked)} 个任务的录制返回确认流程。')
        return 0

    if args.client:
        if not args.launcher:
            ap.error('--client 必须显式指定 --launcher，不能遗漏标准启动入口。')
        root = Path(ROOT)
        version = args.version or json.loads((root/'assets/interface.json').read_text(encoding='utf8'))['version']
        if not version or any(c not in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-' for c in version):
            raise ValueError('版本号只能包含数字、字母、点和连字符。')
        destination = root / args.out / f'MAAZMXY4-v{version}-win-x64.zip'
        print(build_client(root, root/args.frontend, root/args.python_runtime, destination, root/args.launcher,
                           client_version=version, repository=args.repository, channel=args.channel,
                           manifest_urls=args.manifest_url))
        return 0

    version = args.version
    if not version:
        # CI 里优先用 tag 名（refs/tags/v1.1 → 1.1），否则从 interface.json 的 version 读
        ref = os.environ.get("GITHUB_REF_NAME", "")
        if ref.startswith("v"):
            version = ref[1:]
        else:
            with open(os.path.join(ROOT, "assets", "interface.json"), encoding="utf-8") as f:
                version = json.load(f).get("version", "0.0")
    version = str(version).lstrip("v")

    out_dir = os.path.join(ROOT, args.out)
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"SurvivalAdventure-v{version}-slim.zip")

    print(f"版本: {version}")
    print(f"输出: {zip_path}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        base = "生存大冒险"
        for rel_src, rel_dst in FILES:
            src = os.path.join(ROOT, rel_src.replace("/", os.sep))
            if not os.path.exists(src):
                raise SystemExit(f"缺少文件: {rel_src}")
            zf.write(src, f"{base}/{rel_dst}")
            print(f"  + {rel_dst}  ({os.path.getsize(src)} 字节)")

        for name, text in (
            ("interface_patch.json", json.dumps(build_interface_patch(), ensure_ascii=False, indent=2) + "\n"),
            ("安装说明.md", INSTALL_DOC),
            ("resource/announcement/关于本项目.md", ANNOUNCEMENT),
        ):
            zf.writestr(f"{base}/{name}", text)
            print(f"  + {name}  (生成, {len(text.encode('utf-8'))} 字节)")

    size_kb = os.path.getsize(zip_path) / 1024
    print(f"\n完成: {os.path.basename(zip_path)}  {size_kb:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
