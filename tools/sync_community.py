# -*- coding: utf-8 -*-
"""玩家自由下载社区刷取方法工具（独立小工具，不改客户端）。

玩家从 GitHub / Gitee 仓库的 community-scripts/ 目录，按需下载刷取方法，
保存到本项目的 resource/base/pipeline/community/ 目录。

常用用法（在项目根目录）：
    python tools/sync_community.py --list              # 1. 先看仓库有哪些方法（含简述）
    python tools/sync_community.py --only 唐僧低战      # 2. 只下载指定的方法
    python tools/sync_community.py --only 悟空 --only 沙僧   #    下载多个
    python tools/sync_community.py --gitee --list       #    从 Gitee 看（默认 GitHub）
    python tools/sync_community.py --source <URL>        #    自定义源

下载到本地后，重启 MFA 客户端，方法即作为独立任务出现在任务列表。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMUNITY_DIR = os.path.join(ROOT, "resource", "base", "pipeline", "community")

GITHUB_API = "https://api.github.com/repos/Inkyu-Zero/MAAZMXY4/contents/community-scripts"
GITEE_API = "https://gitee.com/api/v5/repos/InkyuZero/MAAZMXY4/contents/community-scripts"


def fetch_url(url, timeout=30):
    """请求 URL，返回字节；失败返回 None。"""
    req = urllib.request.Request(url, headers={"User-Agent": "MAAZMXY4-sync"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"  [失败] 请求失败: {e}", file=sys.stderr)
        return None


def list_community_files(source_api):
    """列出 community-scripts 下的 .json 文件（含描述，从文件内读取首个 description 字段）。"""
    data = fetch_url(source_api)
    if not data:
        return []
    try:
        items = json.loads(data)
    except json.JSONDecodeError:
        return []
    files = []
    for it in items:
        if not isinstance(it, dict) or not it.get("name", "").endswith(".json"):
            continue
        url = it.get("download_url") or it.get("raw_url") or ""
        files.append({"name": it["name"], "url": url})
    return files


def read_description(content):
    """尝试从方法文件里读一句简述（文件内第一个 'description' 字段或注释）。"""
    try:
        data = json.loads(content)
        # 递归找一个字符串 description
        for v in data.values():
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, str) and len(vv) < 60:
                        return vv
    except Exception:
        pass
    return ""


def download_method(name, url, only_set, dry_run=False):
    """按需下载单个方法。only_set 为空则下载全部；否则只下载在集合里的。"""
    if only_set and name not in only_set:
        return None, False
    print(f"  下载 {name} ...")
    if dry_run:
        return name, True
    content = fetch_url(url)
    if content is None:
        return name, False
    try:
        json.loads(content)  # 校验
    except json.JSONDecodeError as e:
        print(f"    [跳过] 不是有效方法: {e}", file=sys.stderr)
        return name, False
    os.makedirs(COMMUNITY_DIR, exist_ok=True)
    with open(os.path.join(COMMUNITY_DIR, name), "wb") as f:
        f.write(content)
    print(f"    [完成] 保存到 resource/base/pipeline/community/{name}")
    return name, True


INTERFACE_FILE = os.path.join(ROOT, "interface.json")
ASSETS_INTERFACE_FILE = os.path.join(ROOT, "assets", "interface.json")


def register_task(interface_path, method_name):
    """把下载的方法注册为 interface.json 里的独立任务（已存在则跳过）。

    方法文件名形如：刷取灵魂_高战通用.json
    -> 任务名：刷取灵魂（高战通用）
    -> 入口：读取方法文件内的第一个节点（pipeline 的入口）
    返回 (任务名, 是否新增)。
    """
    try:
        with open(interface_path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [警告] 读取 {interface_path} 失败: {e}", file=sys.stderr)
        return None, False

    # 任务名 = 去掉 .json 和后缀里的"刷取灵魂_"前缀
    base = method_name[:-5]  # 去掉 .json
    task_name = base.replace("刷取灵魂_", "刷取灵魂·", 1) if base.startswith("刷取灵魂_") else base

    # 已存在则跳过
    if any(t.get("name") == task_name for t in data.get("task", [])):
        return task_name, False

    # 读取方法文件的入口节点（第一个非 $__mpe 的节点）
    method_path = os.path.join(COMMUNITY_DIR, method_name)
    entry = None
    try:
        with open(method_path, encoding="utf-8-sig") as f:
            mdata = json.load(f)
        for k in mdata:
            if not k.startswith("$"):
                entry = k
                break
    except (OSError, json.JSONDecodeError):
        entry = None
    if not entry:
        print(f"  [警告] 无法读取方法入口，跳过注册 {task_name}", file=sys.stderr)
        return task_name, False

    data.setdefault("task", []).append({
        "name": task_name,
        "type": "pipeline",
        "entry": entry,
        "description": f"社区刷取方法：{task_name}。\n\n使用前确认已进游戏，并设置「循环次数」「账号名称」「存档序号」等选项。",
        "option": ["循环次数", "账号名称", "存档序号", "卡死重启"],
    })
    with open(interface_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  [注册] 已把 {task_name} 加入任务列表")
    return task_name, True


def main():
    parser = argparse.ArgumentParser(description="玩家自由下载社区刷取方法")
    parser.add_argument("--list", action="store_true", help="只列出仓库里的方法（含简述），不下载")
    parser.add_argument("--only", action="append", help="只下载指定名称的方法，可多次指定（不含扩展名 .json）")
    parser.add_argument("--gitee", action="store_true", help="从 Gitee 拉取（默认 GitHub）")
    parser.add_argument("--source", help="自定义源 API 地址")
    parser.add_argument("--dry-run", action="store_true", help="模拟下载，不写文件")
    args = parser.parse_args()

    source = args.source or (GITEE_API if args.gitee else GITHUB_API)
    print(f"== 仓库: {source} ==\n")

    files = list_community_files(source)
    if not files:
        print("未获取到方法列表（网络问题，或仓库暂无 community-scripts 目录）。")
        return 1

    only_set = set(args.only or [])
    # 列出
    print(f"仓库共有 {len(files)} 个刷取方法：")
    for m in files:
        desc = ""
        if args.list:
            c = fetch_url(m["url"])
            if c:
                desc = read_description(c)
        print(f"  - {m['name']}{('  ' + desc) if desc else ''}")

    if args.list:
        print("\n提示：用 --only 名称 下载指定方法（可多次 --only 下载多个）。")
        return 0

    if only_set:
        print(f"\n按需下载 {len(only_set)} 个方法：")
    else:
        print(f"\n下载全部 {len(files)} 个方法：")
    ok = 0
    for m in files:
        name, success = download_method(m["name"], m["url"], only_set, args.dry_run)
        if success:
            ok += 1
            if not args.dry_run:
                register_task(INTERFACE_FILE, name)
                register_task(ASSETS_INTERFACE_FILE, name)
    print(f"\n完成 {ok} 个。重启 MFA 后，下载的方法出现在任务列表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())