# -*- coding: utf-8 -*-
#
# 造梦西游4 · 生存大冒险 自动答题
# 作者：InkyuZero   (https://github.com/Inkyu-Zero/MAAZMXY4)
#
# 本脚本基于 MaaFramework 开发，完全免费。
# 如果你是付费获得的，请及时申请退款 —— 本脚本从未收费。
#
"""生存大冒险 —— Agent 侧实现。

分工：
  pipeline 负责「截图 / 判断是否还在答题界面 / 点击识别框」；
  本文件负责「OCR 题干 → 匹配事件 → 读当前属性 → 查表决策 → 返回要点的那一行」。

调用方式（pipeline）：
  "生存大冒险_读题": recognition = Custom(adventure_read), action = Click, target = true(自身)
  → 本识别器返回的 box 就是目标选项行的矩形，pipeline 的 Click 点其中心。

事件识别依据在 assets/data/adventure_ui.json 的 events.*.match（正则列表），
未登记的事件会走「采集分支」：记录题干文本到 debug/adventure/collected.jsonl，并先点第 1 项继续推进。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import adventure_logic as logic  # noqa: E402

from maa.agent.agent_server import AgentServer  # noqa: E402
from maa.context import Context  # noqa: E402
from maa.custom_action import CustomAction  # noqa: E402
from maa.custom_recognition import CustomRecognition  # noqa: E402

UI_PATH = os.path.join(_ROOT, "assets", "data", "adventure_ui.json")
LOG_DIR = os.path.join(_ROOT, "debug", "adventure")
COLLECTED_PATH = os.path.join(LOG_DIR, "collected.jsonl")
LOG_PATH = os.path.join(LOG_DIR, "adventure.log")

QUESTION_NODE = "生存大冒险_识别题干"
ATTR_NODE_FMT = "生存大冒险_属性_{}"
OPTION_NODE_FMT = "生存大冒险_选项_{}"
BULK_ATTR_NODE = "生存大冒险_属性全部"
CONTINUE_NODE = "生存大冒险_继续冒险"

# 「继续冒险」按钮在 720p 下的位置（由截图实测：原图 1062..1190 x 689..726）
CONTINUE_BOX = [863, 520, 106, 34]

# 选项文本反查事件的置信度门槛（4 行平均相似度）
OPTION_MATCH_THRESHOLD = 0.55

# 截图实测「小玉」的初始六维；interface.json 的输入项取不到值时的兜底
DEFAULT_ATTRS: Dict[str, int] = {
    "健康": 3,
    "战力": 3,
    "智慧": 4,
    "魅力": 5,
    "运气": 3,
    "灵巧": 6,
}


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
class _State:
    """跨题保存的属性账本：OCR 读不到时作为兜底，也便于日志追踪。"""

    attrs: Dict[str, int] = {}


STATE = _State()


def _ensure_dirs() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(f"[adventure] {msg}", flush=True)
    try:
        _ensure_dirs()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_LOGGED_ONCE: set = set()


def _log_once(key: str, msg: str) -> None:
    """同一类提示只打一次，避免每题刷屏。"""
    if key in _LOGGED_ONCE:
        return
    _LOGGED_ONCE.add(key)
    _log(msg)


def _ui() -> Dict[str, Any]:
    with open(UI_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _events() -> Dict[str, Any]:
    return logic.load_events()


def _to_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    digits = re.findall(r"-?\d+", str(value or ""))
    return int(digits[0]) if digits else None


def _as_dict(value: Any) -> Dict[str, Any]:
    """custom_action_param / custom_recognition_param 在 binding 里是 JSON 字符串。

    见 source/binding/Python/maa/custom_action.py 的 RunArg.custom_action_param: str。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _pick_text(obj: Any) -> str:
    """兼容不同 binding 版本的文本字段命名。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("text", "detail", "all", "best"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                return v
        return ""
    for attr in ("text", "detail"):
        v = getattr(obj, attr, None)
        if isinstance(v, str) and v:
            return v
    return ""


def _texts_of(detail: Any) -> List[str]:
    """尽力从 RecognitionDetail 里取出所有 OCR 文本。"""
    if detail is None:
        return []
    texts: List[str] = []
    for group in ("filtered_results", "all_results", "best_result"):
        results = getattr(detail, group, None)
        if results is None and isinstance(detail, dict):
            results = detail.get(group)
        if results is None:
            continue
        if not isinstance(results, (list, tuple)):
            results = [results]
        for item in results:
            t = _pick_text(item)
            if t:
                texts.append(t)
        if texts:
            return texts
    single = _pick_text(detail)
    return [single] if single else []


def _ocr(
    context: Context,
    image: Any,
    node: str,
    all_texts: bool = False,
    override: Optional[Dict[str, Any]] = None,
):
    try:
        if override:
            detail = context.run_recognition(node, image, pipeline_override=override)
        else:
            detail = context.run_recognition(node, image)
    except Exception as exc:  # 兼容不同版本签名/字段失败时不让任务崩掉
        _log(f"OCR 调用失败（{node}）：{type(exc).__name__}: {exc}")
        return [] if all_texts else ""
    texts = _texts_of(detail)
    if all_texts:
        return texts
    return texts[0] if texts else ""


def _ui_attr_order() -> List[str]:
    """界面自上而下的六维顺序（取自 adventure_ui.json 的 attr_rows 键序）。

    注意：与 logic.ATTRS 的顺序不同，按行配对时必须用这个。
    """
    order = list((_ui().get("layout") or {}).get("attr_rows", {}).keys())
    return order if len(order) == 6 else list(logic.ATTRS)


def _read_attrs_each(
    context: Context, image: Any, located: Optional[Dict[str, List[int]]]
) -> Optional[Dict[str, int]]:
    """逐行 OCR 六维：located 非空时用自适应坐标，否则用 pipeline 里的固定 ROI。"""
    attrs: Dict[str, int] = {}
    for name in logic.ATTRS:
        node = ATTR_NODE_FMT.format(name)
        override = None
        if located and name in located:
            override = {
                node: {
                    "recognition": {
                        "type": "OCR",
                        "param": {"roi": located[name], "only_rec": True},
                    }
                }
            }
        for text in _ocr(context, image, node, all_texts=True, override=override):
            value = _to_int(text)
            if value is not None:
                attrs[name] = value
                break
    if len(attrs) != len(logic.ATTRS):
        return None
    return attrs


def _read_attrs_bulk(context: Context, image: Any) -> Optional[Dict[str, int]]:
    """兜底：一次 OCR 整个属性数值列，按出现顺序取 6 个数字。"""
    texts = _ocr(context, image, BULK_ATTR_NODE, all_texts=True)
    numbers: List[int] = []
    for text in texts:
        for token in re.findall(r"\d+", text or ""):
            numbers.append(int(token))
    if len(numbers) != 6:
        _log(f"整块 OCR 得到 {len(numbers)} 个数字（期望 6 个）：{texts}")
        return None
    return dict(zip(_ui_attr_order(), numbers))


def _read_attrs(context: Context, image: Any) -> Optional[Dict[str, int]]:
    """识别左侧「生存属性」六维，三级兜底。

    1. 运行时自适应定位 + 逐行 OCR（最稳）
    2. pipeline 里写死的 6 个 ROI + 逐行 OCR
    3. 一次 OCR 整个数值列，按顺序取 6 个数字
    全都失败则返回 None，由调用方回退到账本。
    """
    if image is None:
        return None

    located = _locate_attrs(image)
    if located:
        _log_once("attrs_located", f"属性行使用自适应定位（健康行 y={located['健康'][1]}）")
        attrs = _read_attrs_each(context, image, located)
        if attrs:
            return attrs

    attrs = _read_attrs_each(context, image, None)
    if attrs:
        return attrs

    attrs = _read_attrs_bulk(context, image)
    if attrs:
        _log(f"属性走整块 OCR 兜底成功：{attrs}")
        return attrs

    _log("属性识别三级尝试全部失败，回退账本")
    return None


def _locate_question(image: Any) -> Optional[List[int]]:
    """自适应定位题干：扫描黄色文字行，返回 [x, y, w, h]。

    只做降采样扫描（每 3 行、每 2 列取一个像素），既不需要 numpy，也能在离线测试里跑。
    过滤条件来自对真实截图的像素实测：
      - 题干是黄字（R>160, G>120, B<115）
      - 位于画面中部（y 在 20%~75% 高度、x 在右侧 75% 宽度），可排除左上角的积分牌与顶部标题
    定位失败时返回 None，调用方回退到 pipeline 里写死的 ROI。
    """
    if image is None:
        return None
    try:
        h = len(image)
        w = len(image[0])
    except (TypeError, IndexError):
        return None
    if h < 100 or w < 200:
        return None

    step_y, step_x = 3, 2
    mid_x = int(w * 0.25)
    y_lo, y_hi = int(h * 0.20), int(h * 0.75)
    # 单行黄像素阈值：按采样密度折算，避免噪点被当成一行文字
    thr = max(6, int(((w - mid_x) / step_x) * 0.02))

    rows = []  # (y, xmin, xmax)
    for y in range(0, h, step_y):
        if not (y_lo < y < y_hi):
            continue
        row = image[y]
        xmin, xmax = w, -1
        for x in range(mid_x, w, step_x):
            px = row[x]
            if int(px[2]) > 160 and int(px[1]) > 120 and int(px[0]) < 115:
                if x < xmin:
                    xmin = x
                if x > xmax:
                    xmax = x
        if xmax >= 0 and ((xmax - xmin) / step_x) >= thr:
            rows.append((y, xmin, xmax))

    if not rows:
        return None

    # 把连续的行合并成区间，取最高的一段（题干通常 1~2 行，比标题更"厚"）
    # 合并阈值放宽到 6 个采样步长，以容忍多行题干之间的行距（否则会把一行行拆开）
    ranges = []
    start = rows[0][0]
    prev = rows[0][0]
    for y, _, _ in rows[1:]:
        if y - prev > step_y * 6:
            ranges.append((start, prev))
            start = y
        prev = y
    ranges.append((start, prev))

    y0, y1 = max(ranges, key=lambda r: r[1] - r[0])
    xs = [(xmin, xmax) for y, xmin, xmax in rows if y0 <= y <= y1]
    x_lo = min(p[0] for p in xs)
    x_hi = max(p[1] for p in xs)
    pad = 8
    return [
        max(0, x_lo - pad),
        max(0, y0 - pad),
        (x_hi - x_lo) + pad * 2,
        (y1 - y0) + pad * 2,
    ]


def _union_roi(a: List[int], b: List[int]) -> List[int]:
    """取两个 ROI 的并集。

    自适应定位有时只框住题干的一行（实测出现过宽 496/高 37、而实际是 680/66），
    与固定 ROI 取并集就不会把第二行截掉。
    """
    x0 = min(a[0], b[0])
    y0 = min(a[1], b[1])
    x1 = max(a[0] + a[2], b[0] + b[2])
    y1 = max(a[1] + a[3], b[1] + b[3])
    return [x0, y0, x1 - x0, y1 - y0]


def _locate_options(image: Any) -> Optional[List[List[int]]]:
    """自适应定位 4 个选项行：扫描画面中下部的白色文字，返回 4 个点击矩形。

    选项文字是白字、位于中部偏下，左侧属性区被 x 阈值排除、顶部标题被 y 阈值排除。
    只有当恰好找到 4 行时才返回，否则回退到 adventure_ui.json 里写死的坐标。
    """
    if image is None:
        return None
    try:
        h = len(image)
        w = len(image[0])
    except (TypeError, IndexError):
        return None
    if h < 100 or w < 200:
        return None

    step_y, step_x = 3, 2
    mid_x = int(w * 0.28)
    y_lo, y_hi = int(h * 0.35), int(h * 0.90)
    thr = max(10, int(((w - mid_x) / step_x) * 0.03))

    rows = []
    for y in range(y_lo, y_hi, step_y):
        row = image[y]
        xmin, xmax = w, -1
        for x in range(mid_x, w, step_x):
            px = row[x]
            if int(px[0]) > 150 and int(px[1]) > 150 and int(px[2]) > 150:
                if x < xmin:
                    xmin = x
                if x > xmax:
                    xmax = x
        if xmax >= 0 and ((xmax - xmin) / step_x) >= thr:
            rows.append((y, xmin, xmax))

    if not rows:
        return None

    ranges = []
    start = prev = rows[0][0]
    for y, _, _ in rows[1:]:
        if y - prev > step_y * 6:
            ranges.append((start, prev))
            start = y
        prev = y
    ranges.append((start, prev))

    if len(ranges) != 4:
        _log(f"选项行自适应定位得到 {len(ranges)} 行（期望 4 行），回退固定坐标")
        return None

    boxes = []
    for y0, y1 in ranges:
        center = (y0 + y1) // 2
        boxes.append([int(w * 0.28), center - 22, int(w * 0.42), 44])
    return boxes


def _locate_attrs(image: Any) -> Optional[Dict[str, List[int]]]:
    """自适应定位左侧「生存属性」面板的六维数值行。

    面板自上而下共 7 行（角色 + 六维），因此取扫描到的最后 6 行。
    行数不足 6 就放弃，回退到 pipeline 里的固定 ROI。
    """
    if image is None:
        return None
    try:
        h = len(image)
        w = len(image[0])
    except (TypeError, IndexError):
        return None
    if h < 100 or w < 200:
        return None

    step_y, step_x = 3, 2
    right_x = int(w * 0.30)
    y_lo, y_hi = int(h * 0.28), int(h * 0.92)
    thr = max(6, int((right_x / step_x) * 0.02))

    rows = []
    for y in range(y_lo, y_hi, step_y):
        row = image[y]
        count = 0
        for x in range(0, right_x, step_x):
            px = row[x]
            if int(px[0]) > 150 and int(px[1]) > 150 and int(px[2]) > 150:
                count += 1
        if count >= thr:
            rows.append(y)

    if not rows:
        return None

    ranges = []
    start = prev = rows[0]
    for y in rows[1:]:
        if y - prev > step_y * 6:
            ranges.append((start, prev))
            start = y
        prev = y
    ranges.append((start, prev))

    if len(ranges) < 6:
        _log(f"属性行自适应定位只找到 {len(ranges)} 行（期望 ≥6），回退固定坐标")
        return None

    # 行序必须与界面自上而下的顺序一致，因此从 adventure_ui.json 的 attr_rows 取键顺序
    # （logic.ATTRS 的顺序是另一套，直接用会错位）
    order = list((_ui().get("layout") or {}).get("attr_rows", {}).keys())
    if len(order) != 6:
        _log("adventure_ui.json 的 attr_rows 不足 6 项，跳过属性自适应定位")
        return None

    result: Dict[str, List[int]] = {}
    for name, (y0, y1) in zip(order, ranges[-6:]):
        center = (y0 + y1) // 2
        result[name] = [int(w * 0.103), center - 14, int(w * 0.040), 28]
    return result


def _is_result_panel(context: Context, image: Any) -> bool:
    """判断当前是否停在「结果面板」（答完一题后需要点「继续冒险」才能进下一题）。

    复用 pipeline 里的模板匹配节点，命中即认为结果面板还在。
    """
    try:
        detail = context.run_recognition(CONTINUE_NODE, image)
    except Exception as exc:  # noqa: BLE001
        _log(f"结果面板检测失败：{type(exc).__name__}: {exc}")
        return False
    return bool(getattr(detail, "hit", False))


def _match_event(question: str) -> Optional[str]:
    if not question:
        return None
    best: Optional[str] = None
    best_score = 0
    for name, feature in (_ui().get("events") or {}).items():
        score = 0
        for pattern in feature.get("match") or []:
            try:
                hit = re.search(pattern, question) is not None
            except re.error:
                hit = pattern in question
            if hit:
                score += 1
        if score > best_score:
            best, best_score = name, score
    return best


def _read_options(context: Context, image: Any) -> List[str]:
    return [_ocr(context, image, OPTION_NODE_FMT.format(i)) for i in range(1, 5)]


def _match_event_by_options(option_texts: List[str]) -> Optional[str]:
    name = logic.match_event_by_options(_events(), option_texts, OPTION_MATCH_THRESHOLD)
    if name:
        _log(f"按选项文本匹配到「{name}」")
    else:
        _log(f"选项文本匹配度不足，按未知事件处理：{option_texts}")
    return name


def _looks_like_question(question: str, option_texts: List[str]) -> bool:
    """判断当前是否真的停在答题界面。

    判据（满足任一即可）：
      1. 识别到至少 3 个非空选项——答题界面固定 4 个选项，这是最强特征；
      2. 题干含「你会」或以「_」结尾——题干特征。
    之所以不要求两者同时成立：题干是两行文字，OCR 偶尔只读到其中一行，
    若因此否决整个判断，就会把正常题目也挡掉（实测踩过这个坑）。
    """
    non_empty = sum(1 for t in option_texts if t and t.strip())
    if non_empty >= 3:
        return True
    if not question:
        return False
    return ("你会" in question) or question.rstrip().endswith("_")


def _collect(
    question: str,
    attrs: Optional[Dict[str, int]],
    option_texts: Optional[List[str]] = None,
    image: Any = None,
) -> None:
    """记录未知事件的题干与 4 行选项文本，供后续补进 adventure_ui.json。"""
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "question": question,
        "options_ocr": option_texts or [],
        "attrs": attrs or STATE.attrs,
        "has_image": image is not None,
    }
    _log(f"未知事件，已记录：{question[:60]}…")
    try:
        _ensure_dirs()
        with open(COLLECTED_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        _log(f"写入采集记录失败：{exc}")


def _apply(attrs: Dict[str, int], gains: Dict[str, int]) -> Dict[str, int]:
    out = dict(attrs)
    for key, delta in (gains or {}).items():
        out[key] = int(out.get(key, 0)) + int(delta)
    return out


# --------------------------------------------------------------------------- #
# 初始化属性（由 interface.json 的输入项注入）
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("adventure_init")
class AdventureInit(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # 只接受显式传入的属性；没有传入就保持空账本，完全以界面 OCR 为准
        # （角色是随机出现的，写死默认值反而会在 OCR 失败时给出错误判断）
        param = _as_dict(getattr(argv, "custom_action_param", None))
        attrs: Dict[str, int] = {}
        for name in logic.ATTRS:
            value = _to_int(param.get(name))
            if value is not None:
                attrs[name] = value
        STATE.attrs = attrs
        if attrs:
            _log(f"账本初始化（来自输入项）：{attrs}")
        else:
            _log("账本为空：本局六维完全以界面 OCR 识别为准")
        return True


# --------------------------------------------------------------------------- #
# 读题 + 决策
# --------------------------------------------------------------------------- #
@AgentServer.custom_recognition("adventure_read")
class AdventureRead(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # 任何异常都不该让整个任务崩掉：记录后按「未命中」处理（节点会走 on_error）
        try:
            return self._analyze(context, argv)
        except Exception as exc:  # noqa: BLE001
            _log(f"识别异常，按未命中处理：{type(exc).__name__}: {exc}")
            return None

    def _analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        image = getattr(argv, "image", None)
        # 点击行位置同样优先用运行时实测值，避免坐标偏移导致点错行
        boxes = _locate_options(image) or _ui()["layout"]["option_boxes"]

        # 优先用运行时实测的题干位置（应对分辨率/DPI/窗口差异），失败才用 pipeline 里的固定 ROI
        located = _locate_question(image)
        override = None
        if located:
            fixed = (_ui().get("layout") or {}).get("question_roi")
            roi = _union_roi(located, fixed) if fixed else located
            override = {QUESTION_NODE: {"recognition": {"type": "OCR", "param": {"roi": roi}}}}
            _log_once("question_located", f"题干定位：自适应={located}，取并集后={roi}")
        question = _ocr(context, image, QUESTION_NODE, override=override)
        if not question:
            _log("题干区域没有文字，判定为已离开答题界面")
            return None

        # 答完一题后会出现结果面板，此时题干仍在屏幕上，必须优先把它点掉
        if _is_result_panel(context, image):
            _log("检测到「继续冒险」按钮，判定为结果面板，点击它")
            return CustomRecognition.AnalyzeResult(
                box=(CONTINUE_BOX[0], CONTINUE_BOX[1], CONTINUE_BOX[2], CONTINUE_BOX[3]),
                detail={"event": None, "option": 0, "reason": "结果面板：点击「继续冒险」"},
            )

        attrs = _read_attrs(context, image) or dict(STATE.attrs)
        STATE.attrs = dict(attrs)

        option_texts = _read_options(context, image)
        if not _looks_like_question(question, option_texts):
            _log(
                "不像答题界面（题干或选项不符），不做任何点击"
                f"｜题干：{question[:40]}｜选项：{option_texts}"
            )
            return None

        event = _match_event(question) or _match_event_by_options(option_texts)
        if event:
            decision = logic.choose(_events(), event, attrs)
            STATE.attrs = _apply(attrs, decision.gains)
            _log(f"{decision.describe()}｜题干：{question}")
            detail = {
                "event": event,
                "option": decision.index + 1,
                "text": decision.text,
                "will_succeed": decision.will_succeed,
                "gains": decision.gains,
                "reason": decision.reason,
                "options_ocr": option_texts,
            }
            index = decision.index
        else:
            _collect(question, attrs, option_texts, image)
            # 未知事件（通常是选项 OCR 截断或新事件）一律不点击：
            # 题库已覆盖 41 个事件，识别失败时乱点的代价（可能扣 9 点健康）远大于中断一次
            _log("未知事件：已记录题干与选项，但为安全起见不做任何点击")
            return None

        box = boxes[index]
        # AnalyzeResult.detail 必须是 dict：binding 内部会自己 json.dumps
        # 见 source/binding/Python/maa/custom_recognition.py
        return CustomRecognition.AnalyzeResult(
            box=(int(box[0]), int(box[1]), int(box[2]), int(box[3])),
            detail=detail,
        )
