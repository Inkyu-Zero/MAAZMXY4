"""生存大冒险脚本自检：JSON/语法/节点引用/命名对齐。一次性校验脚本。"""

import json
import os
import py_compile
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

ok = True


def check(label, passed, extra=""):
    global ok
    if not passed:
        ok = False
    print(f"{'OK  ' if passed else 'FAIL'} {label}{(' -> ' + str(extra)) if extra else ''}")


# 1) JSON 可解析
json_files = [
    "assets/interface.json",
    "assets/data/adventure.json",
    "assets/data/adventure_ui.json",
    "assets/resource/base/pipeline/生存大冒险.json",
]
data = {}
for f in json_files:
    try:
        data[f] = json.load(open(f, encoding="utf-8"))
        check(f"JSON 解析 {f}", True)
    except Exception as exc:
        check(f"JSON 解析 {f}", False, exc)

# 2) Python 语法
for f in ["agent/adventure.py", "agent/adventure_logic.py", "agent/main.py"]:
    try:
        py_compile.compile(f, cfile=os.path.join(os.environ.get("TEMP", "."), "dsh_check.pyc"), doraise=True)
        check(f"Python 语法 {f}", True)
    except Exception as exc:
        check(f"Python 语法 {f}", False, exc)

pipeline = data.get("assets/resource/base/pipeline/生存大冒险.json", {})

# 节点可以跨文件被引用，因此引用检查要合并 pipeline 目录下的全部文件
pipeline_dir = "assets/resource/base/pipeline"
nodes = {}
for fname in os.listdir(pipeline_dir):
    if not fname.endswith(".json"):
        continue
    try:
        nodes.update(json.load(open(os.path.join(pipeline_dir, fname), encoding="utf-8")))
    except Exception as exc:
        check(f"JSON 解析 {pipeline_dir}/{fname}", False, exc)
nodes = set(nodes)

# 3) pipeline 内部引用完整
missing = []
for name, body in pipeline.items():
    for field in ("next", "on_error"):
        value = body.get(field)
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            target = item.get("name") if isinstance(item, dict) else item
            if isinstance(target, str) and target not in nodes:
                missing.append(f"{name}.{field} -> {target}")
check("pipeline next/on_error 引用都存在", not missing, missing)

# 4) Agent 使用的 OCR 节点名必须存在于 pipeline
attr_nodes = [f"生存大冒险_属性_{n}" for n in ("健康", "战力", "智慧", "魅力", "运气", "灵巧")]
need = ["生存大冒险_识别题干"] + attr_nodes
absent = [n for n in need if n not in nodes]
check("Agent 依赖的 OCR 节点都存在", not absent, absent)

# 5) adventure_ui.json 的事件特征必须能在题库中找到
ui = data.get("assets/data/adventure_ui.json", {})
lib = data.get("assets/data/adventure.json", {})
lib_events = set((lib.get("events") or {}).keys())
ui_events = set((ui.get("events") or {}).keys())
check("ui.events 均存在于题库", ui_events <= lib_events, sorted(ui_events - lib_events))
check("option_boxes 恰好 4 个", len(ui.get("layout", {}).get("option_boxes", [])) == 4)
check(
    "attr_rows 覆盖六维",
    set(ui.get("layout", {}).get("attr_rows", {}).keys()) == set(("健康", "战力", "智慧", "魅力", "运气", "灵巧")),
)

# 5b) pipeline 里的 ROI 必须与 adventure_ui.json 完全一致，防止两处漂移
layout = ui.get("layout", {})
roi_pairs = [("生存大冒险_识别题干", layout.get("question_roi"))]
for attr_name, roi in (layout.get("attr_rows") or {}).items():
    roi_pairs.append((f"生存大冒险_属性_{attr_name}", roi))
for idx, roi in enumerate(layout.get("option_ocr_rois") or [], start=1):
    roi_pairs.append((f"生存大冒险_选项_{idx}", roi))

drift = []
for node_name, expect in roi_pairs:
    param = ((pipeline.get(node_name) or {}).get("recognition") or {}).get("param") or {}
    actual = param.get("roi")
    if expect is not None and actual != expect:
        drift.append(f"{node_name}: pipeline={actual} ui={expect}")
check("pipeline ROI 与 adventure_ui.json 一致", not drift, drift)

# 6) interface.json 的任务入口都在 pipeline 中
iface = data.get("assets/interface.json", {})
entries = [t.get("entry") for t in iface.get("task", [])]
bad = [e for e in entries if e and e not in nodes]
check("interface.json 的 task.entry 都存在", not bad, bad)

# 7) 事件匹配自测：用截图里读到的真实界面文案，验证「按选项文本反查事件」是否准确
sys.path.insert(0, os.path.join(ROOT, "agent"))
import adventure_logic as logic  # noqa: E402

lib_events = logic.load_events()
samples = [
    (
        "危险任务",
        ["坚定不移的接受任务", "我拒绝接受任务，自己出去冒险", "默默不做声", "推荐你的仇敌"],
    ),
]
for expect, texts in samples:
    got = logic.match_event_by_options(lib_events, texts)
    check(f"选项匹配「{expect}」", got == expect, f"实际={got}")

    ranking = []
    for name, body in lib_events.items():
        opts = body.get("options") or []
        if len(opts) != len(texts):
            continue
        avg = sum(logic.text_similarity(o.get("text", ""), t) for o, t in zip(opts, texts)) / len(texts)
        ranking.append((avg, name))
    ranking.sort(reverse=True)
    print("     候选前 5：" + "，".join(f"{n}={s:.2f}" for s, n in ranking[:5]))

# 8) 题库统计
events = lib.get("events") or {}
total = sum(len(e["options"]) for e in events.values())
uncertain = sum(1 for e in events.values() for o in e["options"] if o.get("_uncertain"))
always_fail = sum(1 for e in events.values() for o in e["options"] if o.get("always_fail"))
print(f"\n题库：{len(events)} 个事件 / {total} 个选项 / 待核对 {uncertain} 项 / 必定失败 {always_fail} 项")
print(f"已登记界面特征的事件：{len(ui_events)} 个 -> {sorted(ui_events)}")
print(f"接口任务：{entries}")

print("\n" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
