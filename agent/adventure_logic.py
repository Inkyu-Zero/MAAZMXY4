# -*- coding: utf-8 -*-
#
# 造梦西游4 · 生存大冒险 自动答题
# 作者：InkyuZero   (https://github.com/Inkyu-Zero/MAAZMXY4)
#
# 本项目基于 MaaFramework 开发，完全免费。
# 如果你是付费获得的，请及时申请退款 —— 本项目从未收费。
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
import gzip
import hashlib
import json
import math
import os
import pickle
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

ATTRS: Sequence[str] = ("战力", "智慧", "魅力", "灵巧", "运气", "健康")

# 七个可选角色（每局随机一位）及其初始六维，顺序同 ATTRS。
# 来源：debug/screencap 里「角色」行逐帧像素聚类（7 组，见 adventure_ui.json 的 character_roi）
#       + 开局面板逐格读出，并用 debug/adventure/adventure.log 的 24 局判定反推交叉验证
#       （7 个向量能复现 98% 的「必定成功/必然失败」判定）。
# 用途：开局先认角色，就能拿到**权威**的初始六维 —— 三级属性 OCR 全失败且账本为空时
#       （实测确实发生过），原来会退化成 attrs={}，整个价值函数的地基就错了。
CHARACTERS: Dict[str, Dict[str, int]] = {
    "小沙": {"战力": 3, "智慧": 4, "魅力": 3, "灵巧": 6, "运气": 3, "健康": 3},
    "小吒": {"战力": 4, "智慧": 4, "魅力": 4, "灵巧": 4, "运气": 3, "健康": 5},
    "小唐": {"战力": 4, "智慧": 5, "魅力": 5, "灵巧": 3, "运气": 4, "健康": 3},
    "小猪": {"战力": 4, "智慧": 3, "魅力": 3, "灵巧": 3, "运气": 3, "健康": 6},
    "小玉": {"战力": 3, "智慧": 4, "魅力": 5, "灵巧": 6, "运气": 3, "健康": 3},
    "小猴": {"战力": 5, "智慧": 4, "魅力": 3, "灵巧": 4, "运气": 4, "健康": 4},
    "小白": {"战力": 3, "智慧": 4, "魅力": 4, "灵巧": 4, "运气": 5, "健康": 4},
}

# 角色名只有 7 个、都是两个字且第二个字互不相同（沙/吒/唐/猪/玉/猴/白），
# 所以「末字」就是唯一键 —— 形近字 OCR 认错第一个字不影响判定。
_CHAR_TAIL: Dict[str, str] = {name[-1]: name for name in CHARACTERS}

# 实测见过的形近误认（我自己的转写和 OCR 都出现过）：小圤→小吒、小塘→小唐、小砂→小沙。
_CHAR_ALIAS: Dict[str, str] = {
    "小圤": "小吒", "小叱": "小吒", "小咤": "小吒",
    "小塘": "小唐", "小糖": "小唐",
    "小砂": "小沙", "小纱": "小沙",
    "小诸": "小猪", "小朱": "小猪",
    "小候": "小猴", "小侯": "小猴",
    "小王": "小玉", "小五": "小玉",
}


def character_attrs(text: str) -> Optional[Dict[str, int]]:
    """把 OCR 到的「角色」文本映射成初始六维；认不出来返回 None。

    三层判定，越靠前越可信：
      1. 去除非汉字后正好是角色名（或整个文本包含某个角色名）
      2. 已知形近误认别名
      3. 末字唯一命中（末字表 7 个字互不相同，所以这一层也很硬）
    认不准就返回 None，让调用方退回属性 OCR —— 宁可不用也不要猜错角色，
    因为角色一错，整局的属性基线就全错了。
    """
    if not text:
        return None
    chars = "".join(c for c in str(text) if "\u4e00" <= c <= "\u9fff")
    if not chars:
        return None
    for name in CHARACTERS:
        if name in chars:
            return dict(CHARACTERS[name])
    if chars in _CHAR_ALIAS:
        return dict(CHARACTERS[_CHAR_ALIAS[chars]])
    hit = _CHAR_TAIL.get(chars[-1])
    if hit:
        return dict(CHARACTERS[hit])
    return None

# 与 adventure.py 的 UI_PATH 同理：assets/ 不进发布包，题库放 agent/data 里跟着 agent 走。
_DEFAULT_DATA = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "adventure.json"
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
# 注意健康：它的门槛只有 3/5/6 三处，target 取 3。
# 实测（1500 局成对比较，见 tools/simulate_adventure.py）：target 从 6 降到 3，
# 平均分 78.44 → 80.11。原因是 target=6 时健康在 3~5 区间每 +1 都落进 NEAR 档，
# 拿 6.0×1.5=9.0 分，压过了堆战力/智慧/灵巧的收益；而健康真正的风险只有
# 「归零提前结束」，那件事由 ZERO_PENALTY 单独兜底。
THRESHOLD_TARGETS: Dict[str, int] = {
    "战力": 10,
    "灵巧": 10,
    "魅力": 10,
    "智慧": 10,
    "运气": 9,
    "健康": 3,
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
    value: float = 0.0
    all_scores: Sequence[Tuple[int, str, bool, float]] = field(default_factory=tuple)
    exact: bool = False
    # 实际用的规划方式："seq"=已知题序的前向规划，"stage"=按阶段均匀抽的
    # 后向归纳，""=启发式。日志按它打标签，避免把两条路都叫「精确」。
    plan: str = ""

    def describe(self) -> str:
        tag = {
            "seq": "［精确·已知题序］",
            "stage": "［精确·按阶段］",
        }.get(self.plan, "")
        return (
            f"[{self.event}] 选第 {self.index + 1} 项「{self.text}」"
            f"（{'必定成功' if self.will_succeed else '必然失败'}，"
            f"属性变化 {self.gains or '无'}，评分 {self.score:+.2f}，"
            f"后续期望 {self.value:+.2f}）{tag}"
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


# --------------------------------------------------------------------------- #
# 价值函数：把「该堆哪项属性」从阈值分档升级为「后续关键阶段的期望得分」
# --------------------------------------------------------------------------- #
# 决策的真实处境：每阶段的事件是从该阶段题库里**随机抽**的（同一事件可能重复
# 出现，日志里 唐僧受伤 一局内出现过两次），所以本项目无法预知这一局会碰到哪些
# 事件，只能按题库分布做期望最优。
#
# 原做法（_gain_score 的阈值分档）在 target=10 时所有属性 gap 都 > MID_GAP，
# 权重全部落进 FAR_WEIGHT=1.0，退化成「谁的总增益数值大」，与"能不能过关"脱钩。
#
# 现做法：直接估算「带着当前属性去打后面几个关键阶段，期望能拿多少分」。
# 因为过关是硬门槛，这个期望天然奖励「把某项属性顶过门槛」，也天然抑制
# 「每项都涨一点、结果一项都过不了」。
#
# 量级参考（4000 条相同题序成对比较，见 tools/simulate_adventure.py、
# tools/adventure_upper_bound.py）：
#   改造前 78.59  →  当前 80.57  →  理论最优（不知题序）81.33
#   →  上帝视角（预知题序）87.19 —— 后者不可达。
# 满分率 5.25% → 7.28%；阶段 4/5 逐题成功率 Q7 37→43% Q8 38→46% Q9 40→46%
# Q10 16→20%。

# 参与估值的关键阶段：3+4+5 合计 82 分（满分 100），是分数的决定性来源。
# 包含阶段 3（不只是 4+5）是为了让「前 6 题该往哪个属性投资」更早拿到信号，
# 成对比较实测优于只用 4+5（+0.49 vs +0.17）。
VALUE_STAGES: Sequence[int] = (3, 4, 5)

# 价值项与原评分的混合比例。价值项量级在几十、原评分在个位到十几。
#
# 定值过程（成对比较：同一批题序跑两种参数，比配对差与 t 值）：
#   阶段 3+4+5 上扫描，4000 条 × 2 个独立池 → blend 0.25 最好（+0.49 / +0.44）；
#   但那批池是用**单一初始六维（小玉）**跑的。换成真实初始分布（7 个角色等权）
#   重新扫，0.25 反而偏高：0.05~0.20 整片高原都优于它，最终在 4 个**全新**池上
#   定案 0.125（+0.100 / +0.080 / +0.101 / +0.055，合并 +0.084，t = +4.38）。
# 教训：用一组初始值调出来的参数会过拟合那一组；真实角色强弱差很多
# （理论上界从 66.2 到 83.3），必须在真实角色分布上重调。
VALUE_BLEND = 0.125

# 软边距尺度：把「还差几点达标」换成 sigmoid((属性 - 门槛) / VALUE_SCALE)。
#
# 为什么不能用 0/1 硬判定：过关确实是硬门槛，但**决策需要梯度**。硬判定下
# 「灵巧 7 → 8」这类变化只要没跨过门槛就完全不改变价值，启发式因此看不见
# 「再堆 1 点灵巧就能吃下阶段 4 的两个 12 分」，也就不会集中投资——实测个案里
# 这正是后 4 题全灭的原因。换成软边距后任何位置的属性增长都有梯度。
#
# 0.7 由真实初始分布上的 4 个全新池定案（+0.100 / +0.080 / +0.101 / +0.055）；
# 在旧的单角色池上选出的是 1.0 —— 同样是过拟合单角色的表现。
VALUE_SCALE = 0.7

# 无门槛选项在软边距里的「超标余量」，等效于「肯定能过」。
VALUE_FREE_MARGIN = 8.0

# 生存因子：健康 >= 该值时不打折，只剩更少血时价值按比例缩水。
#
# 为什么要单独处理健康：健康见底会让剩余题目**全部作废**（丢分最狠的一种失败），
# 但纯期望分对「健康从 3 掉到 2」毫无感觉（未来没有任何选项要求健康 >= 4）。
# 打折后决策愿意为「别掉到 1 点血」付一点代价。
#
# 实测（4000 局成对比较 × 2 个独立题序池）：
#   关闭        池A 80.482 / 池B 80.504，第 9 题前早死 186 / 178 局
#   2.0         池A 80.573 / 池B 80.588，早死 100 / 101 局（+0.091 / +0.083）
#   3.0         池A 80.251 / 池B 80.275，早死 71 / 75 局（-0.231 / -0.229）
# 3.0 虽然早死更少，但过度保守反而丢分，故取 2.0。
VALUE_HEALTH_FULL = 2.0

_stage_meta: Dict[int, Dict[str, Any]] = {}
_stage_meta_ready = False
_stage_entries: Dict[int, list] = {}
_stage_questions: Dict[int, int] = {}
_stage_scores: Dict[int, Tuple[int, int]] = {}
_value_cache: Dict[Tuple[int, ...], float] = {}
_value_ready = False
_value_src: Optional[int] = None


def _ensure_stage_meta() -> None:
    """从题库 `_meta.stages` 读每个阶段的题数与成败得分（只读一次）。"""
    global _stage_meta_ready
    if _stage_meta_ready:
        return
    for s in (load_data().get("_meta") or {}).get("stages") or []:
        _stage_meta[int(s["stage"])] = s
    _stage_meta_ready = True


def _ensure_value_index(events: Mapping[str, Mapping[str, Any]]) -> None:
    """按传入的 events 建「阶段 → 事件与选项 / 题数 / 分数」索引。

    用 `id(events)` 做缓存键：调用方换了题库（换文件或换字典）会自动重建，
    而正常运行时每道题都命中缓存，不会重复解析。
    """
    global _value_ready, _value_src
    key = id(events)
    if _value_ready and _value_src == key:
        return
    _ensure_stage_meta()
    _stage_entries.clear()
    _stage_questions.clear()
    _stage_scores.clear()
    _value_cache.clear()
    for st, s in _stage_meta.items():
        _stage_questions[st] = len(s.get("questions") or [])
        _stage_scores[st] = (
            int(s.get("score_success", 0)),
            int(s.get("score_fail", 0)),
        )
    for name, body in events.items():
        st = int(body.get("stage") or 0)
        _stage_entries.setdefault(st, []).append((name, get_options(body)))
    _value_ready = True
    _value_src = key


def apply_delta(attrs: Mapping[str, int], delta: Mapping[str, int]) -> Dict[str, int]:
    """返回施加属性变化后的新属性字典。"""
    out = dict(attrs)
    for k, v in (delta or {}).items():
        out[k] = int(out.get(k, 0)) + int(v)
    return out


def future_value(
    attrs: Mapping[str, int], events: Mapping[str, Mapping[str, Any]]
) -> float:
    """按当前属性估算「后面几个关键阶段的期望得分」。

    对每个事件取「4 个选项里最可能通过的那一个」，通过概率用软边距
    (sigmoid) 估计而不是 0/1 硬判定；再对该阶段题库求平均。

    为什么要软边距：过关本身是硬门槛，但**决策需要梯度**。硬判定下
    「灵巧 7 → 8」只要没跨过门槛就完全不改变价值，启发式因此看不见
    「再堆 1 点灵巧就能吃下阶段 4 的两个 12 分」这件事，于是不会集中投资。
    """
    _ensure_value_index(events)
    key = tuple(int(attrs.get(a, 0)) for a in ATTRS)
    got = _value_cache.get(key)
    if got is not None:
        return got
    if key[ATTRS.index("健康")] <= 0:
        _value_cache[key] = 0.0
        return 0.0
    total = 0.0
    for st in VALUE_STAGES:
        entries = _stage_entries.get(st) or []
        if not entries:
            continue
        win, lose = _stage_scores.get(st, (0, 0))
        acc = 0.0
        for _name, opts in entries:
            best_p = 0.0
            for o in opts:
                if o.always_fail:
                    continue
                req = o.require or {}
                if not req:
                    margin = VALUE_FREE_MARGIN
                else:
                    margin = min(
                        float(attrs.get(k, 0)) - float(v) for k, v in req.items()
                    )
                p = 1.0 / (1.0 + math.exp(-margin / VALUE_SCALE))
                if p > best_p:
                    best_p = p
            acc += win * best_p + lose * (1.0 - best_p)
        total += acc / len(entries) * _stage_questions.get(st, 1)
    if VALUE_HEALTH_FULL > 0:
        total *= min(1.0, int(attrs.get("健康", 0)) / VALUE_HEALTH_FULL)
    _value_cache[key] = total
    return total


# --------------------------------------------------------------------------- #
# 精确后向归纳：真正的最优策略
# --------------------------------------------------------------------------- #
# 上面的 future_value 是**启发式**：它只看剩余阶段的题库分布，不展开后面的分支。
# 真正的优解可以精确算出来 —— 记忆化后向归纳：
#
#     V(q, attrs) = E_{事件 e ~ 第 q 题所在阶段的题库} [ max_{选项 o}
#                     ( 本题得分(o 是否通过) + V(q+1, attrs ⊕ Δ(o)) ) ]
#
# 出题就是从阶段题库里抽，所以这个期望正是「不知道本局题序」前提下的最优值，
# 也就是 tools/adventure_upper_bound.py 算的那个上界；直接对每个选项取 argmax
# 就是最优策略。
#
# 代价（单角色，实测 tools/_report_final.py 的「建表成本」一节）：
#   把阶段 1 的 4 个事件各决策一次（比真局更狠）最坏 77 万状态 / 27 秒；
#   真局首题只抽到其中 1 个事件，所以线上首题通常 10~20 秒，
#   之后每道题都命中缓存，增量几乎为零。
#   为了不每局都重付这笔钱，表**跨局保留**，只在状态数超过
#   EXACT_RESET_THRESHOLD 时才清空（见 maybe_reset_exact_memo）——
#   同一次会话里第 2 局起基本不额外花钱。
#   （pipeline 的「生存大冒险_读题」节点因此把 timeout 放宽到 90 秒，
#   默认 20 秒在慢机器上会因为识别超时直接走 on_error 结束整局。）
# 收益（4 个全新题序池 × 400 条 × 7 个真实角色 = 11,200 对，成对比较）：
#   改造前 73.21 → 启发式 75.00 → 精确 75.88；理论上界（不知题序）76.32。
#   即 +2.67（t = +32.1），把可抢的 +3.11 里拿掉了 86%。
# 同一批数据里调参最多只值 +0.08，所以这里直接把启发式换成精确策略；
# 启发式保留为「题库元信息不完整 / 状态数异常」时的兜底。
EXACT_ENABLED = True

# 状态数硬上限。实测（tools/measure_table_warm.py）：
#   有放回：单角色单事件（= 线上首题）最坏约 38~48 万状态，10~20 秒；
#           7 个角色的并集约 112 万~150 万状态。
#   阶段内不放回：单角色 85~149 万状态、40~81 秒；7 角色并集 600~1000 万。
# 所以接入不放回之后不能再整表常驻 7 个角色，改成**按角色清表**
# （见 maybe_reset_exact_memo），上限 180 万仍按「单角色」定。
# 内存代价约 150 MB（CPython 每条约 100 B）。
EXACT_MAX_STATES = 1_800_000

# 到上限时在半数处裁剪，而不是把精确策略整体关掉（旧行为）。
# 归纳表是**纯缓存**：丢条目只会重算，不会改变任何判定结果。
_EXACT_MAX_TRIMS = 3  # 单次决策内最多裁几次；再多说明工作集本身就超上限，只能降级

_EXACT_BITS = 7  # 每个属性占 7 bit：健康上限 99、其余属性 <=10，7 bit 足够且键更短
_EXACT_QSHIFT = _EXACT_BITS * len(ATTRS)  # 题号必须整体挪到 6 个属性字段之上

# 「阶段内不放回」的 used 掩码宽度。取 16 位够装最大池（stage3 有 15 个事件）。
# 状态键 = (q << (QSHIFT + MSHIFT)) | (pack(attrs) << MSHIFT) | used
_EXACT_MSHIFT = 16

# 哪些阶段按「不放回」建模。默认关闭，可用环境变量覆盖（调试用）。
# 为什么不默认开：不放回让状态数涨到 3 倍、单角色建表 20 秒 -> 75 秒，
# 而它只值 +0.26 分，且这张表只在「读不到题序」时才会用到。
#   ADV_NOREP_STAGES=4        只对 stage4
#   ADV_NOREP_STAGES=         全部关闭（等价于旧的有放回行为）
#   默认 1,2,3,4,5 —— 全阶段不放回（游戏内同阶段不重复出题）。
#   代价是一次性建表（单角色 75~163 万状态、34~80 秒），
#   已由 debug/exact_cache 的磁盘缓存吸收，第二次起 0.2 秒。
EXACT_NOREP_STAGES = frozenset(
    int(x) for x in os.environ.get("ADV_NOREP_STAGES", "1,2,3,4,5").split(",") if x.strip()
)

_exact_memo: Dict[int, float] = {}
_exact_src: Optional[int] = None
_exact_event_slot: Dict[str, int] = {}  # 事件名 → 它在所属阶段池子里的槽位（0 基）
_exact_character: str = ""  # 当前归纳表对应的角色（换角色才清表）
_exact_qstage: list = []  # 题号（0 基）→ 阶段
_exact_stages: list = []  # 参与归纳的阶段（按题号顺序去重）
_exact_compiled: Dict[int, list] = {}  # 阶段 → 该阶段每个事件的编译选项表
_exact_event_opts: Dict[str, list] = {}  # 事件名 → 编译后的选项表
_exact_event_stage: Dict[str, int] = {}  # 事件名 → 所属阶段（用于反推题号）
_exact_stage_qs: Dict[int, list] = {}  # 阶段 → 该阶段的题号列表（0 基）
_exact_cap: list = []  # 属性截断上限（与参考实现 adventure_upper_bound.CAP 同步）
_exact_ai: Dict[str, int] = {}
_exact_hidx = ATTRS.index("健康")
_exact_states = 0
_exact_trims = 0
_exact_off_reason: Optional[str] = None
_exact_fp: str = ""  # 题库内容指纹（缓存键的一部分）

# 归纳表磁盘缓存：建表是纯计算，同一份题库 + 同一角色结果永远一样，
# 进程一退就丢太浪费（再开又要重算 74~80 秒），所以落盘复用。
# 只缓存记忆化表本身，阶段/选项结构每次照常重建（很便宜），
# 题库一改指纹就变、旧缓存自动失效。
_EXACT_CACHE_VER = 3
_exact_cache_state: Dict[str, Any] = {"tried": False, "loaded": False, "saved": False}


class _ExactOverflow(Exception):
    """状态数超过上限（正常不可达，纯防呆）。"""


def _exact_disable(reason: str) -> None:
    global EXACT_ENABLED, _exact_off_reason
    EXACT_ENABLED = False
    _exact_off_reason = reason
    _exact_memo.clear()


def _fingerprint(events: Mapping[str, Mapping[str, Any]]) -> str:
    """题库内容指纹：事件名 + 选项 + 阶段元信息，变化即让缓存失效。"""
    try:
        blob = json.dumps({"stages": _stage_meta, "events": events},
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _exact_cache_key() -> str:
    return "|".join([str(_EXACT_CACHE_VER), _exact_fp, _exact_character or "default",
                     ",".join(str(s) for s in sorted(EXACT_NOREP_STAGES))])


def _exact_cache_file() -> Optional[str]:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, "debug", "exact_cache")
        os.makedirs(d, exist_ok=True)
        tag = "".join(str(s) for s in sorted(EXACT_NOREP_STAGES)) or "off"
        return os.path.join(d, "%s_%s_n%s.pk.gz"
                            % (_exact_character or "default", _exact_fp, tag))
    except Exception:
        return None


def _announce_first_build(reason: str) -> None:
    """首次给某个角色铺归纳表时先吭一声。

    铺表要 30~80 秒，这期间 agent 一行日志都不会再打，界面上看着就像卡死
    （用户反馈过「没反应了」）。缓存是按角色分别落盘的，所以每个角色第一次
    跑都会走到这里；第二个角色起照样提示，只是各自的缓存文件不同。
    """
    print("首次构建「%s」的归纳表（%s）：约 75~163 万状态，"
          "视机器需 30~80 秒，期间界面不会有任何反应，请稍候……"
          % (_exact_character or "当前角色", reason),
          flush=True)


def _exact_cache_load() -> bool:
    """尝试从磁盘恢复记忆化表，命中返回 True（同时把状态数补回来）。"""
    global _exact_states
    st = _exact_cache_state
    if st["tried"]:
        return st["loaded"]
    st["tried"] = True
    if not EXACT_ENABLED or not _exact_fp:
        return False
    path = _exact_cache_file()
    if not path or not os.path.exists(path):
        _announce_first_build("还没有缓存文件")
        return False
    try:
        t0 = time.time()
        with gzip.open(path, "rb") as fp:
            head, memo, states = pickle.load(fp)
        if head != _exact_cache_key():
            _announce_first_build("缓存参数不匹配，需要重算")
            return False
        _exact_memo.clear()
        _exact_memo.update(memo)
        _exact_states = int(states)
        st["loaded"] = True
        print("归纳表缓存命中：%s（%s 状态，载入 %.1fs）"
              % (os.path.basename(path), format(len(memo), ","), time.time() - t0),
              flush=True)
        return True
    except Exception:
        _announce_first_build("缓存读取失败，需要重算")
        return False


def _exact_cache_maybe_save() -> None:
    """首次铺完表后落盘一次（半成品不写）。"""
    st = _exact_cache_state
    if st["saved"] or st["loaded"] or len(_exact_memo) < 20000:
        return
    st["saved"] = True
    path = _exact_cache_file()
    if not path:
        return
    try:
        t0 = time.time()
        tmp = path + ".tmp"
        with gzip.open(tmp, "wb", compresslevel=1) as fp:
            pickle.dump((_exact_cache_key(), dict(_exact_memo), _exact_states), fp,
                        protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
        print("归纳表已缓存：%s（%s 状态，写入 %.1fs）"
              % (os.path.basename(path), format(len(_exact_memo), ","), time.time() - t0),
              flush=True)
    except Exception:
        pass


def reset_exact_memo() -> None:
    """强制清空记忆化表（测试与调试用）。"""
    global _exact_states, _exact_trims
    _exact_memo.clear()
    _exact_states = 0
    _exact_trims = 0
    _exact_cache_state.update(tried=False, loaded=False, saved=False)


def _trim_exact_memo() -> None:
    """缓存到上限时丢掉最早插入的一半。

    表是纯缓存，丢条目只会让后续重算，**不会改变任何判定结果** ——
    所以这里选择「重算」而不是旧实现的「把精确策略永久关掉」
    （旧行为代价 1.05 分/局，而且是静默的）。
    """
    global _exact_states, _exact_trims
    n = len(_exact_memo)
    if not n:
        return
    for k in list(_exact_memo)[: n // 2]:
        del _exact_memo[k]
    _exact_states = len(_exact_memo)
    _exact_trims += 1


def maybe_reset_exact_memo(character: Optional[str] = None) -> bool:
    """换角色时清表；同一角色连续多局**复用**，返回是否真的清了。

    为什么要按角色清：接入「阶段内不放回」后，单个角色的状态数从 30~59 万涨到
    75~163 万（实测小玉 1,312,272 / 73.8 秒）（used 掩码把同阶段的每个排列都拆开），7 个角色的并集会到 600~1000
    万，远超 EXACT_MAX_STATES。按角色清表后单角色 ≤149 万，正好装得下上限。

    角色是「开局 OCR 识别 + 六维校验」得到的（见 agent/adventure.py 的
    _opening_attrs），传入空值表示这一局没能识别出角色 —— 此时**不清表**，
    宁可多占一点内存，也不要因为识别失败而白丢已经建好的表。
    """
    global _exact_character
    c = str(character or "").strip()
    if not c:
        return False
    if _exact_character == c:
        return False
    had = len(_exact_memo)
    _exact_character = c
    if not had:
        return False
    reset_exact_memo()
    return True


def exact_stats() -> Tuple[int, int]:
    """返回 (记忆化状态数, 已展开节点数)，供日志使用。"""
    return len(_exact_memo), _exact_states


def ensure_exact_index(events: Mapping[str, Mapping[str, Any]]) -> bool:
    """确保题库索引（题号→阶段、事件→阶段）已建好。

    `question_for_event()` 依赖 `_exact_event_stage` / `_exact_stage_qs`。
    第 1 题时索引还没被任何一次 `choose()` 建过，直接调 question_for_event
    会拿到 None：题号退化成兜底值，事件也登记不进 answered —— 结算时
    「上帝视角」就只看到 9 题、给不出最优对比。所以在推导题号前先确保建好。
    """
    return _build_exact_tables(events)


def question_for_event(event: str, answered: Dict[int, list]) -> Optional[int]:
    """由「这一题是什么事件」反推它是本局第几题（0 基），推不出返回 None。

    题号**不能**用「决策次数」推。点击有时落不到新题上（截图还是上一题），
    同一题于是被再答一次，决策次数就比真实进度多；一旦多算，
    之后每一题的价值评估都会偏后一个阶段（实测第 1 题后多一次就掉 2.0 分）。
    而事件名唯一确定了阶段，同一阶段里的题号是连续的、事件不会重复抽到，
    所以「阶段 + 该阶段内已答过几个不同事件」就是可靠题号。

    answered 由调用方（agent 的局内状态）持有，形如 {阶段: [事件名, ...]}；
    本函数会就地把新事件登记进去。事件已在列表里 = 同一题被重复读取，
    返回同一题号、不推进。
    """
    st = _exact_event_stage.get(event)
    if st is None:
        return None
    qs = _exact_stage_qs.get(st) or []
    if not qs:
        return None
    seen = answered.setdefault(int(st), [])
    if event in seen:
        idx = seen.index(event)
    else:
        idx = len(seen)
        seen.append(event)
    return qs[min(idx, len(qs) - 1)]


def question_stage(question: int) -> Optional[int]:
    """题号（0 基）→ 阶段号。题库元信息缺失时返回 None。

    只用 `_meta.stages[].questions`，不依赖归纳表 —— 启发式模式下也要能算分。
    """
    _ensure_stage_meta()
    for st in sorted(_stage_meta):
        for num in _stage_meta[st].get("questions") or []:
            if int(num) - 1 == question:
                return int(st)
    return None


def question_score(question: int, success: bool) -> int:
    """按题号（0 基）返回「本题得分」。成功取 score_success，失败取 score_fail。

    分数是题库 `_meta.stages` 里的固定值（6/3、9/4、12/6、19/9），
    与实际结算逐分吻合 —— `tools/adventure_predict_vs_real.py` 验证过 11/12 局偏差 0。
    """
    st = question_stage(question)
    if st is None:
        return 0
    s = _stage_meta.get(st) or {}
    return int(s.get("score_success" if success else "score_fail", 0) or 0)


def _exact_pairs(delta: Optional[Mapping[str, int]]) -> tuple:
    """把属性增减字典编译成 ((属性下标, 整数增量), ...)，省掉归纳内层的字典开销。"""
    if not delta:
        return ()
    out = []
    for k, v in delta.items():
        i = _exact_ai.get(k)
        if i is None or not v:
            continue
        out.append((i, int(v)))
    return tuple(out)


def _build_exact_tables(events: Mapping[str, Mapping[str, Any]]) -> bool:
    """按题库建「题号→阶段、阶段→编译后的选项」索引（只建一次）。"""
    global _exact_src, _exact_qstage, _exact_stages, _exact_compiled, _exact_cap, _exact_ai
    global _exact_event_opts
    global _exact_event_stage, _exact_stage_qs, _exact_event_slot, _exact_fp
    if not EXACT_ENABLED:
        return False
    # 用题库「内容指纹」判断要不要重建，**不能**用 id(events)：调用方每次
    # 都是新读一份 dict，id() 每次都不同，于是每题都被当成「换了题库」——
    # 整张索引重建、记忆化表被 reset，下一帧再从磁盘把 122 万状态读回来
    # （实测每答一题都打一次「归纳表缓存命中…载入 0.6s」，每题白花 0.6 秒）。
    # 注意：先把阶段元信息补齐再算指纹 —— 指纹里含 _stage_meta，第一次调用时
    # 它还是空的，算出来的指纹和第二次不一样，缓存就永远命不中。
    _ensure_value_index(events)
    _ensure_stage_meta()
    fp = _fingerprint(events)
    key = fp or "id:%d" % id(events)
    if _exact_src == key and _exact_qstage:
        return True
    _exact_fp = fp
    _exact_ai = {a: i for i, a in enumerate(ATTRS)}

    # 题号 → 阶段：题库 `_meta.stages[].questions` 直接给出了题号列表（1 基）
    qstage: list = []
    for st in sorted(_stage_meta):
        for num in _stage_meta[st].get("questions") or []:
            n = int(num) - 1
            while len(qstage) <= n:
                qstage.append(None)
            qstage[n] = st
    if not qstage or any(s is None for s in qstage):
        _exact_disable("题库 _meta.stages 的题号不完整")
        return False

    compiled: Dict[int, list] = {}
    event_opts: Dict[str, list] = {}
    event_slot: Dict[str, int] = {}
    for st, entries in _stage_entries.items():
        pool = []
        for name, opts in entries:
            one = [
                (
                    o.index,
                    _exact_pairs(o.require),
                    _exact_pairs(o.success),
                    _exact_pairs(o.fail),
                    bool(o.always_fail),
                )
                for o in opts
            ]
            # 槽位 = 该事件在本阶段池子里的下标，用来做「不放回」的 used 位掩码
            event_slot[name] = len(pool)
            pool.append(one)
            event_opts[name] = one
        if pool:
            compiled[st] = pool
    if not compiled:
        _exact_disable("题库里没有可归纳的事件")
        return False

    # 属性截断上限：超过「该属性在题库里的最高门槛」后不再改变任何判定，
    # 因此可以截断——这是把状态数从 65 万压到 53 万的关键。健康不截断
    # （它没有门槛意义上的天花板，且负增益会一路累积）。
    #
    # 注意这与 `tools/adventure_upper_bound.py` 的 `add()` 必须完全一致：
    # 生产实现的每一步都要和量出收益的那份参考实现逐决策对齐，否则
    # 「调参得到的 +0.xx」根本不能保证会在线下复现。曾经漏掉这层截断，
    # 结果同一局面算出 78.88 而参考是 78.83，299 个决策里 136 个对不上。
    cap = []
    for a in ATTRS:
        mx = 0
        for pool in compiled.values():
            for _i, req, _s, _f, _af in (o for ev in pool for o in ev):
                for i, v in req:
                    if ATTRS[i] == a:
                        mx = max(mx, v)
        cap.append(mx if a != "健康" else 99)

    _exact_qstage = qstage
    _exact_stages = sorted({s for s in qstage})
    _exact_compiled = compiled
    _exact_event_opts = event_opts
    _exact_event_slot = event_slot
    _exact_stage_qs = {st: [n for n, s in enumerate(qstage) if s == st] for st in set(qstage)}
    _exact_event_stage = {
        name: st for st, entries in _stage_entries.items() for name, _opts in entries
    }
    _exact_cap = cap
    _exact_src = key
    reset_exact_memo()
    return True


def _pack(attrs: Tuple[int, ...]) -> int:
    """把 6 维属性打包成一个整数（热路径，按 ATTRS 长度固定为 6 展开）。"""
    a, b, c, d, e, f = attrs
    B = _EXACT_BITS
    M = (1 << B) - 1
    return (
        (int(a) & M)
        | ((int(b) & M) << B)
        | ((int(c) & M) << (2 * B))
        | ((int(d) & M) << (3 * B))
        | ((int(e) & M) << (4 * B))
        | ((int(f) & M) << (5 * B))
    )


def _eval_options(
    opts: list,
    attrs: Tuple[int, ...],
    q: int,
    win: int,
    lose: int,
    used: int = 0,
    ev_slot: int = -1,
) -> Tuple[Optional[int], float]:
    """在一组（编译后的）选项里按精确期望取最优，返回 (选项下标, 期望值)。

    used / ev_slot 服务于「阶段内不放回」：把本题抽到的那个事件记进掩码，
    下一层就只在「本阶段还没抽走的事件」上取平均（见 _exact_value）。
    跨阶段时掩码清零 —— 不同阶段是各自独立的池子，互不排斥。
    """
    best_i, best_v = None, -1e18
    nq = q + 1
    st = _exact_qstage[q] if 0 <= q < len(_exact_qstage) else None
    if (
        ev_slot >= 0
        and st is not None
        and st in EXACT_NOREP_STAGES
        and 0 <= nq < len(_exact_qstage)
        and _exact_qstage[nq] == st
    ):
        nxt_used = used | (1 << ev_slot)
    else:
        nxt_used = 0
    for idx, req, succ, fail, af in opts:
        ok = not af
        if ok:
            for i, v in req:
                if attrs[i] < v:
                    ok = False
                    break
        pairs = succ if ok else fail
        if pairs:
            nxt = list(attrs)
            for i, v in pairs:
                nxt[i] = min(nxt[i] + v, _exact_cap[i])
            nxt = tuple(nxt)
        else:
            nxt = attrs
        val = (win if ok else lose) + _exact_value(nq, nxt, nxt_used)
        if val > best_v:
            best_v, best_i = val, idx
    return best_i, (best_v if best_i is not None else 0.0)


def _exact_value(q: int, attrs: Tuple[int, ...], used: int = 0) -> float:
    """从第 q 题（0 基）起、属性为 attrs、本阶段已抽走 used 时的最优期望剩余得分。"""
    global _exact_states
    if q >= len(_exact_qstage) or attrs[_exact_hidx] <= 0:
        return 0.0
    key = (q << (_EXACT_QSHIFT + _EXACT_MSHIFT)) | (_pack(attrs) << _EXACT_MSHIFT) | used
    got = _exact_memo.get(key)
    if got is not None:
        return got
    if _exact_states >= EXACT_MAX_STATES:
        if _exact_trims >= _EXACT_MAX_TRIMS:
            raise _ExactOverflow()
        _trim_exact_memo()
    _exact_states += 1
    st = _exact_qstage[q]
    win, lose = _stage_scores.get(st, (0, 0))
    pools = _exact_compiled[st]
    # 下面这段是 _eval_options 的内联版：这一步是整张表的热路径
    # （单角色 163 万状态 x 池子里 15 个事件 = 2400 万次调用），
    # 省掉函数调用与参数打包实测能省掉三成时间。
    nq = q + 1
    norep_here = st in EXACT_NOREP_STAGES and nq < len(_exact_qstage) and _exact_qstage[nq] == st
    cap = _exact_cap
    tot = 0.0
    cnt = 0
    for slot, opts in enumerate(pools):
        if used & (1 << slot):  # 本阶段已经被抽走过的事件，不会再出现
            continue
        nxt_used = (used | (1 << slot)) if norep_here else 0
        best_v = -1e18
        for _idx, req, succ, fail, af in opts:
            ok = not af
            if ok:
                for i, v in req:
                    if attrs[i] < v:
                        ok = False
                        break
            pairs = succ if ok else fail
            if pairs:
                nxt = list(attrs)
                for i, v in pairs:
                    nv = nxt[i] + v
                    c = cap[i]
                    nxt[i] = nv if nv < c else c
                nxt = tuple(nxt)
            else:
                nxt = attrs
            val = (win if ok else lose) + _exact_value(nq, nxt, nxt_used)
            if val > best_v:
                best_v = val
        tot += best_v if best_v > -1e17 else 0.0
        cnt += 1
    res = (tot / cnt) if cnt else 0.0
    _exact_memo[key] = res
    return res


def exact_choice(
    events: Mapping[str, Mapping[str, Any]],
    event_name: str,
    attrs: Mapping[str, int],
    question: int,
    answered: Optional[Mapping[int, Sequence[str]]] = None,
) -> Optional[Tuple[int, float]]:
    """精确最优选项，返回 (选项下标, 期望剩余得分)；不可用时返回 None。

    answered 形如 {阶段: [本阶段已答过的事件名, ...]}，用来建模「阶段内不放回」：
    已经抽走过的事件不会再出现，所以后续期望只在剩余池子里平均。
    """
    global _exact_trims
    if not _build_exact_tables(events):
        return None
    if not (0 <= question < len(_exact_qstage)):
        return None
    opts = _exact_event_opts.get(event_name)
    if not opts:
        return None
    if _exact_cache_load():
        pass  # 命中：下面的 _eval_options 直接查表，不再枚举
    st = _exact_qstage[question]
    win, lose = _stage_scores.get(st, (0, 0))
    t = tuple(int(attrs.get(a, 0)) for a in ATTRS)
    used = 0
    if answered and st in EXACT_NOREP_STAGES:
        for nm in answered.get(st, ()) or ():
            if nm == event_name:  # 当前这道题还没答完，不算「已抽走」
                continue
            slot = _exact_event_slot.get(nm, -1)
            if slot >= 0:
                used |= 1 << slot
    slot_self = _exact_event_slot.get(event_name, -1)
    _exact_trims = 0  # 单次决策内的裁剪次数预算（见 _trim_exact_memo）
    try:
        res = _eval_options(opts, t, question, win, lose, used, slot_self)
    except _ExactOverflow:
        _exact_disable("状态数超过上限")
        return None
    _exact_cache_maybe_save()
    return res


# --------------------------------------------------------------------------
# 已知题序的精确规划
#
# 如果能提前拿到本局 10 题的确切顺序（例如调用方自行提供，或离线仿真里已知），
# 就不用再按「池子里按阶段均匀抽」算期望，而是沿着这份确定的题序做后向归纳：
#
#     V(q, attrs) = max_o [ 本题得分(o) + V(q+1, attrs + Δo) ]
#
# 收益来自两处：该放弃的题果断放弃（第 7~9 题每题 12 分，门槛够不到时把属性留给
# 后面更划算）；该押的题集中押（第 10 题 19 分，知道它是「水之祖巫」只要战力，
# 前 9 题的资源就该往战力上堆）。
#
# 实测（tools/adventure_seq_value.py，19 局真实对局）：实际均分 74.58，按已知
# 题序重排后 79.21，**+4.63 分/局**；19 局里只有 2 局原本就走对了路线，最好的一
# 局能从 84 分打到 100 分。
#
# 这份 memo 与 _exact_memo（不知题序时的按阶段期望）是两套独立的表；题序变了必须
# 清空，否则会拿旧题序的结论去指导新对局。
# --------------------------------------------------------------------------

_seq_memo: Dict[Any, Tuple[Optional[int], float]] = {}
_SEQ_NAMES_KEY = "_names"


def reset_seq_memo() -> None:
    """清空「已知题序」的规划缓存（每读到一份新题序就调一次）。"""
    _seq_memo.clear()


def seq_stats() -> int:
    """当前「已知题序」缓存里有多少状态（调试/自检用）。"""
    return max(0, len(_seq_memo) - (1 if _SEQ_NAMES_KEY in _seq_memo else 0))


def _seq_value(names: Sequence[str], q: int, attrs: Tuple[int, ...]) -> float:
    """沿确定题序 names，从第 q 题起的最优期望剩余得分。"""
    if q >= len(names) or attrs[_exact_hidx] <= 0:
        return 0.0
    key = (q << _EXACT_QSHIFT) | _pack(attrs)
    got = _seq_memo.get(key)
    if got is not None:
        return got[1]
    opts = _exact_event_opts.get(names[q])
    if not opts:
        # 题序里出现了题库没有的事件：当作 0 分走完，不至于整条路线崩掉
        _seq_memo[key] = (None, _seq_value(names, q + 1, attrs))
        return _seq_memo[key][1]
    st = _exact_qstage[q]
    win, lose = _stage_scores.get(st, (0, 0))
    _eval_options_seq(names, opts, attrs, q, win, lose, key)
    return _seq_memo[key][1]


def _eval_options_seq(
    names: Sequence[str],
    opts: list,
    attrs: Tuple[int, ...],
    q: int,
    win: int,
    lose: int,
    key: Any,
) -> None:
    """枚举本题所有选项，把 (最优下标, 期望值) 写进 _seq_memo[key]。"""
    best_i, best_v = None, -1e18
    for idx, req, succ, fail, af in opts:
        ok = not af
        if ok:
            for i, v in req:
                if attrs[i] < v:
                    ok = False
                    break
        pairs = succ if ok else fail
        if pairs:
            nxt = list(attrs)
            for i, v in pairs:
                nxt[i] = min(nxt[i] + v, _exact_cap[i])
            nxt_t = tuple(nxt)
        else:
            nxt_t = attrs
        val = (win if ok else lose) + _seq_value(names, q + 1, nxt_t)
        if val > best_v:
            best_v, best_i = val, idx
    _seq_memo[key] = (best_i, best_v if best_i is not None else 0.0)


def seq_plan(
    events: Mapping[str, Mapping[str, Any]],
    names: Optional[Sequence[str]],
    attrs: Mapping[str, int],
    cur_q: int = 0,
    event_name: str = "",
) -> Optional[Tuple[Optional[int], float]]:
    """已知本局题序 names 时，第 cur_q 题的最优选项与期望剩余得分。

    返回 (选项下标, 期望剩余分)；题库元信息不完整或题序不匹配时返回 None，
    调用方应回退到 exact_choice（按阶段期望）或启发式。
    names 必须是完整 10 题的事件名，顺序与游戏一致；cur_q 是当前第几题（0 基）。
    """
    if not EXACT_ENABLED or not names:
        return None
    if not _build_exact_tables(events):
        return None
    if not (0 <= cur_q < min(len(names), len(_exact_qstage))):
        return None
    # 题序必须与当前正在答的事件对得上：如果这份题序其实是上一轮遗留的
    # （提供方未及时刷新），第 cur_q 题就会是别的题目，照着它规划会给错价值。
    if event_name and names[cur_q] != event_name:
        return None
    if not _exact_event_opts.get(names[cur_q]):
        return None
    want = tuple(names)
    if _seq_memo.get(_SEQ_NAMES_KEY) != want:
        reset_seq_memo()
        _seq_memo[_SEQ_NAMES_KEY] = want  # type: ignore[assignment]
    st = _exact_qstage[cur_q]
    win, lose = _stage_scores.get(st, (0, 0))
    t = tuple(int(attrs.get(a, 0)) for a in ATTRS)
    key = (cur_q << _EXACT_QSHIFT) | _pack(t)
    _eval_options_seq(names, _exact_event_opts[names[cur_q]], t, cur_q, win, lose, key)
    return _seq_memo[key]


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

    界面选项顺序与题库顺序一致（已用「危险任务」验证），故逐行配对求相似度。

    判定分取**去掉最差一行后的平均分**：选项 OCR 偶尔会把某一行整行糊成一两个字
    （实测「选唐僧带着去大夫」被读成「厦」），若按 4 行平均，正确事件会被压到阈值
    以下 → 整题判成「未知事件」而不点击 → 游戏停在原地等点击，表现出来就是卡死。
    去掉最差一行仍要求过阈值，既能容一次 OCR 失误，又不会把「4 行里有 2 行对不上」
    的事件放进来。
    """
    if not option_texts or not any(option_texts):
        return None
    scored = []
    for name, body in events.items():
        lib_options = body.get("options") or []
        if len(lib_options) != len(option_texts):
            continue
        sims = [
            text_similarity(opt.get("text", ""), ocr)
            for opt, ocr in zip(lib_options, option_texts)
        ]
        if not sims:
            continue
        if len(sims) > 3:
            score = (sum(sims) - min(sims)) / (len(sims) - 1)
        else:
            score = sum(sims) / len(sims)
        scored.append((score, name))
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
    question: Optional[int] = None,
    seq: Optional[Sequence[str]] = None,
    answered: Optional[Mapping[int, Sequence[str]]] = None,
) -> Decision:
    """返回该事件的最优选项。

    `question` 是本题的题号（0 基）。传了就启用精确后向归纳（EXACT_ENABLED 为真
    且题库元信息完整时），不传则只用启发式 —— 这样既保持旧调用方式的行为不变，
    也让单元测试可以单独验证启发式那条路径。

    `seq` 是本局 10 题的完整题序（事件名列表，来自服务器下发）。传了就用「已知
    题序」的精确规划，比按阶段均匀抽的期望更高；读不到题序时不传即可。

    `answered` 是 {阶段: [本阶段已答过的事件名, ...]}，让按阶段的归纳知道
    「同一个阶段里已经抽走过的题不会再出现」（阶段内不放回）。
    """
    if event_name not in events:
        raise KeyError(f"题库中没有事件「{event_name}」")
    opts = get_options(events[event_name])
    if not opts:
        raise ValueError(f"事件「{event_name}」没有任何选项")

    scored = []
    for opt in opts:
        ok, score, gains = evaluate(opt, attrs, weights, success_bonus)
        nxt = apply_delta(attrs, gains)
        # 排序主键：选完之后「带着新属性去打后面关键阶段的期望得分」。
        # 原评分只作次级项，用于近似平局时细化（实测 0.5 最优，2.0/8.0 差 0.1~0.3）。
        value = future_value(nxt, events) + VALUE_BLEND * score
        # 末位平局键：前面全相同时，留更多血更安全（血是唯一能让本局提前结束的资源）
        scored.append((opt, ok, score, gains, value, int(nxt.get("健康", 0))))

    # 排序键：先「能否成功」（拿阶段大分的前提），再「后续期望得分」，
    # 再原评分，最后剩余健康
    best = max(scored, key=lambda x: (x[1], x[4], x[2], x[5]))

    # 精确策略：用后向归纳的期望直接取 argmax（见本文件「精确后向归纳」一节）。
    # 它比启发式多 1.05 分，是线上默认路径；失败/不可用时安静地退回启发式。
    exact_note = ""
    plan_kind = ""
    picked: Optional[Tuple[Optional[int], float]] = None
    if question is not None:
        # 优先用「已知题序」的规划：它把后面每道题的具体内容都算进去了，
        # 比按阶段均匀抽的期望更高（实测 +4.63 分/局）。
        if seq:
            got = seq_plan(events, seq, attrs, int(question), event_name)
            if got is not None and got[0] is not None:
                for cand in scored:
                    if cand[0].index == got[0]:
                        best = cand
                        picked = got
                        plan_kind = "seq"
                        exact_note = (
                            f"精确前向规划·已知题序（第 {int(question) + 1} 题，"
                            f"期望 {got[1]:+.2f}，已展开 {seq_stats():,} 状态）"
                        )
                        break
        if exact_note == "":
            got = exact_choice(events, event_name, attrs, int(question), answered)
            if got is not None and got[0] is not None:
                for cand in scored:
                    if cand[0].index == got[0]:
                        best = cand
                        picked = got
                        plan_kind = "stage"
                        exact_note = (
                            f"精确后向归纳（第 {int(question) + 1} 题，"
                            f"期望 {got[1]:+.2f}，已展开 {exact_stats()[0]:,} 状态）"
                        )
                        break

    opt, ok, score, gains, value, _hp_after = best
    if exact_note and picked is not None:
        value = picked[1]

    if ok:
        reason = "满足门槛，必定成功" if opt.require else "无要求，必定成功"
        if any(v < 0 for v in gains.values()):
            reason += "；成功收益含负项，但在可成功项中评分最高"
    else:
        short = []
        for k, v in (opt.require or {}).items():
            short.append(f"{k}需{v}（当前{attrs.get(k, 0)}）")
        reason = "所有选项都不满足门槛，此为该情况下损失最小项：" + "、".join(short)
    if exact_note:
        reason = exact_note + "｜" + reason

    return Decision(
        event=event_name,
        index=opt.index,
        text=opt.text,
        will_succeed=ok,
        gains=gains,
        score=score,
        reason=reason,
        value=value,
        all_scores=tuple(
            (o.index, o.text, ok_, sc) for o, ok_, sc, _, _, _ in scored
        ),
        exact=bool(exact_note),
        plan=plan_kind,
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
