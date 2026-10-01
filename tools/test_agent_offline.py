"""离线验证 Agent 逻辑：用 stub 顶替 maa 包，跑通 analyze 的完整决策链路。

不需要安装 MaaFw，也不需要游戏——只要能 import 本文件所在的 agent 目录即可。
验证点：题干识别 → 选项文本反查事件 → 属性读取 → 决策 → 返回目标选项行的 box。
"""

import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(ROOT, "agent")
UI_PATH = os.path.join(ROOT, "assets", "data", "adventure_ui.json")

ok = True


def check(label, passed, extra=""):
    global ok
    if not passed:
        ok = False
    print(f"{'OK  ' if passed else 'FAIL'} {label}{(' -> ' + str(extra)) if extra else ''}")


# --------------------------------------------------------------------------- #
# 1) 用 stub 顶替 maa 包（adventure.py 依赖它，但逻辑本身与框架无关）
# --------------------------------------------------------------------------- #
class _AgentServer:
    @staticmethod
    def custom_recognition(name):
        def deco(cls):
            cls._registered_name = name
            return cls

        return deco

    @staticmethod
    def custom_action(name):
        def deco(cls):
            cls._registered_name = name
            return cls

        return deco


class _CustomAction:
    class RunArg:
        pass


class _CustomRecognition:
    class AnalyzeArg:
        pass

    class AnalyzeResult:
        def __init__(self, box=None, detail=None):
            self.box = box
            self.detail = detail


def _mk(name):
    return types.ModuleType(name)


maa = _mk("maa")
maa_agent = _mk("maa.agent")
maa_agent_server = _mk("maa.agent.agent_server")
maa_agent_server.AgentServer = _AgentServer
maa_context = _mk("maa.context")
maa_context.Context = type("Context", (), {})
maa_ca = _mk("maa.custom_action")
maa_ca.CustomAction = _CustomAction
maa_cr = _mk("maa.custom_recognition")
maa_cr.CustomRecognition = _CustomRecognition

sys.modules.update(
    {
        "maa": maa,
        "maa.agent": maa_agent,
        "maa.agent.agent_server": maa_agent_server,
        "maa.context": maa_context,
        "maa.custom_action": maa_ca,
        "maa.custom_recognition": maa_cr,
    }
)

sys.path.insert(0, AGENT_DIR)
import adventure  # noqa: E402

# 把日志与采集输出重定向到临时目录，避免污染真实运行日志（debug/adventure/）
_TMP = os.environ.get("TEMP", ".")
adventure.LOG_PATH = os.path.join(_TMP, "adventure_offline_test.log")
adventure.COLLECTED_PATH = os.path.join(_TMP, "adventure_offline_test_collected.jsonl")

# --------------------------------------------------------------------------- #
# 2) 假的识别结果与 context（按节点名分派文本）
# --------------------------------------------------------------------------- #
class FakeResult:
    def __init__(self, text):
        self.text = text
        self.box = (0, 0, 1, 1)
        self.score = 0.99


class FakeDetail:
    def __init__(self, texts):
        self.hit = bool(texts)
        self.box = (0, 0, 1, 1)
        self.filtered_results = [FakeResult(t) for t in texts]
        self.all_results = self.filtered_results
        self.best_result = self.filtered_results[0] if self.filtered_results else None


class FakeContext:
    def __init__(self, mapping):
        self.mapping = mapping

    def run_recognition(self, node, image, pipeline_override=None):
        return FakeDetail(self.mapping.get(node, []))


class Argv:
    image = object()  # 非 None 即可，OCR 由 FakeContext 顶替


UI = json.load(open(UI_PATH, encoding="utf-8"))
BOXES = [tuple(b) for b in UI["layout"]["option_boxes"]]
ATTRS = ("健康", "战力", "智慧", "魅力", "运气", "灵巧")

DANGER_QUESTION = "在你准备离开村子去冒险时，族长想交给你一个很危险的任务，并许诺完成任务后给你奖励，你会_"
DANGER_OPTIONS = ["坚定不移的接受任务", "我拒绝接受任务，自己出去冒险", "默默不做声", "推荐你的仇敌"]


def build_mapping(question, options, attrs):
    mapping = {adventure.QUESTION_NODE: [question]}
    for i, text in enumerate(options, start=1):
        mapping[adventure.OPTION_NODE_FMT.format(i)] = [text]
    for name in ATTRS:
        mapping[adventure.ATTR_NODE_FMT.format(name)] = [str(attrs[name])]
    return mapping


def run(question, options, attrs, initial=None):
    adventure.STATE.attrs = dict(initial if initial is not None else attrs)
    ctx = FakeContext(build_mapping(question, options, attrs))
    return adventure.AdventureRead().analyze(ctx, Argv())


print("=== 场景 1：危险任务，战力 4（满足「接受」门槛 战力4）===")
attrs = {"健康": 3, "战力": 4, "智慧": 4, "魅力": 5, "运气": 3, "灵巧": 6}
res = run(DANGER_QUESTION, DANGER_OPTIONS, attrs)
check("命中并返回结果", res is not None)
if res:
    check("识别为「危险任务」", res.detail.get("event") == "危险任务", res.detail.get("event"))
    check("选中第 1 项「接受」", res.detail.get("option") == 1, res.detail.get("option"))
    check("box 对应第 1 行", tuple(res.box) == BOXES[0], f"{res.box} vs {BOXES[0]}")
    print(f"     决策理由：{res.detail.get('reason')}")

print("\n=== 场景 2：危险任务，战力 3（四项全不满足门槛）===")
attrs_low = {"健康": 3, "战力": 3, "智慧": 3, "魅力": 3, "运气": 3, "灵巧": 6}
res2 = run(DANGER_QUESTION, DANGER_OPTIONS, attrs_low)
check("命中并返回结果", res2 is not None)
if res2:
    check("仍在「危险任务」内决策", res2.detail.get("event") == "危险任务")
    check("box 是 4 行之一", tuple(res2.box) in BOXES, res2.box)
    check("判定为必然失败", res2.detail.get("will_succeed") is False)
    print(f"     选中第 {res2.detail.get('option')} 项，理由：{res2.detail.get('reason')}")

print("\n=== 场景 3：题干区无文字（已离开答题界面）===")
res3 = run("", DANGER_OPTIONS, attrs)
check("返回 None 表示未命中", res3 is None, res3)

print("\n=== 场景 4：未知事件（题干与选项都不匹配题库）===")
before = 0
if os.path.exists(adventure.COLLECTED_PATH):
    before = sum(1 for _ in open(adventure.COLLECTED_PATH, encoding="utf-8"))
res4 = run("这是一段题库里没有的题干内容_", ["选项甲", "选项乙", "选项丙", "选项丁"], attrs)
check("未知事件返回 None（为安全不点击）", res4 is None, res4)
after = 0
if os.path.exists(adventure.COLLECTED_PATH):
    after = sum(1 for _ in open(adventure.COLLECTED_PATH, encoding="utf-8"))
check("仍写入采集记录 collected.jsonl", after > before, f"{before} -> {after}")

print("\n=== 场景 5：属性 OCR 失败时回退到内部账本 ===")
adventure.STATE.attrs = {"健康": 9, "战力": 9, "智慧": 9, "魅力": 9, "运气": 9, "灵巧": 9}
ctx = FakeContext({adventure.QUESTION_NODE: [DANGER_QUESTION],
                   **{adventure.OPTION_NODE_FMT.format(i): [t] for i, t in enumerate(DANGER_OPTIONS, 1)}})
res5 = adventure.AdventureRead().analyze(ctx, Argv())
check("属性读不到仍能决策", res5 is not None)
if res5:
    check("用账本属性判定为成功", res5.detail.get("will_succeed") is True, res5.detail.get("reason"))

print("\n=== 场景 6：自适应题干定位（用模拟 720p 图的真实像素）===")
RAW_PATH = os.path.join(ROOT, "debug", "_sim_720p.raw")
if not os.path.exists(RAW_PATH):
    print("  跳过：缺少 debug/_sim_720p.raw")
else:
    raw = open(RAW_PATH, "rb").read()
    _w, _h = 1147, 720
    _stride = len(raw) // _h

    class _Row:
        __slots__ = ("data", "base", "w")

        def __init__(self, data, base, w):
            self.data = data
            self.base = base
            self.w = w

        def __len__(self):
            return self.w

        def __getitem__(self, x):
            i = self.base + x * 3
            return (self.data[i], self.data[i + 1], self.data[i + 2])

    class RawImage:
        __slots__ = ("data", "stride", "w", "h")

        def __init__(self, data, stride, w, h):
            self.data = data
            self.stride = stride
            self.w = w
            self.h = h

        def __len__(self):
            return self.h

        def __getitem__(self, y):
            return _Row(self.data, y * self.stride, self.w)

    img = RawImage(raw, _stride, _w, _h)
    roi = adventure._locate_question(img)
    check("定位到题干区域", roi is not None, roi)
    if roi:
        x, y, w2, h2 = roi
        print(f"     自适应定位结果 {roi}；pipeline 里写死的是 [335, 232, 680, 66]")
        check("y 落在题干行（200~285）", 200 <= y <= 285, y)
        check("x 落在题干列（300~390）", 300 <= x <= 390, x)
        check("宽度合理（600~790）", 600 <= w2 <= 790, w2)
        check("高度合理（40~130）", 40 <= h2 <= 130, h2)
        check("未把顶部标题误判成题干（y > 150）", y > 150, y)

    print("\n=== 场景 7：自适应选项行定位（同样用真实像素）===")
    opt_boxes = adventure._locate_options(img)
    check("定位到 4 个选项行", bool(opt_boxes) and len(opt_boxes) == 4, opt_boxes)
    if opt_boxes and len(opt_boxes) == 4:
        fixed = UI["layout"]["option_boxes"]
        for i, (got, exp) in enumerate(zip(opt_boxes, fixed), start=1):
            got_cy = got[1] + got[3] // 2
            exp_cy = exp[1] + exp[3] // 2
            check(f"选项{i} 行中心与固定值相差 ≤12px", abs(got_cy - exp_cy) <= 12, f"自适应={got_cy} 固定={exp_cy}")
        print(f"     自适应: {opt_boxes}")
        print(f"     固定值: {fixed}")

    print("\n=== 场景 8：自适应属性行定位 ===")
    attr_map = adventure._locate_attrs(img)
    check("定位到 6 个属性行", bool(attr_map) and len(attr_map) == 6, list(attr_map) if attr_map else None)
    if attr_map and len(attr_map) == 6:
        fixed_attrs = UI["layout"]["attr_rows"]
        for name in ("健康", "战力", "智慧", "魅力", "运气", "灵巧"):
            got = attr_map[name]
            exp = fixed_attrs[name]
            got_cy = got[1] + got[3] // 2
            exp_cy = exp[1] + exp[3] // 2
            check(f"属性「{name}」行中心相差 ≤12px", abs(got_cy - exp_cy) <= 12, f"自适应={got_cy} 固定={exp_cy}")
        print(f"     自适应: {attr_map}")

print("\n=== 场景 9：逐行 OCR 全失败时的整块兜底（且账本为空）===")
adventure.STATE.attrs = {}
bulk_mapping = {adventure.QUESTION_NODE: [DANGER_QUESTION]}
for i, t in enumerate(DANGER_OPTIONS, start=1):
    bulk_mapping[adventure.OPTION_NODE_FMT.format(i)] = [t]
# 健康 战力 智慧 魅力 运气 灵巧（界面自上而下）
bulk_mapping[adventure.BULK_ATTR_NODE] = ["3", "3", "4", "5", "3", "6"]
res9 = adventure.AdventureRead().analyze(FakeContext(bulk_mapping), Argv())
check("整块兜底仍能完成决策", res9 is not None)
if res9:
    check("识别为「危险任务」", res9.detail.get("event") == "危险任务", res9.detail.get("event"))
    check("属性确实来自整块 OCR", isinstance(res9.detail.get("will_succeed"), bool), res9.detail)
    print(f"     选中第 {res9.detail.get('option')} 项，理由：{res9.detail.get('reason')}")

print("\n=== 场景 10：属性完全读不到 + 账本为空（保守决策）===")
adventure.STATE.attrs = {}
res10 = adventure.AdventureRead().analyze(
    FakeContext({adventure.QUESTION_NODE: [DANGER_QUESTION],
                 **{adventure.OPTION_NODE_FMT.format(i): [t] for i, t in enumerate(DANGER_OPTIONS, 1)}}),
    Argv(),
)
check("无属性信息时仍返回结果（不崩）", res10 is not None)
if res10:
    check("判定为必然失败（保守）", res10.detail.get("will_succeed") is False, res10.detail)
    print(f"     选中第 {res10.detail.get('option')} 项，理由：{res10.detail.get('reason')}")

print("\n=== 场景 11：不在答题界面时绝不点击（防止乱点扣属性）===")
# 这串文字来自真实日志：当时界面是某个挑战/积分面板，不是答题界面
bad_question = "挑战不满意，可花费一定点券"
bad_options = ["挑战不满意，可花费一定点券", "", "本周生友积分", ""]
res11 = adventure.AdventureRead().analyze(
    FakeContext(build_mapping(bad_question, bad_options, attrs)), Argv()
)
check("非答题界面返回 None（不点击）", res11 is None, res11)

# 题干是答题界面的（含「你会」）时，即使选项没读全也应当继续——避免误伤正常题目
res12 = adventure.AdventureRead().analyze(
    FakeContext(build_mapping(DANGER_QUESTION, ["坚定不移的接受任务", "", "", ""], attrs)), Argv()
)
check("题干达标时即使选项不全也继续决策", res12 is not None, res12)

# 正常答题界面仍然照常决策
res13 = adventure.AdventureRead().analyze(
    FakeContext(build_mapping(DANGER_QUESTION, DANGER_OPTIONS, attrs)), Argv()
)
check("正常答题界面仍能决策", res13 is not None and res13.detail.get("event") == "危险任务")

print("\n=== 场景 12：战力接近门槛时，不该为了加健康而牺牲战力 ===")
# 登山黑影第2项「给他一拳」：成功收益 健康+1 / 战力-1（该事件门槛是战力6）
# 战力接近上限门槛(10)时减 1 会失守，应避开；战力富余时换健康才是划算的
_events = adventure.logic.load_events()
for _power, _avoid in ((9, True), (10, True), (12, False), (15, False)):
    _attrs = {"健康": 4, "战力": _power, "智慧": 5, "魅力": 5, "运气": 5, "灵巧": 5}
    _d = adventure.logic.choose(_events, "登山黑影", _attrs)
    if _avoid:
        check(f"战力{_power} 时不牺牲战力（避开第2项）", _d.index != 1, f"选了「{_d.text}」")
    else:
        check(f"战力{_power} 富余时可换健康（选第2项）", _d.index == 1, f"选了「{_d.text}」")
    for _i, _t, _ok2, _sc in _d.all_scores:
        print(f"       第{_i}项 {_t:<6} 可成功={str(_ok2):<5} 评分={_sc:+7.2f}")

print("\n=== 场景 13：减少接近门槛的属性必须被重罚（正负对称）===")
_base = {"健康": 4, "战力": 9, "智慧": 5, "魅力": 5, "运气": 5, "灵巧": 5}
_w9 = adventure.logic._attr_weight("战力", -1, _base)
_w12 = adventure.logic._attr_weight("战力", -1, {"健康": 4, "战力": 12})
_wup = adventure.logic._attr_weight("战力", 1, _base)
check("战力9 减1（会跌破门槛10）按高权重计", _w9 > _w12, f"战力9时={_w9} 战力12时={_w12}")
check("战力12 减1（仍达标）按低权重计", _w12 < _wup, f"战力12减1={_w12} 战力9加1={_wup}")

print("\n=== 场景 14：健康扣血权重必须随剩余血量单调递增 ===")
# 早先的缺陷：扣血用「距门槛的差距」计权，导致剩 2 点血(gap=4)比剩 3 点(gap=3)罚得更轻，恰好反了
_prev, _mono, _wseq = None, True, []
for _hp in (12, 8, 6, 5, 4, 3, 2, 1):
    _w = adventure.logic._attr_weight("健康", -1, {"健康": _hp})
    _wseq.append(f"血{_hp}={_w:g}")
    if _prev is not None and _w < _prev:
        _mono = False
    _prev = _w
check("扣血代价随剩余血量减少而单调增加", _mono, " ".join(_wseq))
check("剩 1 点血的代价高于剩 4 点",
      adventure.logic._attr_weight("健康", -1, {"健康": 2})
      > adventure.logic._attr_weight("健康", -1, {"健康": 5}))
check("血量充足（≥7）扣 1 点代价很低",
      adventure.logic._attr_weight("健康", -1, {"健康": 9}) <= 1.0)

print("\n=== 场景 15：血量低时优先补血，血量够时转堆其他属性 ===")
# 冒险前神秘礼物 4 个选项都能成功：绝世秘籍(健康+1/战力+1)、招财猫(运气+1) 等
for _hp, _want in ((2, 1), (4, 1), (6, 2), (9, 2)):
    _a = {"健康": _hp, "战力": 4, "智慧": 4, "魅力": 4, "运气": 4, "灵巧": 4}
    _d = adventure.logic.choose(_events, "冒险前神秘礼物", _a)
    check(f"健康{_hp} 时选择第{_want+1}项", _d.index == _want, f"选了「{_d.text}」")

print("\n=== 场景 16：健康目标不应高于题库实际最高门槛 ===")
check("健康目标 = 6（实际最高门槛）", adventure.logic.THRESHOLD_TARGETS.get("健康") == 6,
      adventure.logic.THRESHOLD_TARGETS.get("健康"))
for _k, _v in (("战力", 10), ("灵巧", 10), ("魅力", 10), ("智慧", 10), ("运气", 9)):
    check(f"{_k}目标 = {_v}", adventure.logic.THRESHOLD_TARGETS.get(_k) == _v,
          adventure.logic.THRESHOLD_TARGETS.get(_k))

print("\n" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
