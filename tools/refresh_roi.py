"""把 pipeline JSON 里的百分比 roi 展开成像素。

MAA 只认像素坐标，所以给「按比例写、按像素跑」加一层编译：

    "roi": ["17.4%", "55.6%", "55.8%", "18.1%"]
    "roi": "17.4%,55.6%,55.8%,18.1%"

展开成（基准 1147x720）：

    "roi": [199, 400, 640, 130]

用法：

    python tools/refresh_roi.py            # 展开所有 pipeline
    python tools/refresh_roi.py --check    # 只报告还有多少没展开
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

BASE_W = 1147
BASE_H = 720
PERCENT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*%\s*$")


def to_px(value, base: int) -> int:
    """百分比字符串 -> 像素；不是百分比就原样返回。"""
    if isinstance(value, str):
        matched = PERCENT.match(value)
        if matched:
            return int(round(float(matched.group(1)) * base / 100.0))
        return int(value)
    return int(value)


def expand_roi(roi, width: int = BASE_W, height: int = BASE_H):
    """把一个 roi 展开成 [x, y, w, h]；认不出的原样返回。"""
    if isinstance(roi, str):
        parts = [p for p in roi.split(",") if p.strip()]
        if len(parts) != 4:
            return roi              # 节点名 / 锚点名，不是坐标
        roi = parts
    if not isinstance(roi, list) or len(roi) != 4:
        return roi
    if not any(isinstance(v, str) and "%" in v for v in roi):
        return roi                  # 已经是像素，不动
    return [to_px(roi[0], width), to_px(roi[1], height),
            to_px(roi[2], width), to_px(roi[3], height)]


def walk(node, width: int, height: int) -> int:
    """递归处理一个 JSON 树，返回展开了几处。"""
    if isinstance(node, dict):
        changed = 0
        for key, value in list(node.items()):
            if key in ("roi", "target") and not isinstance(value, dict):
                new = expand_roi(value, width, height)
                if new is not value and new != value:
                    node[key] = new
                    changed += 1
            else:
                changed += walk(value, width, height)
        return changed
    if isinstance(node, list):
        return sum(walk(item, width, height) for item in node)
    return 0


def pending(node) -> int:
    """数一下还有多少处百分比没展开。"""
    if isinstance(node, dict):
        total = 0
        for key, value in node.items():
            if key in ("roi", "target") and isinstance(value, list):
                if any(isinstance(v, str) and "%" in v for v in value):
                    total += 1
            total += pending(value)
        return total
    if isinstance(node, list):
        return sum(pending(item) for item in node)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="百分比 roi -> 像素")
    parser.add_argument("--check", action="store_true", help="只检查，不写回")
    parser.add_argument("--dir", action="append", help="pipeline 目录，可多次指定")
    parsed = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    # 源是 pipeline_src/（百分比），产物写回 pipeline/（像素，MFA 实际加载的）。
    pairs = [(str(root / "resource" / "base" / "pipeline_src"),
              str(root / "resource" / "base" / "pipeline")),
             (str(root / "assets" / "resource" / "base" / "pipeline_src"),
              str(root / "assets" / "resource" / "base" / "pipeline"))]
    if parsed.dir:
        pairs = [(d, d) for d in parsed.dir]
    total_files = total_changed = total_pending = 0
    for src_dir, out_dir in pairs:
        path = Path(src_dir)
        if not path.is_dir():
            continue
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        for file in sorted(path.glob("*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception as error:
                print("跳过 %s：%s" % (file.name, error))
                continue
            remain = pending(data)
            if parsed.check:
                if remain:
                    print("%s 还有 %d 处百分比" % (file.name, remain))
                    total_pending += remain
                continue
            changed = walk(data, BASE_W, BASE_H)
            total_files += 1
            out_file = Path(out_dir) / file.name
            out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8", newline="\n")
            total_changed += changed
            if changed:
                print("%s 展开 %d 处" % (file.name, changed))

    if parsed.check:
        print("未展开合计：%d 处" % total_pending)
        return 1 if total_pending else 0
    print("处理 %d 个文件，展开 %d 处" % (total_files, total_changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
