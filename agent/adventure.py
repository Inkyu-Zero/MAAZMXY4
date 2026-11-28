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

from datetime import datetime
import json
import os
from rolling_log import append_log
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
# 给 GUI 日志窗看的提示文件（pipeline 的 focus 只认文件内容）
DAILY_DONE_FILE = os.path.join(LOG_DIR, "daily_done.md")
VOYAGE_DONE_FILE = os.path.join(LOG_DIR, "voyage_done.md")
COLLECTED_PATH = os.path.join(LOG_DIR, "collected.jsonl")
LOG_PATH = os.path.join(LOG_DIR, "adventure.log")

QUESTION_NODE = "生存大冒险_识别题干"
# 改动 agent 代码后改这个值，日志里就能确认「agent 到底加载了哪一版」
AGENT_VERSION = "2026-10-29-managed-boundary"
CHARACTER_NODE = "生存大冒险_角色"
ATTR_NODE_FMT = "生存大冒险_属性_{}"
OPTION_NODE_FMT = "生存大冒险_选项_{}"
# 4 行选项在 1280×720 基准下的 ROI（与 pipeline 里 生存大冒险_选项_N 一致）。
# 只在「整条重读」时用作 pipeline_override 的 roi，正常情况下直接跑节点即可。
_OPTION_ROI = {
    1: [375, 337, 410, 44],
    2: [375, 407, 410, 44],
    3: [375, 476, 410, 44],
    4: [375, 546, 410, 44],
}
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
        append_log(LOG_PATH, line + "\n")
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
    """读 4 行选项文本。

    实测过一次读错：第一行正文「送唐僧去看大夫」只拿到一个字「厦」。用同一帧的重放
    验证过，固定 ROI 本身没问题（位置、清晰度都对，四种 ROI/only_rec 组合都能读对），
    所以原因不是坐标，而是那一帧的 OCR **返回了多段、而代码只取了第一段**——第一段
    就是行首那个圆形项目符号被识别成一个字。

    对策三条，都不改变点击位置（点击框由 `_locate_options` 另外自适应算）：
      1. 取回全部文本，**选最长的一段**（选项正文一定比符号长）；
      2. 若最长的一段仍然只有 1 个字，用 `only_rec=True` 把这个 ROI 整条再读一次
         （跳过检测阶段，避免符号被单独框出来）；
      3. 多段或重读时打日志，便于下次复核。
    """
    out: List[str] = []
    for i in range(1, 5):
        node = OPTION_NODE_FMT.format(i)
        texts = _ocr(context, image, node, all_texts=True)
        good = [t for t in texts if t and len(t.strip()) >= 2] or list(texts)
        picked = max(good, key=len) if good else ""
        if len(picked.strip()) < 2:
            retry = _ocr(
                context,
                image,
                node,
                all_texts=True,
                override={
                    node: {
                        "recognition": {
                            "type": "OCR",
                            "param": {"roi": _OPTION_ROI[i], "only_rec": True},
                        }
                    }
                },
            )
            if retry:
                cand = max(retry, key=len)
                if len(cand.strip()) > len(picked.strip()):
                    picked = cand
            _log(f"选项{i} 只读到「{picked}」，已用 only_rec 重读")
        if len(texts) > 1:
            _log(f"选项{i} 识别到多段 {texts} -> 取最长「{picked}」")
        out.append(picked)
    return out


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


_RESULT_MARKS = ("冒险成功", "冒险失败")

# 结算识别专用节点（pipeline 里 ROI 放大到题干+选项+按钮）
RESULT_NODE = "生存大冒险_结算识别"
# check 阶段只认「真的结算了」（冒险成功/冒险失败），不能含「结束冒险」——
# 那只是按钮文字，按钮还在时把它当结算标记会提前收工。
_CHECK_MARKS = ("冒险成功", "冒险失败")


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
    return ""


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


# 顶部进度条：10 个圆点，已答过的是亮金色，当前那一格被角色头像盖住（紫色），
# 没答的是暗棕色。坐标按 1147x720 量得，用比例存，跟随窗口缩放。
_DOT_X0 = 355.0 / 1147.0
_DOT_STEP = 71.7 / 1147.0
_DOT_Y = 150.0 / 720.0
_DOT_COUNT = 10


def _find_close_button(image):
    """在右上角找关闭按钮，返回 [(x, y), ...]，按红色像素多少排序。

    生存大冒险有两层弹窗，两个叉位置完全一样。返回多个候选，调用方挨个点。
    """
    shape = getattr(image, "shape", None)
    if shape is None or len(shape) < 2:
        return []
    height, width = int(shape[0]), int(shape[1])
    cell = 10
    counts = {}
    x_lo, x_hi = int(width * 0.76), int(width)
    y_lo, y_hi = 0, int(height * 0.30)
    for y in range(y_lo, y_hi, 2):
        row = image[y]
        for x in range(x_lo, x_hi, 2):
            pixel = row[x]
            if int(pixel[2]) > 170 and int(pixel[1]) < 85 and int(pixel[0]) < 85:
                counts[(y // cell, x // cell)] = counts.get((y // cell, x // cell), 0) + 1
    if not counts:
        _log("红叉：右上角没有红色像素")
        return []
    points = []
    for key, hits in sorted(counts.items(), key=lambda kv: -kv[1]):
        if hits < 20:
            break
        top, left = key[0] * cell, key[1] * cell
        sum_x = sum_y = total = 0
        for y in range(max(0, top - cell), min(height, top + 2 * cell)):
            row = image[y]
            for x in range(max(x_lo, left - cell), min(x_hi, left + 2 * cell)):
                pixel = row[x]
                if int(pixel[2]) > 170 and int(pixel[1]) < 85 and int(pixel[0]) < 85:
                    sum_x += x
                    sum_y += y
                    total += 1
        pt = (sum_x // total, sum_y // total) if total else (left + cell // 2, top + cell // 2)
        if any(abs(pt[0] - q[0]) < 20 and abs(pt[1] - q[1]) < 20 for q in points):
            continue
        points.append(pt)
        if len(points) >= 2:
            break
    _log("红叉：找到 %d 个候选 %s" % (len(points), points))
    return points


def _question_from_progress(image) -> int:
    """数进度条上亮金色的圆点 = 本局已经答完几题（也就是当前题号，0 基）。

    中途接管时 pipeline 没法知道用户停在第几题，题号猜错会让之后每题都按错的
    属性需求去选，所以这里直接从界面读数。

    只认「金色圆点」，不认角色头像：当前那一格会被头像盖住，盖住之后就不算
    金色了，所以金色个数天然等于已答题数。头像长什么样（兔子/别的角色、什么
    颜色）都不影响。
    """
    if image is None:
        return 0
    try:
        height, width = int(image.shape[0]), int(image.shape[1])
    except Exception:                          # noqa: BLE001
        return 0
    radius = max(2, int(round(width * 0.006)))
    done = 0
    for i in range(_DOT_COUNT):
        cx = int(round((_DOT_X0 + i * _DOT_STEP) * width))
        cy = int(round(_DOT_Y * height))
        gold = total = 0
        for y in range(cy - radius, cy + radius + 1):
            if y < 0 or y >= height:
                continue
            for x in range(cx - radius, cx + radius + 1):
                if x < 0 or x >= width:
                    continue
                px = image[y, x]
                b, g, r = int(px[0]), int(px[1]), int(px[2])
                total += 1
                if r > 150 and g > 100 and b < 120:
                    gold += 1
        if total and gold * 2 > total:
            done += 1
    return max(0, min(done, _DOT_COUNT))


_ENTRY_RETRY = {}


def _entry_retry(entry_node: str) -> int:
    """计一下这个入口短时间内被调了几次。

    入口重试是正常循环（每轮只挪一步），但动作没生效时
    会一直回来。超过 60 秒算新一轮，避免上一局的计数污染这一局。
    """
    now = time.time()
    count, ts = _ENTRY_RETRY.get(entry_node, (0, 0.0))
    if now - ts > 60.0:
        count = 0
    count += 1
    _ENTRY_RETRY[entry_node] = (count, now)
    return count


def _find_toolbar_arrow(image):
    """在右上角工具栏里找展开/收起菜单的三角箭头，返回 (x, y) 或 None。

    之前是按 (0.750w, 0.076h) 硬算一个点，屏幕最左边
    还有一个圆形黄色按钮形状也像箭头。这里只在右上角
    那一块里做模板匹配，找不到就不点。
    """
    if image is None or getattr(image, "shape", None) is None:
        return None
    try:
        import cv2 as _cv2
        import numpy as _np
        tpl_path = os.path.join(_ROOT, "resource", "base", "image", "survival", "arrow.png")
        tpl = _cv2.imdecode(_np.fromfile(tpl_path, dtype=_np.uint8), _cv2.IMREAD_COLOR)
    except Exception as error:                        # noqa: BLE001
        _log("入口：读箭头模板失败 %s" % error)
        return None
    if tpl is None:
        return None
    h, w = image.shape[:2]
    x0, y0 = int(round(w * 0.70)), int(round(h * 0.0))
    x1, y1 = int(round(w * 0.80)), int(round(h * 0.12))
    sub = image[y0:y1, x0:x1]
    if sub.size == 0:
        return None
    best_score, best_pt = 0.0, None
    for scale in (0.85, 0.9, 1.0, 1.1):
        tw, th = int(round(tpl.shape[1] * scale)), int(round(tpl.shape[0] * scale))
        if tw < 8 or th < 8 or tw > sub.shape[1] or th > sub.shape[0]:
            continue
        small = _cv2.resize(tpl, (tw, th), interpolation=_cv2.INTER_AREA)
        res = _cv2.matchTemplate(sub, small, _cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = _cv2.minMaxLoc(res)
        if score > best_score:
            best_score = score
            best_pt = (x0 + loc[0] + tw // 2, y0 + loc[1] + th // 2)
    _log("入口：工具栏箭头 best=%.3f at %s" % (best_score, best_pt))
    if best_score < 0.70 or best_pt is None:
        return None
    return best_pt


@AgentServer.custom_action("adventure_entry_route")
class AdventureEntryRoute(CustomAction):
    """定位一次当前界面，直接把入口的 next 改到对应分支。

    原来入口把 4 个判断节点挨个试，每个都要等自己的超时，停在大地图也要
    十几秒。导航运行时本来就是「一帧图逐项识别、命中即返回」，这里直接用它
    的 locate()，再把结果写成入口的下一个节点，省掉全部试探。
    """

    ROUTE = {
        "map": "生存大冒险_展开菜单",
        "survival_week": "生存大冒险_开始冒险",
        "survival_question": "生存大冒险_接管",
    }

    def run(self, context, argv) -> bool:
        from script_task import capture  # noqa: PLC0415

        # 这个入口是通用的：pipeline 用 custom_action_param 告诉它"点开菜单后
        # 该去点哪个图标节点"。生存大冒险、航海寻宝共用同一套红叉/箭头/
        # 世界地图/返回地图的判断。
        # binding 给的是 JSON 字符串（见 _as_dict 的说明），直接走统一的解析。
        conf = _as_dict(getattr(argv, "custom_action_param", None))
        # 整套入口判断（红叉 / 箭头 / 世界地图 / 回地图 / 返回地图）都是通用的，
        # pipeline 用 custom_action_param 指定自己那一份节点名。
        entry_node = str(conf.get("entry_node") or "生存大冒险_入口")
        # 入口是「一步一步往目标挪」的循环点：每轮只做一个动作，做完回来重新判。
        # 一直回来就说明动作没生效，超过 10 轮就放弃，别无限点下去。
        retry = _entry_retry(entry_node)
        if retry > 10:
            _log("入口已重试 %d 轮仍没到目标，放弃" % retry)
            return False
        _log("入口第 %d 轮判断" % retry)
        icon_node = str(conf.get("icon_node") or "生存大冒险_点图标")
        map_node = str(conf.get("map_node") or "生存大冒险_回地图")
        back_node = str(conf.get("back_node") or "生存大冒险_返回地图")
        settings_node = str(conf.get("settings_node") or "生存大冒险_点设置")
        big_map_node = str(conf.get("big_map_node") or "生存大冒险_识别大地图")
        arrow_node = str(conf.get("arrow_node") or "生存大冒险_展开菜单")

        # 先抓一帧看右上角有没有红叉：有就是活动面板/弹窗界面，直接点掉最省事，
        # 不用去跑候选界面的识别。红叉用模板匹配（红色 X + 橙边 + 黑圈），
        # 不再按「红色像素最密」猜——那样会把工具栏的红色图标当成叉。
        image = None
        try:
            image = capture(context)
        except Exception as error:                      # noqa: BLE001
            _log("入口截图失败：" + str(error))
        close_node = str(conf.get("close_node") or "生存大冒险_识别红叉")
        close_click = str(conf.get("close_click_node") or "生存大冒险_点红叉")
        # 通用导航节点做完事要回本任务入口，否则任务会被当成走完
        self._return_home(context, entry_node, settings_node, back_node, close_click)
        # 先看是不是已经停在目标界面上了。这些界面的右上角都有红叉，
        # 要是先判红叉会把它点掉，白跑一圈重新点进来。
        # panels 是 [{node, target}] 的有序列表，逐个识别，命中就用它的 target 当 next。
        panels: List[Dict[str, Any]] = []
        raw_panels = conf.get("panels")
        if isinstance(raw_panels, list):
            panels = [item for item in raw_panels if isinstance(item, dict)]
        elif conf.get("panel_node"):
            panels = [{"node": conf.get("panel_node"),
                       "target": conf.get("panel_target")}]
        if panels and image is not None:
            for item in panels:
                node = str(item.get("node") or "")
                if not node:
                    continue
                try:
                    panel = context.run_recognition(node, image)
                except Exception as error:              # noqa: BLE001
                    _log("面板识别失败（%s）：%s" % (node, error))
                    continue
                if getattr(panel, "hit", False):
                    panel_target = str(item.get("target") or node)
                    _log("入口：已在「%s」界面，直接继续" % node)
                    return self._route(context, entry_node, "已在目标界面", panel_target)

        # 结算页优先：「返回地图」只在挑战成功/失败的结算页上出现，判据唯一。
        # 放最前面是因为结算页上没有红叉、没开菜单、没有无双和世界图标，
        # 走后面的分支只会空转到 _entry_retry 上限然后任务失败。
        back = str(conf.get("back_node") or "")
        if back and image is not None:
            try:
                found = context.run_recognition(back, image)
                if getattr(found, "hit", False) and getattr(found, "box", None):
                    _log("入口：结算页，点返回地图")
                    _click_box(context, found.box)
                    time.sleep(0.8)
                    return self._route(context, entry_node, "结算页点返回地图", entry_node)
            except Exception as error:                  # noqa: BLE001
                _log("返回地图识别失败：" + str(error))


        # 菜单已经展开时（点过箭头但没点图标），画面上就有「福果来」那行字，
        # 直接交给「点图标」，省掉再点一下箭头把菜单关掉的来回。
        if image is not None:
            try:
                found = context.run_recognition(close_node, image)
                if getattr(found, "hit", False) and getattr(found, "box", None):
                    # 自己点，不走 pipeline 的 close_click：那个节点是
                    # adventure_close_x，也要再取一帧，来这里已经有一帧了。
                    _log("入口：识别到红叉，直接点掉")
                    _click_box(context, found.box)
                    time.sleep(0.8)
                    return self._route(context, entry_node, "点了红叉", entry_node)
            except Exception as error:                  # noqa: BLE001
                _log("红叉识别失败：" + str(error))
            # pipeline 的模板识别受截图尺寸影响很大（只要 1147），
            # 这里用颜色直接找一遍兜底：找到就自己点掉，
            # 不再给 pipeline 点一次反应的机会。
            hit_points = _find_close_button(image)
            if hit_points:
                _close_window(context, image)
                _log("入口：颜色兜底点掉红叉 %s" % (hit_points,))
                return self._route(context, entry_node, "颜色点红叉", entry_node)

        menu_node = str(conf.get("menu_node") or "")
        if menu_node and image is not None:
            try:
                opened = context.run_recognition(menu_node, image)
            except Exception as error:                  # noqa: BLE001
                _log("菜单识别失败：%s" % error)
                opened = None
            if getattr(opened, "hit", False):
                _log("入口：菜单已经展开，直接点图标")
                return self._route(context, entry_node, "菜单已展开", icon_node)

        # 已经在副本里（福果来那种没有底部按钮的副本，靠设置退不出去）：
        # 直接认出来，把入口指到副本流程本身，别再折腾导航。
        dungeon = str(conf.get("dungeon_node") or "")
        if dungeon and image is not None:
            try:
                inside = context.run_recognition(dungeon, image)
            except Exception as error:                  # noqa: BLE001
                _log("副本识别失败：%s" % error)
                inside = None
            if getattr(inside, "hit", False):
                _log("入口：已经在副本内，直接开始")
                return self._route(context, entry_node, "在副本内",
                                   str(conf.get("dungeon_target") or icon_node))

        # 没有红叉就只剩两种可能：停在人物大地图（工具栏上有金色箭头），
        # 或者停在别的界面。
        #
        # 这里不再用模板匹配。模板带着裁剪时的背景色，换一张地图分数就掉到
        # 阈值以下（须弥山是浅蓝天空、普通野外是暗红岩石），而地图上别处的
        # 金色图标反而能过线 —— 22:39 那次就误命中了须弥山中间那棵金树
        # (573, 360)。这颗箭头在所有地图上都在同一相对位置：两种窗口实测
        # 都是 (0.750w, 0.076h)，直接按比例点。
        target, reason = map_node, "没认出来"
        if image is not None and getattr(image, "shape", None) is not None:
            height, width = int(image.shape[0]), int(image.shape[1])
            pt = _find_toolbar_arrow(image)
            if pt is None:
                return self._route(context, entry_node, "右上角没看到箭头", settings_node)
            cx, cy = pt
            try:
                context.tasker.controller.post_click(cx, cy).wait()
            except Exception as error:                  # noqa: BLE001
                _log("点箭头失败：" + str(error))
            time.sleep(0.5)
            target, reason = icon_node, "已点开菜单"
        # 停在世界地图（云海）时，南瞻部洲入口就在画面下方。导航库那条
        # Nav_WorldNanzhan 也是点这个框，但它要额外走一遍「等条件就绪 + 点完
        # 再反复确认 3 秒」，实测 4 秒；这里直接识别框中心点掉，随后让
        # 「识别大地图」去确认，失败会自己回退到回地图。
        if target == map_node:
            try:
                town = context.run_recognition("Nav_WorldNanzhan", image)
                box = getattr(town, "box", None) if town is not None else None
                if getattr(town, "hit", False) and box:
                    bx, by, bw, bh = (int(v) for v in box[:4])
                    cx, cy = bx + bw // 2, by + bh // 2
                    _log("入口：在世界地图，点南瞻部洲 (%d, %d)" % (cx, cy))
                    try:
                        context.tasker.controller.post_click(cx, cy).wait()
                    except Exception as error:          # noqa: BLE001
                        _log("点南瞻部洲失败：" + str(error))
                    time.sleep(1.2)
                    target, reason = big_map_node, "世界地图已点南瞻部洲"
            except Exception as error:                  # noqa: BLE001
                _log("世界地图识别失败：" + str(error))
        # 还是认不出来：多半是停在须弥山 / 副本 / 挑战结果页。
        # 挑战成功/失败页上没有齿轮，但页面上本来就有「返回地图」——先认一次，
        # 命中就直接交给「返回地图」节点点掉，省掉「点设置 + 等 1.5 秒」那一趟。
        if target == map_node and image is not None:
            try:
                back = context.run_recognition(back_node, image)
                if getattr(back, "hit", False) and getattr(back, "box", None):
                    target, reason = back_node, "有返回地图"
            except Exception as error:                  # noqa: BLE001
                _log("返回地图识别失败：" + str(error))
        # 没有「返回地图」才点底部齿轮：菜单里的「返回地图」直接回人物大地图
        # （南瞻部洲），副本 / 须弥山走这条。
        if target == map_node:
            target, reason = settings_node, "点设置再返回地图"
        return self._route(context, entry_node, reason, target)

    @staticmethod
    def _return_home(context, entry_node, settings_node, back_node, close_click):
        """把通用导航节点接回本任务入口。

        通用节点（点红叉 / 点设置 / 返回地图）是几个任务共用的，
        它们自己不知道该回哪一个入口。不接回去的话，MAA 执行完
        那一个节点就把任务当成走完、直接返回成功 —— 09:00 那次
        「点设置」一跑完任务就 Succeeded 就是这个原因。
        """
        overrides = {}
        for node_name in (close_click, settings_node, back_node):
            if node_name:
                overrides[node_name] = {"next": [entry_node]}
        if not overrides:
            return
        try:
            done = context.override_pipeline(overrides)
            _log("入口：通用节点接回 %s（%s）"
                 % (entry_node, "成功" if done else "失败"))
        except Exception as error:                      # noqa: BLE001
            _log("入口：接回失败 %s" % error)


    @staticmethod
    def _route(context, entry_node, why, target):
        done = context.override_pipeline({entry_node: {"next": [target]}})
        _log("入口定位：%s → %s（覆盖%s）" % (why, target, "成功" if done else "失败"))
        return True


@AgentServer.custom_action("adventure_goto_map")
class AdventureGotoMap(CustomAction):
    """从任意界面走回人物大地图：复用项目自带的 navigation 运行时。"""

    def run(self, context, argv) -> bool:
        from navigation import NavigationError
        from navigation.catalog import task_routine
        from navigation.runtime import Navigator
        from script_task import _check, capture

        parameters = {"navigation": {"kind": "screen", "screen": "map"}}
        try:
            routine = task_routine(context, parameters)
        except ValueError as error:
            _log("导航初始化失败：" + str(error))
            return False
        # 只回地图，不需要反复确认界面；压低探测次数和每次操作后的等待。
        # locate_attempts=1：认不出来就立刻走"关窗口"兜底，不再白跑第二轮。
        routine["locate_attempts"] = 1
        routine["settle_ms"] = 200
        # quick 里已经带了 map / world / 暂停 / 设置这几个，这里只补两个
        # 最常停留、又能直接走官方出口的界面。每多一个候选就多 140ms，
        # 42 个候选要 6 秒，7 个候选只要 1 秒。
        routine["fallback"] = ["world", "settings_menu"]
        # 导航库默认做完动作要「反复截图确认画面变了」，最长 10 秒（verify_ms）
        # 或 10 秒（timeout_ms）。回地图这种操作确认个 3 秒足够，超了就按
        # "没确认"处理、交给下一轮重新定位，比干等 10 秒划算。
        for action in routine.get("actions", {}).values():
            action["verify_ms"] = min(int(action.get("verify_ms", 10000)), 3000)
            action["timeout_ms"] = min(int(action.get("timeout_ms", 10000)), 3000)

        def build():
            return Navigator(
                context,
                routine,
                capture=lambda: capture(context),
                check=lambda: _check(context, force=False),
                input_check=lambda: _check(context),
                log=lambda message: _log("[导航] " + message),
            )

        last = None
        closed = 0
        for attempt in range(3):
            try:
                current = build().run()
            except NavigationError as error:
                last = error
                _log("导航失败（第 %d 次）：%s" % (attempt + 1, error))
                if attempt == 2 or closed:
                    break
                # 认不出的界面大多是没收录的活动弹窗或副本场景：前者右上角
                # 有红叉，后者点底部设置再点「返回地图」。点完之后直接回入口
                # 重新判断（入口有世界地图、大地图的快捷路径），比在这里再
                # 跑一轮 locate + 世界地图导航快好几秒。只放行一次，避免
                # 点不掉时来回打转。
                closed += 1
                self._close_window(context)
                # 返回 False 让 pipeline 走 on_error：那个节点会认「返回地图」
                # 并点掉（自己在一帧里完成识别+点击，不跨 Python 调用）。
                return False
            _log("导航完成，当前界面：" + str(current))
            return True
        _log("导航回地图失败：" + str(last))
        return False

def _click_box(context, box) -> bool:
    """点识别框中心，用 swipe 而不是 click。

    post_click 在这个环境里只入队、不产生 touch_down，游戏收不到。
    """
    try:
        x, y, w, h = (int(v) for v in list(box)[:4])
    except Exception:                                 # noqa: BLE001
        return False
    cx, cy = x + w // 2, y + h // 2
    try:
        context.tasker.controller.post_swipe(cx, cy, cx, cy, 80).wait()
        _log("点击 (%d, %d)" % (cx, cy))
        return True
    except Exception as error:                        # noqa: BLE001
        _log("点击失败 %s" % error)
        return False


def _safe_capture(context):
    """取一帧，失败返回 None。"""
    try:
        from script_task import capture
        return capture(context)
    except Exception as error:                        # noqa: BLE001
        _log("取帧失败：%s" % error)
        return None


def _same(a, b) -> bool:
    """两帧是不是基本没变（粗比平均差）。"""
    try:
        import numpy as _np
        x = _np.asarray(a, dtype=_np.float32)
        y = _np.asarray(b, dtype=_np.float32)
    except Exception:                                 # noqa: BLE001
        return False
    if x.size == 0 or x.shape != y.shape:
        return False
    return float(_np.abs(x - y).mean()) < 3.0


def _watch_still(context, base, seconds=1.0, gap=0.2) -> bool:
    """连续看 seconds 秒，期间每一帧都和 base 一样才算「界面没变化」。

    只看一眼容易被动画/过场骗过去（弹窗正在关，那一帧还没画完）。
    """
    import time as _t
    deadline = _t.time() + seconds
    while _t.time() < deadline:
        _t.sleep(gap)
        frame = _safe_capture(context)
        if frame is None:
            return False
        if not _same(frame, base):
            return False
    return True


    @classmethod
    def _close_window(cls, context, image=None):
        """点右上角的红叉。点完挪开光标，再看 1 秒界面有没有变。"""
        controller = context.tasker.controller
        if image is None:
            image = getattr(controller, "cached_image", None)
        shape = getattr(image, "shape", None)
        height, width = (int(shape[0]), int(shape[1])) if shape is not None and len(shape) >= 2 else (720, 1147)
        points = _find_close_button(image)
        if not points:
            _log("关闭窗口：没找到红叉")
            return
        import random as _rnd
        for idx, pt in enumerate(points, 1):
            _log("关闭当前窗口：(%d, %d)" % pt)
            # 用 swipe 原地按 80ms 代替 click：post_click 在 MFA 侧只入队、
            # 不产生 touch_down，游戏收不到点击。
            try:
                controller.post_swipe(pt[0], pt[1], pt[0], pt[1], 80).wait()
            except Exception as error:
                _log("关闭窗口点击失败：" + str(error))
            # 点完把光标挪走：两层弹窗的两个叉位置完全一样，
            # 鼠标停在上面会让下面那个一直高亮。
            try:
                spot_x = _rnd.randint(int(width * 0.10), int(width * 0.35))
                spot_y = _rnd.randint(int(height * 0.55), int(height * 0.75))
                controller.post_swipe(spot_x, spot_y, spot_x, spot_y, 80).wait()
            except Exception as error:
                _log("挪光标失败：" + str(error))
            if not _watch_still(context, image, seconds=1.0):
                _log("关闭窗口：界面已变化（第 %d 次）" % idx)
                return
            _log("关闭窗口：第 %d 次没反应" % idx)
        _log("关闭窗口：都试过，界面还是没变")


@AgentServer.custom_action("adventure_click_back")
class AdventureClickBack(CustomAction):
    """点结算页的「返回地图」，点完再检查是否真的回到了地图。

    领完奖励后停在结算页，不点这个按钮任务就算结束，下一个任务
    进来还得再手工收拾一遍。
    """

    def run(self, context, argv) -> bool:
        from script_task import capture
        for attempt in range(1, 4):
            try:
                image = capture(context)
            except Exception as error:                  # noqa: BLE001
                _log("点返回地图取帧失败：" + str(error))
                return False
            try:
                found = context.run_recognition("Adventure_返回地图", image)
            except Exception as error:                  # noqa: BLE001
                _log("返回地图识别失败：" + str(error))
                found = None
            if not getattr(found, "hit", False) or not getattr(found, "box", None):
                _log("点返回地图：第 %d 次没看到按钮，收工" % attempt)
                return True
            _click_box(context, found.box)
            _log("点返回地图：第 %d 次点击" % attempt)
            time.sleep(0.8)
        return True


@AgentServer.custom_action("adventure_close_twice")
class AdventureCloseTwice(CustomAction):
    """生存大冒险结束时的两层弹窗：点红叉 -> 点屏幕中间 -> 再点红叉。

    中间那一下是为了让下面那层弹窗「活过来」——两个叉位置完全一样，
    鼠标停在上一个位置时下层那个一直是高亮态，直接连点会点空。
    """

    def run(self, context, argv) -> bool:
        from script_task import capture
        controller = context.tasker.controller
        for round_no in (1, 2):
            try:
                image = capture(context)
            except Exception as error:                  # noqa: BLE001
                _log("关弹窗第 %d 次取帧失败：%s" % (round_no, error))
                break
            points = _find_close_button(image)
            if not points:
                _log("关弹窗第 %d 次：没找到红叉" % round_no)
                break
            _close_window(context, image)
            _log("关弹窗第 %d 次：点掉红叉 %s" % (round_no, points[0]))
            time.sleep(0.6)
            height, width = image.shape[0], image.shape[1]
            mid_x, mid_y = width // 2, height // 2
            try:
                controller.post_swipe(mid_x, mid_y, mid_x, mid_y, 80).wait()
                _log("关弹窗第 %d 次：点一下中间 (%d, %d)" % (round_no, mid_x, mid_y))
            except Exception as error:                  # noqa: BLE001
                _log("关弹窗点中间失败：%s" % error)
            time.sleep(0.6)
        return True


@AgentServer.custom_action("adventure_close_x")
class AdventureCloseX(CustomAction):
    """点掉右上角的红叉。

    为什么不用节点自带的 Click：Click 走 post_click，在这个环境里只入队、
    不产生 touch_down，游戏收不到；也没法点完把光标挪开。两层弹窗的两个
    叉位置完全一样，光标停在上面会让下面那个一直高亮。
    """

    def run(self, context, argv) -> bool:
        from script_task import capture
        try:
            image = capture(context)
        except Exception as error:                      # noqa: BLE001
            _log("点红叉取帧失败：" + str(error))
            image = None
        AdventureEntryRoute._close_window(context, image)
        return True


@AgentServer.custom_action("adventure_click_settings")
class AdventureClickSettings(CustomAction):
    """点底部齿轮「设置」。

    「返回地图」按钮有两种出现方式：挑战成功/失败页上直接有这一个按钮；
    副本内要先点设置，弹出的设置菜单里才有。所以入口先直接找「返回地图」，
    找不到再来这里点设置。
    """

    def run(self, context, argv) -> bool:
        image = getattr(argv, "image", None)
        shape = getattr(image, "shape", None)
        height, width = (shape[0], shape[1]) if shape is not None and len(shape) >= 2 else (720, 1147)
        box = list(getattr(argv, "box", []) or [])
        if len(box) >= 4 and box[2] > 0 and box[3] > 0:
            x = int(box[0]) + int(box[2]) // 2
            y = int(box[1]) + int(box[3]) // 2
        else:
            x = int(round(width * 0.606))
            y = int(round(height * 0.935))
        _log("没找到「返回地图」，点底部设置 (%d, %d)" % (x, y))
        context.tasker.controller.post_click(x, y).wait()
        # 设置菜单是滑入动画，睡够再交棒，否则「返回地图」那一步会 OCR 落空。
        time.sleep(1.5)
        return True


@AgentServer.custom_action("adventure_takeover")
class AdventureTakeover(CustomAction):
    """用户已经手动进了冒险：把上一轮残留的局内状态清干净，按界面进度条定题号。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        question = _question_from_progress(getattr(argv, "image", None))
        STATE.question = question
        STATE.stage_seen = {}
        STATE.finished = False
        STATE.result_clicked = False
        STATE.clicked_key = ()
        STATE.repeat_count = 0
        STATE.idle_warned = False
        STATE.last_progress_ts = time.monotonic()
        STATE.attrs = {}
        STATE.initial_attrs = {}
        STATE.character = ""
        STATE.char_checked = False
        STATE.score_by_q = {}
        STATE.strategy_logged = False
        _log(f"接管进行中的一局：进度条读到第 {question + 1} 题，题号按 {question} 记")
        return True


# --------------------------------------------------------------------------- #
# 初始化属性（由 interface.json 的输入项注入）
# --------------------------------------------------------------------------- #
@AgentServer.custom_action("adventure_click_icon")
class AdventureClickIcon(CustomAction):
    """点活动菜单里的「生存大冒险」图标。

    菜单里的图标位置不固定（活动增减会重排），所以先让 pipeline 用 OCR 找到
    「生存大冒险」这行文字，再按画面高度往上偏移到图标本体。
    """

    def run(self, context, argv) -> bool:
        box = list(getattr(argv, "box", []) or [])
        if len(box) < 4:
            _log("点图标：没拿到识别框")
            return False
        x, y, w, h = (int(v) for v in box[:4])
        image = getattr(argv, "image", None)
        height = image.shape[0] if image is not None else 720
        # 文字框中心到图标中心的距离占画面高度的比例（1147x720 下实测 44px）
        offset = int(round(height * 44.0 / 720.0))
        cx, cy = x + w // 2, y + h // 2 - offset
        context.tasker.controller.post_click(cx, cy).wait()
        _log(f"点图标：文字框 {box} → 点击 ({cx}, {cy})")
        return True


@AgentServer.custom_action("adventure_weekday_gate")
class AdventureWeekdayGate(CustomAction):
    """只在指定的星期几执行任务，其他日子直接结束。

    custom_action_param {"weekday": 0} 表示不限制，1~7 表示周一到周日。
    """

    def run(self, context, argv) -> bool:
        params = _as_dict(getattr(argv, "custom_action_param", None))
        try:
            want = int(params.get("weekday", 0) or 0)
        except (TypeError, ValueError):
            want = 0
        today = datetime.now().isoweekday()
        if want == 0:
            _log(f"执行日检查：今天是周{today}，未限制，继续")
            return True
        if want == today:
            _log(f"执行日检查：今天是周{today}，设定的就是今天，继续")
            return True
        _log(f"执行日检查：今天是周{today}，设定的是周{want}，本任务今天不跑")
        return False


@AgentServer.custom_action("adventure_voyage_done")
class AdventureVoyageDone(CustomAction):
    """航海寻宝收工：把完成语写到文本文件，由 pipeline 节点的 focus 显示到日志窗。

    mode=cleared：清空奖励次数模式（界面上剩余次数已归零）
    mode=once：单次模式（只跑一轮就收工）
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        conf = _as_dict(getattr(argv, "custom_action_param", None))
        mode = str(conf.get("mode") or "once")
        if mode == "cleared":
            msg = "已领完航海寻宝奖励"
        else:
            msg = "已完成本次航海寻宝"
        _log(msg)
        # agent 的 stdout 不进 maafw.log（GUI 日志窗看不到），所以这里另写一份
        # 文本文件，由 pipeline 节点的 focus 把它显示到日志窗。
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(VOYAGE_DONE_FILE, "w", encoding="utf-8") as handle:
                handle.write(msg + "\n")
        except Exception as exc:               # noqa: BLE001
            _log(f"写提示文件失败（{type(exc).__name__}: {exc}）")
        return True


@AgentServer.custom_action("adventure_daily_done")
class AdventureDailyDone(CustomAction):
    """本日已无可冒险次数（界面上只剩「重新冒险」）：打一条提示，然后收工。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        msg = (
            "本日生存大冒险已完成，如果你想使用点券重新冒险，"
            "请自行消耗点券进入冒险后开始任务"
        )
        _log(msg)
        # agent 的 stdout 不进 maafw.log（GUI 日志窗看不到），所以这里另写一份
        # 文本文件，由 pipeline 节点的 focus 把它显示到日志窗。
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(DAILY_DONE_FILE, "w", encoding="utf-8") as handle:
                handle.write(msg + "\n")
        except Exception as exc:               # noqa: BLE001
            _log(f"写提示文件失败（{type(exc).__name__}: {exc}）")
        return True


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
        # 新的一局：局内状态清空（归纳表**不动**，见下）。
        # 题号不能写死 0 —— 点「继续冒险」会回到原来那一局，界面进度条才是真相。
        STATE.question = _question_from_progress(getattr(argv, "image", None))
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
    - `check`：认出「冒险成功 / 冒险失败」→ 记面板，由 pipeline 收工
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
            # 闸门只挡「一题都没答」的情况：中途接手或题号错乱时账本可能只有
            # 一两题，原来的 done<9 会把结算检测整段挡掉，导致看到结算面板还在空转。
            if done < 1:
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
            # 结算画面也走这条分支（标题「冒险成功」或「冒险失败」）。
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
            # 题号以界面进度条为准：init 那一刻画面可能还停在加载/卡片页，
            # 读不到进度条会得到 0，所以决策前再读一次（此时一定在答题页）。
            _live_q = _question_from_progress(getattr(argv, "image", None))
            if _live_q and _live_q != STATE.question:
                _log(f"题号以界面进度条为准：已答 {_live_q} 题，账本记的是 {STATE.question} 题")
                STATE.question = _live_q
            # 按阶段反推在「中途接管」时是从空账本起步的，会一路偏小；
            # 这里只借用它把事件登记进 stage_seen。
            probe = logic.question_for_event(event, STATE.stage_seen)
            derived = STATE.question
            if probe is not None and probe != derived:
                _log(f"题号以界面进度为准：按阶段推得第 {probe + 1} 题，进度条是第 {derived + 1} 题")
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
