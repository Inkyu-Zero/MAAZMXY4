# -*- coding: utf-8 -*-
#
# 造梦西游4 · 生存大冒险 自动答题
# 作者：InkyuZero   (https://github.com/Inkyu-Zero/MAAZMXY4)
#
# 本脚本基于 MaaFramework 开发，完全免费。
# 如果你是付费获得的，请及时申请退款 —— 本脚本从未收费。
#
"""生存大冒险 —— 决策核心（纯逻辑，不依赖 maa，可独立测试）

机制（依据用户提供的攻略表转录，见 assets/data/adventure.json）：
  - 每个事件有 4 个选项，每个选项带「成功要求」属性门槛；
  - 当前属性 >= 全部门槛 → 必定成功，否则必定失败（硬门槛，已与用户确认）；
  - 成功/失败分别结算属性增减；每阶段按成败给积分。

决策目标：在"尽量成功（拿大分）"的前提下，让属性收益最大化；若无论如何都会失败，
则选损失最小的选项。
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

ATTRS: Sequence[str] = ("战力", "智慧", "魅力", "灵巧", "运气", "健康")

_DEFAULT_DATA = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "assets", "data", "adventure.json"
)

# 默认属性权重：健康归零即冒险结束，故给更高权重；战力/智慧是后续门槛的主力，略高。
DEFAULT_WEIGHTS: Dict[str, float] = {
    "健康": 1.5,
    "战力": 1.2,
    "智慧": 1.2,
    "魅力": 1.0,
    "灵巧": 1.0,
    "运气": 1.0,
}

# 成功一次的额外加分：积分只与成败相关（成功 6/9/12/19 vs 失败 3/4/6/9），
# 因此"能成功"本身就该压过普通属性收益差异。
DEFAULT_SUCCESS_BONUS = 8.0

# 各属性在题库里出现过的**最高门槛**：决定"现在该补哪个属性最有用"。
# 依据是题库全量统计（不是拍脑袋），括号内为该属性被要求的总次数：
#   战力 10（36 处）  灵巧 10（37 处）  魅力 10（35 处）
#   智慧 10（33 处）  运气  9（35 处）  健康  6（**仅 3 处**）
#
# 注意健康：它的门槛极少、最高只到 6，所以目标设 6 而不是 8。
# 设高了会让健康在"未达标"区间权重虚高，出现"为了加 1 点健康去砍别的属性"。
# 健康真正的风险是归零导致本轮提前结束，那个由 ZERO_PENALTY 单独兜底。
THRESHOLD_TARGETS: Dict[str, int] = {
    "战力": 10,
    "灵巧": 10,
    "魅力": 10,
    "智慧": 10,
    "运气": 9,
    "健康": 6,
}

# 阈值导向的打分参数：离门槛越近的属性，收益价值越高（"差一点就补上"）
NEAR_GAP, MID_GAP = 3, 5
NEAR_WEIGHT, MID_WEIGHT, FAR_WEIGHT = 6.0, 3.0, 1.0

# 健康属于"生死线"，扣血后剩余很少时的额外权重（剩余 1~2 点时随时可能暴毙）
HEALTH_CRITICAL_WEIGHT = 12.0

# 属性跌到 0 及以下的惩罚
ZERO_PENALTY = 1000.0


@dataclass
class Option:
    index: int
    text: str
    require: Optional[Dict[str, int]]
    success: Dict[str, int] = field(default_factory=dict)
    fail: Dict[str, int] = field(default_factory=dict)
    uncertain: bool = False
    always_fail: bool = False


@dataclass
class Decision:
    event: str
    index: int
    text: str
    will_succeed: bool
    gains: Dict[str, int]
    score: float
    reason: str
    all_scores: Sequence[Tuple[int, str, bool, float]] = field(default_factory=tuple)

    def describe(self) -> str:
        return (
            f"[{self.event}] 选第 {self.index + 1} 项「{self.text}」"
            f"（{'必定成功' if self.will_succeed else '必然失败'}，"
            f"属性变化 {self.gains or '无'}，评分 {self.score:+.2f}）"
        )


def load_data(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or _DEFAULT_DATA, "r", encoding="utf-8") as f:
        return json.load(f)


def load_events(path: Optional[str] = None) -> Dict[str, Any]:
    return load_data(path)["events"]


def get_options(event: Mapping[str, Any]) -> list:
    out = []
    for i, raw in enumerate(event["options"]):
        out.append(
            Option(
                index=i,
                text=raw["text"],
                require=raw.get("require"),
                success=raw.get("success") or {},
                fail=raw.get("fail") or {},
                uncertain=bool(raw.get("_uncertain")),
                always_fail=bool(raw.get("always_fail")),
            )
        )
    return out


def can_succeed(opt: Option, attrs: Mapping[str, int]) -> bool:
    """硬门槛判定。

    - always_fail：表格中该选项没有成功态，恒判失败；
    - require 为 None：表格写作「无要求」，必定成功；
    - 其余：当前属性 >= 全部门槛即成功。
    """
    if opt.always_fail:
        return False
    if not opt.require:
        return True
    return all(int(attrs.get(k, 0)) >= int(v) for k, v in opt.require.items())


def _attr_weight(key: str, value: int, attrs: Mapping[str, int]) -> float:
    """给单个属性的变化量定权重（阈值导向，正负对称）。

    规则：
      - **增加**：只对「还没达标」的属性有价值，越接近门槛越值钱；已达标则价值很低
      - **减少**：看变化后是否还达标——跌破门槛就按缺口重罚，仍高于门槛则损失很小

    为什么要分正负两套判断：早先的实现对负收益一律按 1.0 计权，导致
    「+1 健康 / -1 战力」这类选项被严重低估损失——战力已经堆到 9（门槛 10）时，
    减 1 点只扣 1 分，而加 1 健康却按 3~6 分算，于是会做出牺牲战力的选择。
    """
    target = THRESHOLD_TARGETS.get(key, 8)
    cur = int(attrs.get(key, 0))
    after = cur + value

    # 健康特殊处理：它不只是门槛（最高只到 6），更是**生死线**——归零会让本轮提前结束。
    # 所以扣血要按「扣完还剩多少」加权，剩余越少罚得越狠。
    # 不能沿用「距门槛的差距」：那样剩 2 点血(gap=4)会比剩 3 点血(gap=3)罚得更轻，恰好反了。
    if key == "健康" and value < 0:
        if after <= 0:
            # 归零：权重最高（另有 ZERO_PENALTY 兜底，这里保证「剩余越少罚越重」的单调性）
            return HEALTH_CRITICAL_WEIGHT * 2
        if after <= 2:
            return HEALTH_CRITICAL_WEIGHT  # 剩 1~2 点：随时暴毙
        if after <= 3:
            return NEAR_WEIGHT  # 剩 3 点：危险
        # 剩余充足，继续走下面的门槛逻辑

    if value > 0:
        if cur >= target:
            return FAR_WEIGHT  # 已达标，继续堆收益递减
        gap = target - cur
    else:
        if after >= target:
            return FAR_WEIGHT  # 减了仍然达标，损失很小
        gap = target - after  # 减少后形成的缺口

    if gap <= NEAR_GAP:
        return NEAR_WEIGHT
    if gap <= MID_GAP:
        return MID_WEIGHT
    return FAR_WEIGHT


def _gain_score(
    gains: Mapping[str, int],
    attrs: Mapping[str, int],
    weights: Mapping[str, float],
) -> float:
    """按「阈值导向」给属性收益打分（正负对称）。

    依据：阶段的成败是**硬门槛**（属性 >= 要求即成功），而门槛集中在战力/灵巧/魅力
    8~10。实测模拟（各有 4000 局）表明：
      - 每题的收益摊平到六个属性 → 没有一项能长到 8~10 → 阶段4/5 失败率 62%/70%
      - 优先补「离门槛最近」的属性 → 阶段5 成功率 30%→38%，平均分 79.5→82.3
    因此：越接近门槛的属性收益，价值越高（差一点就补上）；
    反过来，**把接近门槛的属性减下去，代价同样高**。
    """
    score = 0.0
    for k, v in gains.items():
        v = int(v)
        if k == "健康" and int(attrs.get("健康", 0)) + v <= 0:
            score -= ZERO_PENALTY  # 健康归零直接判死，权重拉满
        score += float(v) * _attr_weight(k, v, attrs)
    return score


def evaluate(
    opt: Option,
    attrs: Mapping[str, int],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    success_bonus: float = DEFAULT_SUCCESS_BONUS,
) -> Tuple[bool, float, Dict[str, int]]:
    ok = can_succeed(opt, attrs)
    gains = dict(opt.success if ok else opt.fail)
    score = _gain_score(gains, attrs, weights)
    if ok:
        score += success_bonus
    return ok, score, gains


DEFAULT_OPTION_MATCH_THRESHOLD = 0.55


def _lcs_length(a: str, b: str) -> int:
    """最长公共子序列长度（动态规划 + 滚动数组）。"""
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur.append(prev[j - 1] + 1)
            else:
                cur.append(max(prev[j], cur[-1]))
        prev = cur
    return prev[-1]


def text_similarity(lib_text: str, ocr_text: str) -> float:
    """题库里是选项简称、界面上是完整句，衡量两者有多像。

    以**题库简称的长度**为基准计算最长公共子序列占比：
    界面文案常比简称长得多（如「偷走坐骑」vs「偷偷靠近偷走圣诞老公公的坐骑」），
    直接用序列相似度会被长度差稀释，LCS 占比更能反映"简称是否出现在界面里"。
    """
    a = (lib_text or "").strip()
    b = (ocr_text or "").strip()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return _lcs_length(a, b) / len(a)


def match_event_by_options(
    events: Mapping[str, Mapping[str, Any]],
    option_texts: Sequence[str],
    threshold: float = DEFAULT_OPTION_MATCH_THRESHOLD,
) -> Optional[str]:
    """按 4 行选项文本反查事件。

    界面选项顺序与题库顺序一致（已用「危险任务」验证），故逐行配对求平均相似度，
    取最高分的事件；低于阈值则返回 None（交给采集分支）。
    """
    if not option_texts or not any(option_texts):
        return None
    scored = []
    for name, body in events.items():
        lib_options = body.get("options") or []
        if len(lib_options) != len(option_texts):
            continue
        total = sum(
            text_similarity(opt.get("text", ""), ocr)
            for opt, ocr in zip(lib_options, option_texts)
        )
        scored.append((total / len(option_texts), name))
    if not scored:
        return None
    best_score, best_name = max(scored)
    return best_name if best_score >= threshold else None


def choose(
    events: Mapping[str, Mapping[str, Any]],
    event_name: str,
    attrs: Mapping[str, int],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    success_bonus: float = DEFAULT_SUCCESS_BONUS,
) -> Decision:
    """返回该事件的最优选项。"""
    if event_name not in events:
        raise KeyError(f"题库中没有事件「{event_name}」")
    opts = get_options(events[event_name])
    if not opts:
        raise ValueError(f"事件「{event_name}」没有任何选项")

    scored = []
    for opt in opts:
        ok, score, gains = evaluate(opt, attrs, weights, success_bonus)
        scored.append((opt, ok, score, gains))

    # 排序键：先「能否成功」，再评分（都失败时评分即"损失最小"）
    best = max(scored, key=lambda x: (x[1], x[2]))
    opt, ok, score, gains = best

    if ok:
        reason = "满足门槛，必定成功" if opt.require else "无要求，必定成功"
        if any(v < 0 for v in gains.values()):
            reason += "；成功收益含负项，但在可成功项中评分最高"
    else:
        short = []
        for k, v in (opt.require or {}).items():
            short.append(f"{k}需{v}（当前{attrs.get(k, 0)}）")
        reason = "所有选项都不满足门槛，此为该情况下损失最小项：" + "、".join(short)

    return Decision(
        event=event_name,
        index=opt.index,
        text=opt.text,
        will_succeed=ok,
        gains=gains,
        score=score,
        reason=reason,
        all_scores=tuple((o.index, o.text, ok_, sc) for o, ok_, sc, _ in scored),
    )


if __name__ == "__main__":  # 自测：不依赖 maa，可直接 `py agent/adventure_logic.py`
    events = load_events()
    print(f"题库事件数：{len(events)}")
    total_opts = sum(len(e["options"]) for e in events.values())
    print(f"选项总数：{total_opts}")
    uncertain = [
        (name, o["text"])
        for name, e in events.items()
        for o in e["options"]
        if o.get("_uncertain")
    ]
    print(f"待核对条目：{len(uncertain)} -> {uncertain}")
    print()

    scenarios = [
        ("初始属性（各 3）", dict.fromkeys(ATTRS, 3)),
        ("均衡成长（各 6）", dict.fromkeys(ATTRS, 6)),
        ("高战力（战力10 其余5）", {**dict.fromkeys(ATTRS, 5), "战力": 10}),
        ("残血（健康1 其余8）", {**dict.fromkeys(ATTRS, 8), "健康": 1}),
    ]
    for label, attrs in scenarios:
        print(f"=== {label}：{attrs} ===")
        for name in ("危险任务", "烤机外挂", "螺旋地狱", "金之祖巫", "木之祖巫"):
            d = choose(events, name, attrs)
            print("  " + d.describe())
        print()

    print("=== 全事件决策（属性各 6，检查是否有异常）===")
    attrs = dict.fromkeys(ATTRS, 6)
    for name in events:
        d = choose(events, name, attrs)
        flag = " ⚠待核对" if any(o["text"] == d.text and o.get("_uncertain") for o in events[name]["options"]) else ""
        print(f"  {d.describe()}{flag}")
