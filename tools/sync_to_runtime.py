"""把 assets/（规范目录）里的「生存大冒险」相关配置同步到项目根目录的运行副本。

规则：
  - 生存大冒险* 任务：**以 assets 为准整体替换**（这样新增、修改、删除都能同步过去）
  - 你的其他任务：原样保留，不动
  - 由本脚本管理的选项（MANAGED_OPTIONS）：assets 里没有了就从运行副本删掉
  - 其他选项：只追加缺失的

背景：本项目 assets/ 是规范目录，但直接双击 exe 运行时 MFAAvalonia 读的是根目录的
interface.json 与 resource/（Release 运行副本），两者内容并不相同。只改 assets 不会生效。

同步前会备份成 interface.json.bak，可重复执行。
"""

import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_INTERFACE = os.path.join(ROOT, "assets", "interface.json")
DST_INTERFACE = os.path.join(ROOT, "interface.json")
SRC_PIPELINE_DIR = os.path.join(ROOT, "assets", "resource", "base", "pipeline")
DST_PIPELINE_DIR = os.path.join(ROOT, "resource", "base", "pipeline")
SRC_IMAGE_DIR = os.path.join(ROOT, "assets", "resource", "base", "image")
DST_IMAGE_DIR = os.path.join(ROOT, "resource", "base", "image")

PIPELINE_FILES = ["生存大冒险.json"]
IMAGE_FILES = ["继续冒险.png"]

MANAGED_TASK_PREFIX = "生存大冒险"
MANAGED_OPTIONS = {"生存大冒险初始属性"}


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    if not os.path.exists(DST_INTERFACE):
        print(f"运行副本不存在：{DST_INTERFACE}")
        return 1

    src = load(SRC_INTERFACE)
    dst = load(DST_INTERFACE)
    shutil.copy2(DST_INTERFACE, DST_INTERFACE + ".bak")

    changed = []

    # 1) agent 配置以 assets 为准
    if src.get("agent") and dst.get("agent") != src["agent"]:
        dst["agent"] = src["agent"]
        changed.append("agent 配置")

    # 2) 生存大冒险任务整体替换；你的其他任务原样保留
    #    注意：必须按「前缀」剔除旧任务——若按"名字是否出现在 assets 里"判断，
    #    assets 中已删除的任务会因为名字不在 src_names 里而被保留下来（指向不存在的节点）
    src_tasks = [t for t in src.get("task", []) if str(t.get("name", "")).startswith(MANAGED_TASK_PREFIX)]
    kept_tasks = [
        t for t in dst.get("task", []) if not str(t.get("name", "")).startswith(MANAGED_TASK_PREFIX)
    ]
    before = [t.get("name") for t in dst.get("task", [])]
    dst["task"] = kept_tasks + src_tasks
    after = [t.get("name") for t in dst["task"]]
    if before != after:
        changed.append(f"任务列表更新（{len(before)} -> {len(after)}）")

    # 3) 选项：先删掉 assets 里已不存在的受管选项，再追加缺失的
    dst_option = dst.setdefault("option", {})
    src_option = src.get("option") or {}
    for key in list(dst_option.keys()):
        if key in MANAGED_OPTIONS and key not in src_option:
            del dst_option[key]
            changed.append(f"移除选项「{key}」")
    for key, value in src_option.items():
        if key not in dst_option:
            dst_option[key] = value
            changed.append(f"追加选项「{key}」")

    dump(DST_INTERFACE, dst)
    if changed:
        print("已同步：")
        for item in changed:
            print("  -", item)
    else:
        print("interface.json 无需变化")

    # 4) 复制 pipeline 文件
    os.makedirs(DST_PIPELINE_DIR, exist_ok=True)
    for name in PIPELINE_FILES:
        src_file = os.path.join(SRC_PIPELINE_DIR, name)
        dst_file = os.path.join(DST_PIPELINE_DIR, name)
        if os.path.exists(src_file):
            shutil.copy2(src_file, dst_file)
            print(f"  已复制 pipeline：{name} ({os.path.getsize(dst_file)} 字节)")

    # 4b) 复制模板图片
    os.makedirs(DST_IMAGE_DIR, exist_ok=True)
    for name in IMAGE_FILES:
        src_file = os.path.join(SRC_IMAGE_DIR, name)
        dst_file = os.path.join(DST_IMAGE_DIR, name)
        if os.path.exists(src_file):
            shutil.copy2(src_file, dst_file)
            print(f"  已复制模板图：{name} ({os.path.getsize(dst_file)} 字节)")
        else:
            print(f"  ⚠ 缺少模板图：{name}")

    # 5) 校验
    check = load(DST_INTERFACE)
    tasks = check.get("task", [])
    print(f"\n运行副本现有 {len(tasks)} 个任务：")
    for t in tasks:
        print(f"  - {t.get('name')}")
    print(f"agent 配置：{check.get('agent')}")
    print(f"剩余选项数：{len(check.get('option') or {})}")

    nodes = set()
    for fname in os.listdir(DST_PIPELINE_DIR):
        if fname.endswith(".json"):
            try:
                nodes.update(load(os.path.join(DST_PIPELINE_DIR, fname)).keys())
            except Exception as exc:
                print(f"  ⚠ 无法解析 pipeline {fname}: {exc}")
    missing = [t.get("entry") for t in tasks if t.get("entry") not in nodes]
    if missing:
        print("  ⚠ 以下任务入口找不到节点：", missing)
    else:
        print(f"  ✓ 全部 {len(tasks)} 个任务的入口都能解析到节点（共 {len(nodes)} 个节点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
