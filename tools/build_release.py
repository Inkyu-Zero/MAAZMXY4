#!/usr/bin/env python3
"""构建「生存大冒险」发布包（CI 与本地共用）。

用法：
    python tools/build_release.py --version 1.1 --out dist

产物：
    dist/SurvivalAdventure-v{version}-slim.zip    纯脚本包（约 40 KB）

为什么这里只做 slim：完整版要额外塞进 MAA 本体（libs/runtimes，约 200 MB）和
Python 运行时，需要下载外部依赖、体积也大，不适合放在 CI 里构建。完整版请本地打包。

注意：`resource/` 与 `agent/` 在 .gitignore 里，其中 `agent/` 已用 `git add -f` 纳入版本控制
（否则 Agent 代码进不了仓库），而公告文件不在仓库中，所以这里**内联生成**公告内容。
"""

from __future__ import annotations

import argparse
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (仓库内源路径, 包内路径)
FILES = [
    ("agent/adventure.py", "agent/adventure.py"),
    ("agent/adventure_logic.py", "agent/adventure_logic.py"),
    ("assets/data/adventure.json", "assets/data/adventure.json"),
    ("assets/data/adventure_ui.json", "assets/data/adventure_ui.json"),
    ("assets/resource/base/pipeline/生存大冒险.json", "resource/base/pipeline/生存大冒险.json"),
    ("assets/resource/base/image/继续冒险.png", "resource/base/image/继续冒险.png"),
]

ANNOUNCEMENT = """# 关于本脚本

本脚本基于 **MaaFramework** 开发，作者 **InkyuZero**（GitHub：[Inkyu-Zero](https://github.com/Inkyu-Zero/MAAZMXY4)）。

**本脚本完全免费**，仅供学习交流使用。

---

## ⚠️ 如果你是付费购买的

**请立刻申请退款！** 本脚本从未收费，任何向你收费的人都与作者无关。

发现倒卖也欢迎向作者举报。

---

- 项目地址：https://github.com/Inkyu-Zero/MAAZMXY4
"""

INSTALL_DOC = """# 造梦西游4 · 生存大冒险 自动答题（纯脚本包）

**作者：InkyuZero** ｜ 项目地址：https://github.com/Inkyu-Zero/MAAZMXY4

本脚本基于 **MaaFramework** 开发，**完全免费**。如果你是付费购买的，请及时申请退款。

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
   resource/announcement/关于本脚本.md
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

本脚本完全免费，禁止任何形式的倒卖与付费分发。
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
            "agent.child_exec 默认用 py（系统 Python）；",
            "若使用内置 Python 的便携包，请改为 ./python/python.exe。",
        ],
        "task": [task],
        "agent": iface.get("agent"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="构建生存大冒险发布包")
    ap.add_argument("--version", default=None, help="版本号，例如 1.1；默认从 tag 或 interface.json 推断")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    args = ap.parse_args()

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
            ("resource/announcement/关于本脚本.md", ANNOUNCEMENT),
        ):
            zf.writestr(f"{base}/{name}", text)
            print(f"  + {name}  (生成, {len(text.encode('utf-8'))} 字节)")

    size_kb = os.path.getsize(zip_path) / 1024
    print(f"\n完成: {os.path.basename(zip_path)}  {size_kb:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
