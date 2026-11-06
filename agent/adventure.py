# -*- coding: utf-8 -*-
#
# 造梦西游4 · 生存大冒险 自动答题
# 作者：InkyuZero   (https://github.com/Inkyu-Zero/MAAZMXY4)
#
# 本项目基于 MaaFramework 开发，完全免费。
# 如果你是付费获得的，请及时申请退款 —— 本项目从未收费。
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
import threading
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import adventure_logic as logic  # noqa: E402
from mouse_input import finish_mouse_operation

# 通用本地扩展点：仓库版与自用版内容完全一致，靠「有没有这个模块」决定行为。
# 发布包里没有 adventure_local_hook.py，此处自动为 None，行为与发布版相同。
try:
    import adventure_local_hook as _hook  # type: ignore
except Exception:                             # noqa: BLE001
    _hook = None

from maa.agent.agent_server import AgentServer  # noqa: E402
from maa.context import Context  # noqa: E402
from maa.custom_action import CustomAction  # noqa: E402
from maa.custom_recognition import CustomRecognition  # noqa: E402

# 放在 agent/data 而不是 assets/data：整包 7z 白名单和自动更新包都只带
# resource/、agent/、interface.json 三样，assets/ 不会进包。以前写 assets/data
# 时，用户装完包一跑就 FileNotFoundError（每次识别都抛，按未命中处理 →
# 界面永远认不出来 → 任务卡在「运行中」空转）。
UI_PATH = os.path.join(_ROOT, "agent", "data", "adventure_ui.json")
LOG_DIR = os.path.join(_ROOT, "debug", "adventure")
COLLECTED_PATH = os.path.join(LOG_DIR, "collected.jsonl")
LOG_PATH = os.path.join(LOG_DIR, "adventure.log")

QUESTION_NODE = "生存大冒险_识别题干"
# 改动 agent 代码后改这个值，日志里就能确认「agent 到底加载了哪一版」
AGENT_VERSION = "2026-10-29-managed-boundary"
CHARACTER_NODE = "生存大冒险_角色"
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
    # 本局答到第几题（0 基）。精确后向归纳要靠它定位「还剩哪些阶段」，
    # 否则第 5 题也会按「10 题全在」来估值。
    question: int = 0
    # 本局各阶段已答过的事件名 {阶段: [事件名, ...]}。
    # 题号优先由「事件属于哪个阶段、在该阶段里排第几个」反推
    # （见 logic.question_for_event），决策次数只作兜底 ——
    # 点击落不到新题上时同一题会被再答一次，用次数会一路把题号推高。
    stage_seen: Dict[int, list] = {}
    # 本局的策略/耗时那行只打一次（重复读取第 1 题时不再刷屏）
    strategy_logged: bool = False
    # 开局认出的角色名与它的初始六维（第 1 题时识别一次）
    character: str = ""
    char_checked: bool = False
    # 本局初始六维（第 1 题时锁定的那一份）。attrs 会被每题改掉，
    # 上帝模式算最优解必须拿初始值，所以单独留一份。
    initial_attrs: Dict[str, int] = {}
    # 本局建精确归纳表用了多久（用于把「拉表工作量」量化到日志里）
    table_cost: float = 0.0
    # 本局每题得分 {题号(0 基): 分数}。用字典而不是累加一个整数，是为了
    # 「同一题被重复点击」时不会重复计分（实测重复决策很常见，最多一次多 5 次）。
    score_by_q: Dict[int, int] = {}
    # 本局是否已经打过「结算」日志（结算画面每帧都会被读到，只该打一次）
    finished: bool = False
    # 刚刚点过的那一题 (题号, 事件名)：点完选项后答题动画要一两秒，
    # 这期间 pipeline 会再跑一次读题，重复决策会让账本多扣一次属性，
    # 还可能把第二个选项也点进去（实测：螺旋地狱第一次点扣健康的项直接送死）。
    clicked_key: tuple = ()
    repeat_count: int = 0
    # 结算面板的「结束冒险」是否已经点过（防止每轮重复点）
    result_clicked: bool = False
    # 最近一次「有进展」的时刻（识别出题干 / 认出结算面板）。只用于空转提示。
    last_progress_ts: float = 0.0
    idle_warned: bool = False

    def total_score(self) -> int:
        return sum(self.score_by_q.values())


STATE = _State()

# 结算收尾的互斥锁，见 _log_result
_RESULT_LOCK = threading.Lock()


def _ensure_dirs() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


def _log(msg: str) -> None:
    """写日志文件，同时打到 stdout。

    面板展示交给 pipeline 节点的 `focus`（指向 debug/adventure/panel.md），
    这里只负责落盘 + stdout（进 debug/maafw.log，排查时翻）。
    """
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(msg, flush=True)
    try:
        _ensure_dirs()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


if _hook is not None:
    try:
        _hook.configure(_log)
    except Exception:                         # noqa: BLE001
        _hook = None


def _plan_label(decision) -> str:
    """把 Decision.plan 翻成人话，日志里一眼看出走的是哪条策略。"""
    return {
        "seq": "精确·前向规划",
        "stage": "精确·按阶段（后向归纳）",
    }.get(getattr(decision, "plan", ""), "启发式")


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


def _ocr_detail(context: Context, image: Any, node: str, override: Optional[Dict[str, Any]] = None):
    """和 _ocr 一样跑一次识别，但返回原始 RecognitionDetail（要拿 box 时用）。"""
    try:
        if override:
            return context.run_recognition(node, image, pipeline_override=override)
        return context.run_recognition(node, image)
    except Exception as exc:  # noqa: BLE001
        _log(f"OCR 调用失败（{node}）：{type(exc).__name__}: {exc}")
        return None


def _texts_and_boxes(detail: Any) -> List[tuple]:
    """从 RecognitionDetail 里取出 [(文本, [x, y, w, h]), ...]。

    结算面板要点「结束冒险」按钮，光有文字不够，还得有它的框才能点。
    """
    if detail is None:
        return []
    for group in ("filtered_results", "all_results", "best_result"):
        results = getattr(detail, group, None)
        if results is None and isinstance(detail, dict):
            results = detail.get(group)
        if results is None:
            continue
        if not isinstance(results, (list, tuple)):
            results = [results]
        out = []
        for item in results:
            t = _pick_text(item)
            box = getattr(item, "box", None)
            if box is None and isinstance(item, dict):
                box = item.get("box")
            if t:
                out.append((t, list(box) if box else None))
        if out:
            return out
    t = _pick_text(detail)
    box = getattr(detail, "box", None)
    if box is None and isinstance(detail, dict):
        box = detail.get("box")
    return [(t, list(box) if box else None)] if t else []


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


def _read_character(context: Context, image: Any) -> str:
    """OCR 左侧木牌第一行的角色名（如「小玉」）。认不出来返回空串。

    这一行只在「局内」存在：结算面板上该 ROI 是空的 —— 实测 221 张截图按这个
    ROI 聚类正好分成「7 个角色 + 2 组全空」，所以空结果本身也可以当
    「已经离开答题界面」的旁证。
    """
    if image is None:
        return ""
    text = _ocr(context, image, CHARACTER_NODE)
    return (text or "").strip()


def _opening_attrs(
    context: Context, image: Any, ocr_attrs: Dict[str, int], question: int
) -> Dict[str, int]:
    """开局（第 1 题）用「角色」锁死初始六维。

    为什么可信：题号 0 只可能是本局第 1 题，此时属性还没被任何选项改动过，
    所以面板上一定是角色的初始六维；反过来，中途接手一局时第一个事件属于别的
    阶段，题号不会是 0。

    三档处理，越靠前越保守：
      · 属性 OCR 全失败（attrs 为空，账本也为空）→ 直接用角色初值，这是主要的修复点
      · OCR 与角色完全一致 → 记一行日志
      · 只差一个属性 → 判为 OCR 读错一位，按角色纠正
      · 差两处以上 → 判为「中途接手」，信任 OCR，只记录不一致
    """
    if STATE.char_checked or question != 0:
        return ocr_attrs
    STATE.char_checked = True

    name = _read_character(context, image)
    vector = logic.character_attrs(name) if name else None
    STATE.character = name
    if not vector:
        _log(f"开局：角色行 OCR 得到「{name}」，未匹配到 7 个角色，初值仍以属性 OCR 为准")
        return ocr_attrs

    if not ocr_attrs:
        _log(f"开局：角色={name}，属性 OCR 不可用 → 用角色初值锁死六维 {vector}")
        return vector
    if ocr_attrs == vector:
        _log(f"开局：角色={name}，与属性 OCR 完全一致 {vector}")
        return ocr_attrs

    diff = {k: (ocr_attrs.get(k), vector[k]) for k in vector if ocr_attrs.get(k) != vector[k]}
    if len(diff) == 1:
        _log(f"开局：角色={name}，属性 OCR 有 1 处偏差 {diff} → 按角色纠正为 {vector}")
        return vector
    _log(f"开局：角色={name}，与属性 OCR 有 {len(diff)} 处不一致 {diff} → 判为中途接手，按 OCR 走")
    return ocr_attrs


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


_RESULT_MARKS = ("冒险成功", "冒险失败", "直降为0", "结束冒险")

# 结算识别专用节点（pipeline 里 ROI 放大到题干+选项+按钮）
RESULT_NODE = "生存大冒险_结算识别"
# check 阶段只认「真的结算了」（成功/失败/直降），不能含「结束冒险」——
# 那只是按钮文字，按钮还在时把它当结算标记会提前收工。
_CHECK_MARKS = ("冒险成功", "冒险失败", "直降为0")


PANEL_FILE = os.path.join(LOG_DIR, "panel.md")
# 同一题「刚点过」的跳过上限（秒级轮询一次）。
# 实测：点完选项后游戏要 3~10 秒才把界面切走，上限太小会在切走前重新决策，
# 同一题的条目就会重复贴进面板；上限太大则万一真点空了要等更久才重试。
_REPEAT_LIMIT = 12


_PANEL_LAST: list = []


def _panel(text: str) -> None:
    """把当前进度写到 panel.md。

    pipeline 里「读题」与「结算」节点的 `focus` 指向这个文件，
    MFA 收到节点事件时会读取文件内容贴进日志面板 —— 这是 agent 的
    stdout 之外，唯一的「动态文本进面板」通路。

    同一局面被重复决策（答题动画没结束时常见）时内容一模一样，
    这里直接跳过，免得面板刷出两条一样的。
    """
    body = text.rstrip()
    if _PANEL_LAST and _PANEL_LAST[0] == body:
        return
    _PANEL_LAST[:] = [body]
    try:
        os.makedirs(os.path.dirname(PANEL_FILE), exist_ok=True)
        with open(PANEL_FILE, "w", encoding="utf-8", newline="\n") as fp:
            # 空内容写成真正的空文件：结果面板那一圈会调用 _panel("")，
            # 若写成 "\n"，focus 仍会贴出一个空条，看着还是"多了一条"。
            fp.write(body + "\n" if body else "")
    except Exception:
        pass


def _result_mark(text: str) -> str:
    """结算面板的特征文案。认到就说明这一局已经结束，任务该收工了。"""
    body = text or ""
    for one in _RESULT_MARKS:
        if one in body:
            return one
    hit = re.search(r"获得\s*\d+\s*积分", body)
    return hit.group(0) if hit else ""


def _click_end_adventure(context: Context, image: Any) -> bool:
    """结算面板上找「结束冒险」按钮点掉，认不到就返回 False（绝不阻塞收工）。

    这活原来是 pipeline「结算」节点 click 阶段干的。挪进 agent 是为了点完
    当场就能停任务，不用再绕答题循环一圈。
    """
    if STATE.result_clicked:
        return True
    pairs = _texts_and_boxes(_ocr_detail(context, image, RESULT_NODE)) or []
    for text, box in pairs:
        if "结束冒险" in (text or "") and box:
            bx, by, bw, bh = list(box)[:4]
            try:
                context.tasker.controller.post_click(
                    int(bx) + int(bw) // 2, int(by) + int(bh) // 2
                ).wait()
                finish_mouse_operation(context.tasker.controller)
            except Exception as exc:               # noqa: BLE001
                _log(f"点「结束冒险」失败（{type(exc).__name__}: {exc}），跳过，直接收工")
                return False
            STATE.result_clicked = True
            _log(f"结算面板：点「{text.strip()}」")
            return True
    return False


def _log_result(context: Context, question: str, mark: str) -> None:
    """结算面板的统一收尾日志：对账、上帝模式对比，整局只收尾一次。

    结算画面每帧都会被识别到，pipeline 也可能多绕几圈，
    所以第一件事就是拦重复调用 —— 否则「识别到冒险失败」会刷一串。
    """
    # 两个节点（结算 click / 结算确认 check）会同时进来，单靠 finished 布尔量
    # 会在检查与置位之间被另一个线程钻过去，所以用锁把「判断+置位」做成原子操作。
    with _RESULT_LOCK:
        if STATE.finished:
            return
        STATE.finished = True
    managed_checkpoint = getattr(STATE, 'management_checkpoint', None)
    if managed_checkpoint is not None:
        from script_task import write_status
        write_status(managed_checkpoint, 'success', 'adventure_finished')
    if STATE.score_by_q:
        mine = STATE.total_score()
        hit = re.search(r"获得\s*(\d+)\s*积分", question or "")
        if hit:
            real = int(hit.group(1))
            tail = "一致" if real == mine else f"差 {real - mine:+d}"
            _log(f"本局结束：实测 {real} 分｜预计 {mine} 分（{tail}）")
        else:
            _log(f"本局结束：预计 {mine} 分（{len(STATE.score_by_q)} 题）")
    # 题序按题号顺序展平：stage_seen 是 {阶段: [事件名,...]}，
    # 阶段按 1..5 从小到大拼起来就是本局已答题目的真实顺序。
    answered = []
    _seen = getattr(STATE, "stage_seen", None) or {}
    if isinstance(_seen, dict):
        for _st in sorted(_seen):
            answered.extend(_seen[_st])
    else:
        answered = list(_seen)
    god_line = ""
    if answered and not getattr(STATE, "god_logged", False):
        STATE.god_logged = True
        try:
            if len(answered) < 10:
                god_line = f"上帝视角　本局只答了 {len(answered)} 题，无法对比"
            else:
                got = logic.seq_plan(_events(), answered,
                                     STATE.initial_attrs or STATE.attrs,
                                     0, answered[0])
                if got and got[1] is not None:
                    god = float(got[1])
                    mine2 = STATE.total_score()
                    god_line = (f"上帝视角　最优 {god:.0f} 分｜本局 {mine2} 分"
                                f"｜差距 {mine2 - god:+.0f} 分")
                else:
                    god_line = "上帝视角　本局题序不完整，无法对比"
        except Exception as exc:                   # noqa: BLE001
            god_line = f"上帝视角　计算失败（{type(exc).__name__}: {exc}）"
        if god_line:
            _log(god_line)
    panel = (f"**本局结束**：识别到「{mark}」｜预计 {STATE.total_score()} 分\n\n"
             f"六维　{'　'.join(f'{k} {v}' for k, v in STATE.attrs.items())}")
    if god_line:
        panel += "\n\n" + god_line
    _panel(panel)
    _log(f"识别到「{mark}」，本局结束，任务收工")


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
        STATE.management_checkpoint = None
        if param.get('management_checkpoint') is not None:
            from script_task import _new_state, write_status
            checkpoint = _new_state(param['management_checkpoint'])
            write_status(checkpoint, 'running', 'adventure_round')
            STATE.management_checkpoint = checkpoint
        attrs: Dict[str, int] = {}
        for name in logic.ATTRS:
            value = _to_int(param.get(name))
            if value is not None:
                attrs[name] = value
        STATE.attrs = attrs
        # 新的一局：题号归零、局内状态清空（归纳表**不动**，见下）
        STATE.question = 0
        STATE.stage_seen = {}
        if _hook is not None:
            try:
                _hook.reset()
            except Exception:                 # noqa: BLE001
                pass
        STATE.strategy_logged = False
        STATE.last_progress_ts = time.monotonic()
        STATE.idle_warned = False
        STATE.character = ""
        STATE.char_checked = False
        STATE.initial_attrs = {}
        STATE.table_cost = 0.0
        STATE.score_by_q = {}
        STATE.finished = False
        STATE.last_answer = 0
        STATE.result_clicked = False
        # 版本标记：用来确认 agent 加载的是改过的代码（改完文件不重启 agent
        # 会一直跑旧模块，日志里看不出区别，全靠这一行区分）
        _log(f"agent 代码版本：{AGENT_VERSION}")
        # 归纳表现在整表常驻（不再按局清空）：每个角色只付一次建表钱，
        # 整表还会落盘到 debug/exact_cache 供跨进程复用（第二次起 0.8 秒载入）；
        # 默认开启「阶段内不放回」，所以单角色 75~163 万状态、首次建表 74~80 秒。
        # 之后无论随机到谁都直接命中缓存 —— 这里把常驻规模打进日志，方便看「拉表工作量」。
        resident, _nodes = logic.exact_stats()
        if resident:
            _log(f"复用上一局留下的归纳表：{resident:,} 状态（本局不再重建）")
        if attrs:
            _log(f"账本初始化（来自输入项）：{attrs}")
        else:
            _log("账本为空：本局六维完全以界面 OCR 识别为准")
        return True


# --------------------------------------------------------------------------- #
# 读题 + 决策
# --------------------------------------------------------------------------- #
@AgentServer.custom_recognition("adventure_result")
class AdventureResult(CustomRecognition):
    """结算面板处理：先点「结束冒险」，再认「冒险成功 / 冒险失败」收工。

    `custom_recognition_param` 里的 `phase` 决定这一轮干哪件事：
    - `click`：认出「结束冒险」按钮 → 把它的框交给 pipeline 点掉
    - `check`：认出「冒险成功 / 冒险失败 / 直降为0」→ 记面板，由 pipeline 收工
    """

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        try:
            # binding 给的是 JSON 字符串（见 _as_dict 的说明），这层不解析的话
            # phase 会永远落到 "check"：结算节点该走 click 短路却变成 check，
            # 收工后每轮都 OCR 一次大 ROI 再判失败 → 第十题之后原地空转。
            param = _as_dict(getattr(argv, "custom_recognition_param", None))
            phase = (param.get("phase") or "check") if param else "check"

            # 大 ROI 的 OCR 很贵，每轮都跑会拖慢整局：两道前置闸门
            # 收尾后只剩「点结束冒险 + 停任务」该做：click 阶段不用再跑 OCR，
            # 但必须返回「命中」把 pipeline 送到下一节点
            # 「结算确认」，由它执行 StopTask 正常收工。
            if STATE.finished and phase == "click":
                # 这里千万不能 return None：返回 None 只是「识别失败」，而「结算」
                # 是「答题循环」next 里的候选之一，候选失败不会触发它自己的
                # on_error（on_error 只对正式执行的节点生效），只会回头重试
                # 答题循环 → 整局原地空转不动（实测 02:12:11 收尾后 36 秒无响应）。
                # box 给左上角黑边，Click 点上去没有任何副作用。
                return CustomRecognition.AnalyzeResult(
                    box=(1, 1, 3, 3),
                    detail={"phase": "click", "skipped": True},
                )
            # stage_seen 是 {阶段: [事件名,...]}，len() 只有阶段数（最多 5），
            # 要的是「已答题数」→ 把各阶段列表长度加起来。
            seen = getattr(STATE, "stage_seen", None) or {}
            done = sum(len(v) for v in seen.values()) if isinstance(seen, dict) else len(seen)
            if done < 9:
                return None

            image = getattr(argv, "image", None)
            pairs = _texts_and_boxes(_ocr_detail(context, image, RESULT_NODE)) or []
            if not pairs:
                return None
            joined = " ".join(t.strip() for t, _ in pairs if t and t.strip())

            if phase == "click":
                if STATE.result_clicked:
                    return None
                for text, box in pairs:
                    if "结束冒险" in (text or "") and box:
                        STATE.result_clicked = True
                        _log(f"结算面板：点「{text.strip()}」")
                        return CustomRecognition.AnalyzeResult(
                            box=tuple(box),
                            detail={"phase": "click", "text": text[:20]},
                        )
                return None

            mark = next((m for m in _CHECK_MARKS if m in joined), "")
            if not mark:
                hit = re.search(r"获得\s*\d+\s*积分", joined)
                mark = hit.group(0) if hit else ""
            if not mark:
                return None
            _log_result(context, joined, mark)
            return CustomRecognition.AnalyzeResult(
                box=(0, 0, 1, 1),
                detail={"phase": "check", "mark": mark, "text": joined[:60]},
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"结算识别异常，按未命中处理：{type(exc).__name__}: {exc}")
            return None


@AgentServer.custom_recognition("adventure_read")
class AdventureRead(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # 任何异常都不该让整个任务崩掉：记录后按「未命中」处理（节点会走 on_error）
        try:
            return self._analyze(context, argv)
        except Exception as exc:  # noqa: BLE001
            _log(f"识别异常，按未命中处理：{type(exc).__name__}: {exc}")
            # 打出完整栈：只有异常消息时根本看不出是哪一行崩的
            import traceback

            for line in traceback.format_exc().rstrip().splitlines():
                _log("  " + line)
            return None

    def _analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        managed_checkpoint = getattr(STATE, 'management_checkpoint', None)
        if managed_checkpoint is not None and managed_checkpoint.get('status') == 'fatal':
            return None
        # 空转提示：游戏崩溃/断线回登录页时，这里会一轮轮空跑。
        # 只打一行日志方便定位，不改任何流程（是否退出仍由结算节点决定）。
        _now_ts = time.monotonic()
        if STATE.last_progress_ts and not STATE.idle_warned:
            _idle = _now_ts - STATE.last_progress_ts
            if _idle > 180:
                STATE.idle_warned = True
                _log(f"已经 {int(_idle)} 秒没有进展，可能已离开答题界面（游戏卡住/断线？）")
                managed_checkpoint = getattr(STATE, 'management_checkpoint', None)
                if managed_checkpoint is not None:
                    from script_task import write_status
                    write_status(managed_checkpoint, 'fatal', 'adventure_stalled',
                                 message='答题连续三分钟无进展，停止并保留当前局；不自动重启。')
                    return None

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
            STATE.last_progress_ts = time.monotonic()
            STATE.idle_warned = False
            # 自己点掉，然后返回 None（识别失败）。
            # 不能返回 box：那样「读题」会再成功一次，focus 就把 panel.md
            # 再贴一条 —— 内容要么是上一条决策、要么是空行（都算重复输出）。
            # 万一点失败也不怕：on_error 回到答题循环后，「继续冒险」节点
            # 还有 TemplateMatch + Click 兜底。
            try:
                _bx, _by, _bw, _bh = CONTINUE_BOX
                context.tasker.controller.post_click(
                    int(_bx) + int(_bw) // 2, int(_by) + int(_bh) // 2
                ).wait()
                finish_mouse_operation(context.tasker.controller)
            except Exception as _exc:        # noqa: BLE001
                _log(f"点「继续冒险」失败（{type(_exc).__name__}: {_exc}），交给 pipeline 兜底")
            return None

        # 账本优先：点完选项后属性面板刷新比截图慢，读界面会拿到上一题的旧值，
        # 于是「明明够门槛却判成必然失败」——实测因此少算 5 分（魅力 6 被读成 4）。
        # 界面 OCR 只在账本为空（本局第一题）时兜底，那时刚进场、数值是静止的。
        if STATE.attrs:
            attrs = dict(STATE.attrs)
        else:
            attrs = _read_attrs(context, image) or {}
        STATE.attrs = dict(attrs)

        option_texts = _read_options(context, image)
        if not _looks_like_question(question, option_texts):
            _log(
                "不像答题界面（题干或选项不符），不做任何点击"
                f"｜题干：{question[:40]}｜选项：{option_texts}"
            )
            # 结算画面也走这条分支（「获得 N 积分」/「直降为0，冒险失败」）。
            # 认到就先点掉「结束冒险」，记收尾日志，然后返回 None 让 pipeline
            # 接着走「结算」→「结算确认」→ StopTask 正常收工。
            # 注意：**不能用 tasker.post_stop()** —— 从识别回调里停任务，pipeline
            # 会以 FAILED 结束，MFA 界面上就是「任务运行失败！」（实测 02:08:48）。
            mark = _result_mark(question)
            if mark:
                _click_end_adventure(context, image)
                _log_result(context, question, mark)
            return None

        event = _match_event(question) or _match_event_by_options(option_texts)
        if event:
            # 题号优先由「事件属于哪个阶段、在该阶段排第几个」反推：
            # 点击落不到新题上时同一题会被再答一次，用决策次数推题号会一路偏高，
            # 之后每一题的价值评估都偏后一个阶段（实测代价很大）。
            # 先确保题库索引建好：第 1 题时它还是空的，question_for_event 会
            # 返回 None，事件也就登记不进 stage_seen（结算时上帝视角只算到 9 题）。
            logic.ensure_exact_index(_events())
            derived = logic.question_for_event(event, STATE.stage_seen)
            if derived is None:
                derived = STATE.question  # 题库元信息不全时的兜底
            STATE.question = derived + 1
            # 同一题刚点过、界面还没切走 → 只回报一个落在左上角黑边的空点击，不再决策。
            # 上限见 _REPEAT_LIMIT：界面切换慢的时候靠它撑住，别提前重新决策。
            _key = (derived, event)
            if _key == STATE.clicked_key and STATE.repeat_count < _REPEAT_LIMIT:
                STATE.repeat_count += 1
                # 静默跳过：不再逐次打日志（一题会刷十几条，把决策行冲掉）。
                # 只在超过上限、真要重新点的时候留一行提示。
                # 返回 None = 识别失败：节点不产生 Action.Succeeded，
                # 也就不会触发 focus 把同一条内容再贴进面板（去重靠这里）。
                return None
            if _key != STATE.clicked_key:
                STATE.clicked_key = _key
                STATE.repeat_count = 0
            # 开局（第 1 题）：先用「角色」锁死初始六维，再拉表 ——
            # 初值对了，归纳表才只展开这一局真正会走到的状态，不做无用功。
            attrs = _opening_attrs(context, image, attrs, derived)
            STATE.attrs = dict(attrs)
            # 第 1 题这一刻的六维就是本局初值，上帝模式算最优解要用
            if not STATE.initial_attrs:
                STATE.initial_attrs = dict(attrs)
            # 交给本地扩展钩子（如有）：由它决定是否提供一份「后续题目预知」，
            # 并自行承担校验责任。没有钩子就是空列表，走按阶段策略。
            seq: List[str] = []
            if event and _hook is not None:
                try:
                    seq = _hook.observe(event, derived) or []
                except Exception as exc:          # noqa: BLE001
                    _log(f"本地扩展模块出错（{exc}），按阶段策略走")
                    seq = []
            # 传入题号，走精确后向归纳（按阶段 + 阶段内不放回）。
            # 归纳表按角色常驻：接入不放回后单角色就要 85~149 万状态，
            # 7 个角色装不下，所以换角色时才清表（同一角色连续多局复用）。
            if logic.maybe_reset_exact_memo(STATE.character):
                _log(f"换角色（{STATE.character}），归纳表已重置")
            _t0 = time.time()
            decision = logic.choose(
                _events(),
                event,
                attrs,
                question=derived,
                answered=STATE.stage_seen,
                seq=seq or None,
            )
            _cost = time.time() - _t0
            STATE.table_cost = _cost
            STATE.attrs = _apply(attrs, decision.gains)
            if derived == 0 and not STATE.strategy_logged:
                STATE.strategy_logged = True
                states, _nodes = logic.exact_stats()
                _log(
                    f"开局：角色={STATE.character or '未识别'}"
                    f"｜第 1 题={event}｜初值={' '.join(f'{k}{v}' for k, v in attrs.items())}"
                )
                _log(
                    f"本局策略：{_plan_label(decision)}"
                    f"（拉表耗时 {_cost:.1f}s，归纳表 {states:,} 状态，"
                    f"EXACT_ENABLED={logic.EXACT_ENABLED}）"
                )
            _log(f"{decision.describe()}｜题干：{question}")
            # 本题得分：题库 `_meta.stages` 的固定值（成功 6/6/9/12/19，
            # 失败 3/3/4/6/9）。用题号索引的字典存，重复点击同一题不会重复计分。
            gained = logic.question_score(derived, decision.will_succeed)
            STATE.score_by_q[derived] = gained
            total = STATE.total_score()
            # brief 是一行中文摘要：detail 的结构化字段在 maafw.log 里
            # 是一坨 JSON，加一行才好读（面板展示走 focus，不走这里）。
            detail = {
                "brief": (
                    f"第{derived + 1}题 {event} → 第{decision.index + 1}项「{decision.text}」"
                    f"（{'必定成功' if decision.will_succeed else '必然失败'}，"
                    f"+{gained} 分，累计 {total} 分）"
                    f"｜{decision.reason}"
                ),
                "event": event,
                "question": derived + 1,
                "option": decision.index + 1,
                "text": decision.text,
                "will_succeed": decision.will_succeed,
                "gained": gained,
                "total": total,
                "gains": decision.gains,
                "reason": decision.reason,
                "options_ocr": option_texts,
            }
            _delta = "、".join(
                f"{k}{v:+d}" for k, v in (decision.gains or {}).items()
                if isinstance(v, int) and v
            )
            # 本地扩展钩子（如有）提供的额外一行，贴到面板上
            _order = ""
            if _hook is not None:
                try:
                    _order = _hook.panel_text(derived)
                except Exception:             # noqa: BLE001
                    _order = ""
            _panel(
                f"**第 {derived + 1} 题** {event} → 第 {decision.index + 1} 项"
                f"「{decision.text}」\n\n"
                f"{'必定成功' if decision.will_succeed else '必然失败'}"
                f"　+{gained} 分　累计 {total} 分\n\n"
                f"六维　{'　'.join(f'{k} {v}' for k, v in STATE.attrs.items())}"
                + (f"　（本题 {_delta}）" if _delta else "")
                + _order
            )
            index = decision.index
            STATE.clicked_key = (derived, event)
            STATE.repeat_count = 0
            STATE.last_progress_ts = time.monotonic()
            STATE.idle_warned = False
        else:
            _collect(question, attrs, option_texts, image)
            # 未知事件（通常是选项 OCR 截断或新事件）一律不点击：
            # 题库已覆盖 41 个事件，识别失败时乱点的代价（可能扣 9 点健康）远大于中断一次
            _log("未知事件：已记录题干与选项，但为安全起见不做任何点击")
            return None

        box = boxes[index]
        # 先自己点一次，pipeline 的 Click 随后还会点一次：
        # 第 1 题（刚进答题界面）和转场后的那一题，游戏常把第一次点击吞掉，
        # 只点一次就会「跳过 8 次 → 重新决策 → 又贴一条面板」。
        try:
            _cx = int(box[0]) + int(box[2]) // 2
            _cy = int(box[1]) + int(box[3]) // 2
            context.tasker.controller.post_click(_cx, _cy).wait()
            finish_mouse_operation(context.tasker.controller)
        except Exception as _exc:            # noqa: BLE001
            _log(f"补点失败（{type(_exc).__name__}: {_exc}），交给 pipeline 点")
        # AnalyzeResult.detail 必须是 dict：binding 内部会自己 json.dumps
        # 见 source/binding/Python/maa/custom_recognition.py
        return CustomRecognition.AnalyzeResult(
            box=(int(box[0]), int(box[1]), int(box[2]), int(box[3])),
            detail=detail,
        )
