# -*- coding: utf-8 -*-
"""九转炼丹 —— MAA CustomAction 入口

pipeline 里只放一个节点：
    action: Custom / custom_action: jzld_run

它自己跑完一整局：截图 → 识别盘面 → 决策 → 点空格 → 等连锁，直到时间到或分数够。
"""
from __future__ import annotations

import io
import os
import time

import numpy as np
from PIL import Image

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from jzld import board as B
from jzld import bot as bot

TARGET_SCORE = 45000      # 拿满奖励
_SCORE_DBG = [0]          # 读分调试只打前 3 次
TIME_MARGIN = 8           # 剩余时间少于这么多秒就收手
MAX_STEPS = 500           # 保险丝：最多 500 次落子（不含等待识别）
SETTLE_TRIES = 12
SETTLE_INTERVAL = 0.15

# 「下颗丹药」图标在截图里的**相对位置**（按 1408×930 量出来的一组比例）
# 图里它在 (1114, 404) 左右，用比例表示就与窗口尺寸无关
# 「下颗丹药」图标框中心（940×590 客户区坐标）。裁掉截图黑边之后
# 这两个数是对的：实测匹配分 0.984（用户 m09962 那一局）。
# 之前量成 507/167 是因为当时截图带黑边、内容被压到 626 宽，量错了。
NEXT_BALL_RX = 755.0 / 940.0
NEXT_BALL_RY = 251.0 / 590.0
# 「下颗丹药」的匹配分下限。0.70 太松：绿「一」/绿「二」这类同色同级的球
# 在 40px 尺度下分数都挤在 0.7~0.8，读错一颗会让整步决策按错的等级走
# （用户 m08950：右下角明明是绿球，日志里是「下颗 6」，于是它一直去找
# 6 该放哪，永远不会去凑 2 的三连）。

# 顶部两个数字（当前总分 / 剩余时间）的位置，同样按 1408×930 量
SCORE_BOX = (288, 136, 505, 174)
# 1404×882 基准。原来 y=136~174 只框到「剩余时间」四个字的下沿，
# 数字在更上面（940 图上 y≈62~88），所以一直读成「余 ?」。
TIME_BOX = (1010, 92, 1235, 133)


_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "debug", "jzld", "run.log")
_SCORE_PATH = os.path.join(os.path.dirname(_LOG_PATH), "score.log")



def _clear_slag(context, img: np.ndarray, xs, ys, pos) -> bool:
    """把「净丹露」玉瓶拖到丹渣上把它清掉。成功返回 True。

    净丹露只有 3 个，用完就拖不动了——所以调用方拿不到效果时要拉黑该格。
    """
    h, w = img.shape[:2]
    # 和 _click 一样必须乘 _SCALE：识别图是 940×590，截图原图是 raw（1408×885），
    # post_swipe 与截图同源，不乘就整整偏到 0.71 的位置——净丹露永远拖不到丹渣上
    # （用户 m08194「一直没有用净丹露」：日志里拖着「成功」，丹渣却还在原地）。
    _s = _SCALE[0]
    x1 = int(round(w * SLAG_ITEM_RX * _s)) + CLICK_DX
    y1 = int(round(h * SLAG_ITEM_RY * _s)) + CLICK_DY
    r, c = pos
    x2, y2 = int(round(xs[c] * _s)) + CLICK_DX, int(round(ys[r] * _s)) + CLICK_DY
    try:
        controller = context.tasker.controller
        controller.post_swipe(x1, y1, x2, y2, SLAG_SWIPE_MS).wait()
        _log("        丹渣：把净丹露从 (%d,%d) 拖到 (%d,%d)" % (x1, y1, x2, y2))
        return True
    except Exception as e:  # noqa: BLE001
        _log("        丹渣：拖动失败 %s" % e)
        return False

_LAST_SCORE = [0]


_SCORE_BAD = [0]
_SCORE_AT = [0.0]
_ZERO = [0]        # 连续多少步分数没上涨（只认真读到的才算）
_FRESH = [False]   # 本次 _read_score 是否真的读到了数字
_LAST_STEP_SCORE = [0]   # 上次读分的时刻（最后 100 秒限流用）
_nofly = [0]       # 连续读不到「下颗丹药」的次数（>=20 判定本局结束，见 _run）
_noshot = [0]      # 连续截图失败次数（>=30 退出主循环）


def _save_dbg(img, tag: str) -> None:
    """把出问题的那一帧存到 debug/jzld/，方便事后看 OCR 到底裁到了什么。"""
    try:
        import cv2 as _cv
        _p = os.path.join(os.path.dirname(_LOG_PATH), "%s_%d.png" % (tag, time.time()))
        _cv.imwrite(_p, img)
    except Exception:
        pass


def _read_time_left(context, img) -> int:
    """读右上角「剩余时间 N」，拿不到就返回 -1。"""
    _FRESH[0] = False
    try:
        from maa.pipeline import JRecognitionType, JOCR
        h, w = img.shape[:2]
        x1, y1, x2, y2 = TIME_BOX
        sx, sy = w / 1404.0, h / 882.0
        roi = (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))
        det = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR(roi=roi, only_rec=True), img)
        if det is not None and det.hit and det.best_result:
            digits = "".join(ch for ch in (det.best_result.text or "") if ch.isdigit())
            if digits:
                return int(digits)
    except Exception:
        pass
    return -1


def _read_score(context, img) -> int:
    """读左上角「当前总分 N」，拿游戏里的真实分数。

    之前只记「模拟预测的得分」，跟游戏实际给的分毫无关系，所以脚本根本
    不知道自己离 45000 还差多少。这里用 MaaFramework 自带的 OCR
    （`run_recognition_direct`），不用额外装库。
    """
    try:
        from maa.pipeline import JRecognitionType, JOCR
        h, w = img.shape[:2]
        roi = (int(w * 0.115), int(h * 0.10), int(w * 0.22), int(h * 0.045))
        det = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR(roi=roi, only_rec=False), img)
        if det is None:
            if _SCORE_DBG[0] < 3:
                _SCORE_DBG[0] += 1
                _log("        读分：det=None（引擎没启动）")
            return 0
        txt = (det.best_result.text or "") if det.best_result else ""
        if _SCORE_DBG[0] < 3:
            _SCORE_DBG[0] += 1
            _log("        读分：hit=%s roi=%s 原文=%r" % (getattr(det, "hit", None), roi, txt))
        if det.hit and txt:
            digits = "".join(ch for ch in txt if ch.isdigit())
            if digits:
                n = int(digits)
                # 分数只会上涨。比上次还小只可能是读错了（少读一位、把
                # 「6」看成「3」之类），这时宁可沿用上次，别让决策层以为
                # 自己离 45000 还差两万分。
                if n + 5 < _LAST_SCORE[0]:
                    if _SCORE_BAD[0] < 3:
                        _SCORE_BAD[0] += 1
                        _log("        读分倒退 %d < %d，判为误读，沿用" % (n, _LAST_SCORE[0]))
                    return _LAST_SCORE[0]
                _LAST_SCORE[0] = max(_LAST_SCORE[0], n)
                _FRESH[0] = True
                return _LAST_SCORE[0]
    except Exception as _e:
        # 单帧读失败很常见：结算动画、飘字、画面闪一下。这时**沿用上一次
        # 读到的分数**，别掉回 0——否则会把「已经 4 万多」误判成「还没到
        # 45000」，白跑到时间结束。
        _log("        读分失败：%s" % _e)
    if _LAST_SCORE[0] > 0:
        if _SCORE_BAD[0] < 3:
            _SCORE_BAD[0] += 1
            _save_dbg(img, "score_fail")
        _log("        读分为空，沿用上次 %d" % _LAST_SCORE[0])
    return _LAST_SCORE[0]


def _write_file(msg: str) -> None:
    """只写 run.log（给事后排查用）。"""
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with io.open(_LOG_PATH, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _fill_rest(ctx: Context, xs, ys, board, limit: int = 20) -> None:
    """把剩下的空格依次点一遍再收工。

    盘面快满 / 已经达标时，与其干等 285 秒走完，不如把空格填掉：
    游戏侧只有「无处可放」或「时间到」才结算，主动填满能让它尽快进结算页，
    这样紧接着 OCR 到的分数才是这局的真实最终分。
    """
    _log("        填满收工：还剩 %d 个空格"
         % len([p for p, v in board.items() if v <= 0 and v != -1]))
    # 有些格子怎么点都落不上（盘面边缘、或识别把它当成空格其实不是）。
    # 之前每轮都重新算 _emps，于是同一个格子被反复点 20 次、卡死在收工阶段
    # （用户 m08739 看到的「达到分数后没进收工」其实是收工了但卡在这一格）。
    _bad = set()
    # 连续多少个格子都点不动。游戏结束后棋盘其实已经不存在了，每个格子
    # 都会「两次都没落上」，一格格点下去能空转十几分钟（用户 m11851
    # 「炼丹结束还是空转半天」）。连续 3 格都落不上就认定这局已经结束。
    _dead = 0
    for _i in range(limit):
        if _stopped(ctx):
            _log("        任务已停止，停止填格")
            return
        _emps = [p for p, v in board.items() if v <= 0 and v != -1 and p not in _bad]
        if not _emps:
            _log("        填满了")
            return
        # 收工填格也别随便挑第一个空格——按决策层的估值选，至少还能顺手
        # 合掉几对（用户 m06814 看到的是「填满阶段乱放到右边」）。
        _bp = None
        try:
            _imgf = _grab(ctx)
            if _imgf is not None:
                _ball_f = _next_ball(_imgf, dump=False)
                if _ball_f and _ball_f > 0:
                    _bp, _ = bot.plan2(board, _ball_f)
        except Exception:
            _bp = None
        _r, _c = _bp if _bp in _emps else _emps[0]
        for _retry in range(2):
            # 游戏可能已经结算、画面切回活动界面了。这时再按棋盘坐标点下去，
            # 落点正好压在「神丹榜」「兜率商店」这些按钮上（用户 m11578
            # 「为什么炼丹出来会点到神丹榜」）。每点一次之前先看一眼是不是
            # 已经不在棋盘了。
            _ichk = _grab(ctx)
            if _ichk is not None and _is_result_screen(ctx, _ichk):
                _log("        填格时检测到结算画面，停止点击")
                return
            _click(ctx, xs[_c], ys[_r])
            time.sleep(0.5)
            _img = _grab(ctx)
            if _img is None:
                _log("        填格截图失败，放弃")
                return
            _res = B.recognize(_img)
            if not _res.get("ok"):
                _log("        填格识别失败，放弃")
                return
            _after = _res["cells"]
            # 必须是「明确的球」（1~9）。原来只判 != 0，于是识别不确定的 -2
            # 也被当成「落上了」，下一轮就不再重试这一格（用户 m08739 卡住的那格）。
            _v = _after.get((_r, _c), 0)
            if _v > 0:
                board = _after          # 真的落上了
                _dead = 0
                break
            _log("        填格 (%d,%d) 没落上，重试 %d" % (_r, _c, _retry + 1))
        else:
            board = _after              # 两次都没落上，往后走
            _bad.add((_r, _c))          # 这一格点不动，本轮别再碰它
            _dead += 1
            _log("        填格 (%d,%d) 两次都没落上，本轮跳过（连续 %d 格）"
                 % (_r, _c, _dead))
            if _dead >= 3:
                _log("        连续 3 格都落不上，判定这局已结束，收工")
                return



def _is_result_screen(ctx: Context, img) -> bool:
    """是不是结算画面。

    结算页正中有一大段剧情文字（「三界劫气弥漫…欲炼九转金丹…」），
    只有它出现才说明这一局真的结束了。用户 m07203 要求：**没 OCR 到
    这段文字就一直往下填** —— 因为之前「填满了」的判据只是「没识别到
    空格」，可能是识别抖了或者还有一格没落上。
    """
    from maa.pipeline import JRecognitionType, JOCR
    h, w = img.shape[:2]
    for _roi in (
        (int(w * 0.08), int(h * 0.20), int(w * 0.84), int(h * 0.30)),
        (int(w * 0.08), int(h * 0.40), int(w * 0.84), int(h * 0.30)),
        (int(w * 0.05), int(h * 0.15), int(w * 0.90), int(h * 0.60)),
        (0, 0, w, h),                      # 兜底：整张图扫一遍（慢，但不会再漏）
    ):
        try:
            det = ctx.run_recognition_direct(
                JRecognitionType.OCR, JOCR(roi=_roi, only_rec=True), img)
        except Exception:
            continue
        if det is None or not getattr(det, "hit", False):
            continue
        txt = ((det.best_result.text if det.best_result else "") or "")
        # 原来只认剧情文字（劫气/金丹/兜率…），但练习模式的结算页只显示
        # 「游戏结束，练习模式无奖励」，根本没有那段剧情 —— 于是永远判不出
        # 结算，一直空填到 90 秒硬上限（用户 m08616「结束之后空转久了」）。
        if any(k in txt for k in ("劫气", "金丹", "兜率", "淬炼", "执掌炉火",
                                  "游戏结束", "无需奖励", "无奖励", "结算",
                                  "获得积分", "冒险成功", "冒险失败")):
            return True
    return False


def _fill_until_done(ctx: Context, xs, ys, board, rounds: int = 20):
    """把盘面填满；填的过程中如果又合成攒出空格，就再填一轮。

    用户 m07095「最后一手还是没下下去」：只填一轮的话，填格本身可能
    触发合成、腾出新的空格，于是屏幕上还留着空位就结算了。实际那局
    填完 3 格从 47741 涨到 48551（又合了 810 分），说明还在出东西。
    """
    _tried = set()      # 已经试过的「疑似空格」，避免反复点同一格
    # 硬时限：收工阶段每轮都要跑 4 次 OCR + 一轮填格，20 轮能拖到两分钟以上，
    # 用户看到的就是「结束了还在空转」（m08178）。90 秒到点就走人，
    # 反正目的只是把分读准，读不到就沿用上一次的分数。
    _last_e = 10 ** 9
    _deadline = time.time() + 90
    _idle = 0           # 连续几轮没能填上任何一格
    for _i in range(rounds):
        if _stopped(ctx):
            _log("        任务已停止，放弃填格")
            return board
        if time.time() > _deadline:
            _log("        填格超过 90 秒，不再等结算页，直接收工")
            return board
        # 结算页可能在「还有空格」的时候就出现（游戏提前结束）。之前只在
        # 「没空格了」那个分支里检查，于是提前结束时这里会一直空填到 90 秒
        # 超时——用户 m08616「结束之后空转久了一点」。每轮先看一眼。
        _img0 = _grab(ctx)
        if _img0 is not None and _is_result_screen(ctx, _img0):
            _log("        结算画面出现（第 %d 轮），提前收工" % (_i + 1))
            return board
        _fill_rest(ctx, xs, ys, board)
        time.sleep(0.5)
        _img2 = _grab(ctx)
        if _img2 is None:
            return board
        _r2 = B.recognize(_img2)
        if not _r2.get("ok"):
            return board
        board = _r2["cells"]
        _e2 = [k for k, v in board.items() if v <= 0 and v != -1]
        # 收工阶段出问题时唯一的现场就是这一帧——之前不存图，用户反馈
        # 「最后两格不下」时完全查不到证据（m07366）。
        _save_dbg(_img2, "fill_%02d" % _i)
        _log("        填满第 %d 轮：识别到 %d 个空格 %s"
             % (_i + 1, len(_e2), sorted(_e2)[:6]))
        # 连续两轮空格数没减少 → 填不动了，别死等
        if len(_e2) >= _last_e:
            _idle += 1
            if _idle >= 2:
                _log("        连续 %d 轮没填上，直接收工" % _idle)
                return board
        else:
            _idle = 0
        _last_e = len(_e2)
        if not _e2:
            # 「没空格了」不等于「游戏结束了」：可能是识别漏了一格，或者
            # 最后那一手还没落上去。用户要求以结算画面为准（m07203）。
            if _is_result_screen(ctx, _img2):
                _log("        填满了（%d 轮），结算画面已出现" % (_i + 1))
                return board
            # 没有空格也不代表填满了：可能是某个空格被判成了球。挑「和
            # 空格模板最像」的那一格再点一次（用户 m07229 选的反向兜底）。
            _gy = np.array(Image.fromarray(_img2[:, :, ::-1]).convert("L"), dtype=np.float32)
            _stp = (_r2["xs"][-1] - _r2["xs"][0]) / 6.0
            _sc = B.empty_scores(_gy, _r2["xs"], _r2["ys"],
                                 max(6, int(_stp * 0.22)))
            # 只点「还没试过」的格子：之前每次都取最高分那格，点完没反应
            # 再识别还是它最高分 → 死循环点同一格（用户 m07354「还是不下
            # 最后一步」）。
            _sc = {k: v for k, v in _sc.items() if k not in _tried}
            if _sc:
                _r3, _c3 = max(_sc, key=lambda k: _sc[k])
                _tried.add((_r3, _c3))
                _log("        没空格了但没看到结算画面，试 (%d,%d)（相似度 %.2f，已试 %d 格）"
                     % (_r3 + 1, _c3 + 1, _sc[(_r3, _c3)], len(_tried)))
                _click(ctx, _r2["xs"][_c3], _r2["ys"][_r3])
                time.sleep(0.8)
                continue
            _log("        没空格了但没看到结算画面，再试一轮（%d）" % (_i + 1))
            time.sleep(1.0)
            continue
        _log("        填满后又腾出 %d 个空格，再填一轮" % len(_e2))
    return board

def _log(msg: str) -> None:
    """只写 run.log，不 print —— 盘面/候选/前瞻这类调试信息走这里。"""
    _write_file(msg)


def _hist(msg: str) -> None:
    """把每局的最终成绩追加到 history.log，用于事后统计局数/分数分布。"""
    try:
        with io.open(os.path.join(os.path.dirname(_SCORE_PATH), "history.log"),
                     "a", encoding="utf-8", newline="\n") as f:
            f.write("%s  %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _say(msg: str) -> None:
    """print 到 MFA 日志面板 + 写 score.log —— 只给用户看分数用。

    score.log 是给 pipeline 的 focus 用的：focus 会把整个文件贴进面板，
    所以它必须只含分数，不能混调试信息。
    """
    if "本局最终得分" in msg:
        _hist(msg)          # 每局一行，攒下来统计
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print("[jzld] %s" % line, flush=True)
    try:
        os.makedirs(os.path.dirname(_SCORE_PATH), exist_ok=True)
        with io.open(_SCORE_PATH, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass

_CH = {0: '.', -1: 'X', 1: '1', 2: '2', 3: '3', 4: '4', 5: '5',
       6: '6', 7: '7', 8: '8', 9: '9', -2: '?'}


def _board_str(board: dict) -> str:
    """把盘面打成 6 行，方便对着截图核对（. 空 / X 渣 / ? 没认出来）。"""
    out = []
    for r in range(6):
        row = []
        for c in range(7):
            row.append(_CH.get(board.get((r, c)), ' '))
        out.append(''.join(row))
    return ' / '.join(out)



_RAW_SIZE_SET = [False]   # 只用一次 set_screenshot_use_raw_size
# 截图原图 -> 识别图（940x590）的缩放比。点击必须乘回这个比例：
# post_click 和截图用的是同一套「控制器坐标」，识别图被缩小过，坐标就不能直接传。
_SCALE = [1.498]


_RES_SET = [False]


def _grab(ctx: Context) -> np.ndarray | None:
    """截一张图，返回 BGR ndarray。

    先用 set_screenshot_use_raw_size(True) 关掉框架的等比缩放——棋盘坐标是按
    「窗口原始像素」定的；一旦被缩到 1147×720，列宽会从 84 变成 68，
    写死的比例会累积偏掉将近一整格。
    """
    # 把短边提到 1440：窗口只有 940×590，球上的汉字不到 12px，
    # 默认 720 放大后仍然糊，二/三/四 会互相认错（合成出新球后又被当成一级，
    # 于是无限重复合同一批一级球）。1440 下字高约 24px，模板匹配才稳。
    ctrl = ctx.tasker.controller
    if not _RES_SET[0]:
        try:
            # 不再放大：模板是 940×590 原生尺度裁的，放大反而插值糊掉
            ctrl.set_screenshot_use_raw_size(True)
        except Exception:  # noqa: BLE001
            pass
        _RES_SET[0] = True
    job = ctrl.post_screencap()
    job.wait()
    img = job.get()
    if img is None:
        return None
    img = np.ascontiguousarray(img)
    # 模板全部是按 940×590（窗口客户区原生尺寸）裁的。但 MFA 会把短边
    # 放大到 720 附近，实际拿到的常是 1410×885，字号变成 26px 跟模板对不上
    # （日志「缩放=0.66 字高=26 最佳=7 分=0.59」读不出下颗丹药就是这么来的）。
    # 所以不管框架给多大，一律缩回基准尺寸再识别。
    # 注意：set_screenshot_use_raw_size(True) 给的是 1408x885 的**画布**，
    # 但游戏内容只贴在左上 939x590，右边和下边全黑（实测 bbox 就是 939x590）。
    # 也就是说这个窗口的渲染尺寸就是 940x590，放大不了——所以仍然缩回基准尺寸。
    # 关键修正：截图是「画布」——游戏内容只贴在左上角一块，右边/下边是纯黑。
    # 实测 1410×885 的画布里内容只有 939×590（就是客户区大小），
    # 所以**必须先裁掉黑边再缩放**。以前直接按画布宽度算缩放，
    # 内容会被压到 626 宽，于是所有按 940 标的坐标全错位
    # （用户 m09994「你个废物又是哪里截出来的黑框」）。
    _nz = np.any(img[:, :, :3] > 8, axis=2)
    if _nz.any():
        _rs = np.where(_nz.any(axis=1))[0]
        _cs = np.where(_nz.any(axis=0))[0]
        img = img[_rs.min():_rs.max() + 1, _cs.min():_cs.max() + 1]
    if img.shape[1] != 940 or img.shape[0] != 590:
        _SCALE[0] = max(1.0, img.shape[1] / 940.0)
        from PIL import Image as _Img
        img = np.array(_Img.fromarray(img[:, :, ::-1]).resize((940, 590), _Img.LANCZOS))[:, :, ::-1]
        img = np.ascontiguousarray(img)
    else:
        _SCALE[0] = 1.0
    return img


def _read_number(ctx: Context, img: np.ndarray, box) -> int:
    """读顶部区域里的数字（纯数字，白字）。

    这里不做完整 OCR：MAA 的 OCR 只能跑 pipeline 节点，CustomAction 里调不了。
    所以退一步——只判断「有没有到 45000」用不上精确值，先返回 -1，
    由调用方用「时间走完」作为结束条件。
    """
    return -1


_NEXT_DUMP = [0]


def _next_ball(img: np.ndarray, dump: bool = True, cap: int = 0) -> int:
    """读右侧「下颗丹药」是几号球。0 = 还没显示出来（调用方会重试）。"""
    h, w = img.shape[:2]
    x = int(w * NEXT_BALL_RX)
    y = int(h * NEXT_BALL_RY)
    # 采样窗口跟着格子大小走，别写死像素
    _step = w / 940.0 * 56.0        # 按 940×590 的真实步长算，别再用 1147
    rng = max(6, int(_step * 0.36))
    # 球直径约 40px（940×590），窗口给 24 就够：再大就会把蓝底、
    # 「下颗丹药」四个字和框外的暗背景一起喂进去，匹配分被稀释。
    # 窗口必须比模板(40×40)大一圈：matchTemplate 要能滑动搜索。
    # 收到 40×40 时球和模板一样大、没余量，位置差一点就匹配不上，
    # 分数掉到阈值以下 → 连读 8 次空 → 判「本局结束」（08:59 那局）。
    win = max(26, int(_step * 0.48))
    # 死坐标永远差几个像素，不如自己找球心：在 ±40 的搜索区里把
    # 「非褐色底」的像素当球，取质心，再以它为中心裁 win 大小的框。
    # （褐色底 ≈ (160,93,46)：R 高、G 中、B 低，且 R-B 很大）
    sy0, sy1 = max(0, y - 40), min(h, y + 40)
    sx0, sx1 = max(0, x - 40), min(w, x + 40)
    zone = img[sy0:sy1, sx0:sx1].astype(np.int16)
    _brown = ((zone[:, :, 0] > 90) & (zone[:, :, 1] < 135) & (zone[:, :, 2] < 95)
              & ((zone[:, :, 0] - zone[:, :, 2]) > 45))
    # 球本身够亮（有高光）；蓝底/暗背景亮度低，不能算进去
    _bright = zone.max(axis=2) > 130
    _ballpx = (~_brown) & _bright
    if _ballpx.sum() > 200:
        _yy, _xx = np.nonzero(_ballpx)
        x = sx0 + int(_xx.mean())
        y = sy0 + int(_yy.mean())
        _log("        下颗丹药 自动定位球心 (%d,%d)" % (x, y))
    ys0, ys1 = max(0, y - win), min(h, y + win)
    xs0, xs1 = max(0, x - win), min(w, x + win)
    v = 0
    _nb_best, _nb_bv = 0, 0.0      # 供 dump 文件名用（识别值 + 匹配分）
    if ys1 > ys0 and xs1 > xs0:
        rgb = img[:, :, ::-1]
        im_v = rgb.max(axis=2).astype(np.float32) / 255.0
        _vmax = float(im_v[ys0:ys1, xs0:xs1].max())
        if _NEXT_DUMP[0] <= 3:
            _log("        下颗丹药 vmax=%.3f 窗口=(%d,%d)-(%d,%d)" % (_vmax, xs0, ys0, xs1, ys1))
        if _vmax >= 0.90:   # 有球心高光才算有球
            import cv2
            im_gray = np.array(_pil(rgb).convert("L"), dtype=np.float32)
            big = im_gray[ys0:ys1, xs0:xs1]
            # 「下颗丹药」框里的球比棋盘上的球大得多（框约 170px、球径约 120px），
            # 而 templates/t1..t9 是从棋盘格子里裁的字形（约 24px）——**尺度差一倍**，
            # 直接拿来比一定匹配不上。这里按「球径 ≈ win×1.19、字高 ≈ 球径×0.34」
            # 反推目标字高，再把模板整体缩放过去。
            # 模板已经是 940×590 原生尺度裁的（templates_small，40×40 含整颗球），
            # 「下颗丹药」框里的球跟棋盘上的球一样大，直接原尺寸比就行。
            # 之前那套「按 win 反推字高再缩放」是给 1404 尺度模板用的，缩放系数
            # 一算出来就是 0.60，反而把尺寸搞坏了（日志 分=0.63 读不出来）。
            tpl0 = B._templates()
            if tpl0:
                best, bv = 0, -1.0
                for n, t in tpl0.items():
                    if t.shape[0] > big.shape[0] or t.shape[1] > big.shape[1]:
                        continue
                    sc = float(cv2.matchTemplate(big, t, cv2.TM_CCOEFF_NORMED).max())
                    if sc > bv:
                        best, bv = n, sc
                _nb_best, _nb_bv = best, bv
                if _NEXT_DUMP[0] <= 3:
                    _log("        下颗丹药 最佳=%d 分=%.2f" % (best, bv))
                # 提高门槛：0.70 太松，绿「一」和绿「二」这类同色同级的球
                # 在 40px 尺度下分数都挤在 0.7~0.8，读出来的值经常是错的。
                # 读错一颗就等于整步决策都错（用户 m08950：下一颗明明是绿球，
                # 日志里是「下颗 6」，于是它一直去找 6 该放哪，永远不会去凑 2 的三连）。
                # 等级上限：场上最高才 3 级时，下一颗不可能是 6 级。
                # 掉落等级只会跟着滚雪球涨、不会跳级，所以用「场上最高级」当上界
                # 能挡掉绝大部分离谱误读。
                if bv >= B.MATCH_THR:
                    if cap > 0 and best > cap:
                        _log('        下颗丹药 读成 %d 但场上最高才 %d，判为误读' % (best, cap))
                    else:
                        v = best
    # 前几次把这块裁下来存盘，方便核对位置对不对
    _NEXT_DUMP[0] += 1
    if dump and _NEXT_DUMP[0] <= 12:
        try:
            os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
            # 存「真正喂给 matchTemplate 的那个窗口」（win 大小），
            # 而不是另算一个 94×94 —— 以前存的图和实际比对的图不是同一块，
            # 看着像"球被切了"其实只是 dump 尺寸写错了（用户 m09217）。
            _w2 = max(12, win) * 2
            crop = img[max(0, y - _w2 // 2):y + _w2 // 2,
                       max(0, x - _w2 // 2):x + _w2 // 2]
            # 存**原尺寸**（不放大），文件名带上识别值和分数，
            # 免得再拿放大图去比模板得出错误的 0.59（用户 m09784）。
            _pil(crop).save(
                os.path.join(os.path.dirname(_LOG_PATH),
                             "next_window_%02d_v%d(b%d)_%.2f.png"
                             % (_NEXT_DUMP[0], v, _nb_best, _nb_bv)))
            _log("下颗丹药采样点 (%d,%d) 识别=%d 窗口 %d×%d 尺寸=%dx%d"
                 % (x, y, v, _w2, _w2, w, h))
        except Exception:  # noqa: BLE001
            pass
    return v


def _pil(arr: np.ndarray):
    from PIL import Image as _Img
    return _Img.fromarray(arr if arr.dtype == np.uint8 else arr.astype(np.uint8))


def _sig(cells: dict) -> str:
    return "|".join("%d:%d" % (k[0] * 10 + k[1], v) for k, v in sorted(cells.items()))



def _stopped(ctx: Context) -> bool:
    """任务是否已经被停掉（MFA 点停止、或者用户关掉任务）。

    CustomAction 里的代码不会因为 pipeline 停下而被中断——之前收工阶段
    会一口气点 20 轮，用户点了「停止」脚本还在点（m07510「在任务结束
    之后还在点」）。所有长循环里都要查这个。
    """
    try:
        return bool(ctx.tasker.stopping) or bool(getattr(ctx.tasker, "running", True) is False)
    except Exception:
        return False

# 实测（2026-10-30 07:00，直接连窗口做的实验）：
#   游戏窗口 956x629、客户区 **940x590**、raw 截图 1410x885（= 客户区 1.5 倍）
#   post_click 用的就是**客户区坐标**：点 (793,50) 精确命中了 1410 图上的 X(1190,75)
#   —— 1190/1.5=793、75/1.5=50，一比一吻合。
#  已经把截图缩回 940x590（= 客户区），所以点击坐标**不需要任何偏移**。
# 「净丹露」图标框中心（940×590 客户区坐标）。_clear_slag 要把这个图标
# 拖到丹渣格上，所以起点必须点在图标上。
SLAG_ITEM_RX = 755.0 / 940.0
SLAG_ITEM_RY = 365.0 / 590.0

# 把「净丹露」图标拖到丹渣格上的滑动时长（毫秒）。太短游戏不认，
# 太长会把图标甩出格子。
SLAG_SWIPE_MS = 3000

CLICK_DX = 0
CLICK_DY = 0


def _click(ctx: Context, x: int, y: int) -> None:
    """在格子 (x, y) 落一颗子。x/y 是**截图坐标**。

    截图和游戏客户区差了 30px：FramePool 截出来的图比客户区少掉上面 30px，
    所以在截图上球心 y=156，客户区里其实是 y=186。
    - 识别必须用截图坐标（156），否则采样框吃不到球心；
    - 点击/Swipe 必须用客户区坐标，所以这里统一 +CLICK_DY。
    之前把 _hy 从 186 改成 156 只修了识别、没同步修点击，结果点击全部落到
    格子上面一格以外，20 次只成 2~3 次——用户 m07926「原来坐标歪的时候
    一点问题都没有」说的就是：186 那个"歪"坐标点得准。
    """
    # 识别图是 940x590，截图原图是 raw（现在 1408x885）。post_click 与截图同源，
    # 所以要把识别坐标乘回 _SCALE 才是控制器认的坐标。
    _s = _SCALE[0]
    ctx.tasker.controller.post_click(
        int(round(x * _s)) + CLICK_DX, int(round(y * _s)) + CLICK_DY).wait()


@AgentServer.custom_action("jzld_run")
class JzldRun(CustomAction):
    """跑一整局九转炼丹。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """包一层 try：回调里抛异常会让 MFA 直接把这个任务判结束，
        日志上看起来就是"任务瞬间完成"，极难排查。"""
        try:
            return self._run_impl(context, argv)
        except Exception as e:  # noqa: BLE001
            import traceback
            _log("异常中断：%s" % e)
            _log(traceback.format_exc()[:3000])
            # 同时打到 stdout：_log 只写 run.log，别人机器上出问题时那个文件
            # 往往没被一起打包，日志里就只剩一句「任务失败」，啥也看不出来。
            print("九转炼丹异常中断：%s" % e, flush=True)
            print(traceback.format_exc()[:1500], flush=True)
            return False
        finally:
            # `set_screenshot_use_raw_size` 是 **controller 级别的全局开关**，
            # 不只影响本任务。九转炼丹为了拿原始画布把它设成 True，收工后
            # 如果不改回来，紧接着跑副本任务时 script_task.capture() 拿到的
            # 就是 1408×885（短边 885），直接报
            # 「脚本与当前720截图尺寸不一致，请使用相同游戏布局重新录制。」
            # （真实踩过：更新后先跑炼丹、再跑悬赏，悬赏一开始就失败。）
            try:
                context.tasker.controller.set_screenshot_use_raw_size(False)
            except Exception:  # noqa: BLE001
                pass
            _RES_SET[0] = False

    def _run_impl(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
            io.open(_LOG_PATH, "w", encoding="utf-8").close()
        except Exception:  # noqa: BLE001
            pass
        # 每局开头归档上一局的日志，而不是直接删。之前是 os.remove，结果
        # 「刚跑完那局」的盘面/候选全没了，排查问题只能靠猜（真实踩过）。
        for _p in (_SCORE_PATH, _LOG_PATH):
            try:
                if os.path.isfile(_p):
                    _arch = os.path.splitext(_p)[0] + "_prev" + os.path.splitext(_p)[1]
                    if os.path.isfile(_arch):
                        os.remove(_arch)
                    os.rename(_p, _arch)
            except Exception:  # noqa: BLE001
                pass
        # 每局开头把读分状态清空：_LAST_SCORE 是模块级变量，不清的话上一局
        # 的高分会带进来，第一步就判成「已达目标」直接收工（真实踩过）。
        _LAST_SCORE[0] = 0
        _SCORE_BAD[0] = 0
        _SCORE_AT[0] = 0.0
        _nofly[0] = 0
        _log("开始（目标 %d 分）" % TARGET_SCORE)
        last_sig = None
        stuck = 0
        bad = set()      # 点过但盘面没反应的格子，本局内别再碰
        slag_done = set()  # 已经用净丹露清过的丹渣格（净丹露只有 3 个，用完就拖不动）
        step = 0
        fail_dump = 0
        # 截一张先拿尺寸，用来算「下颗丹药」的相对偏移
        first = _grab(context)
        if first is None:
            _log("截图失败，退出")
            return False
        # 开局校准一次：刚进棋盘时盘面是空的，格子底板最干净，这时候量出来
        # 的格子位置是准的；等球铺满了再量就会被球压住边框、量歪。
        # 用户 m08780：「每局刚开局校准一次，然后不重新校准」。
        # 不做自动校准，用写死的格心坐标（_HX0.._HX1 / _HY0.._HY1）。
        # 依据：history.log 里 08:14 之前那一版（没有校准）连续十几局稳定
        # 45000+（45086 / 45285 / 46759 / 47741 / 46635 / 45384 / 45841 …），
        # 08:36 加上自动校准后开始出现 0 分和乱放（用户 m10079「给我回滚到
        # 原来稳定4w5的版本」）。
        B.clear_calibration()

        while step < MAX_STEPS:
            if _stopped(context):
                _log("任务已被停止，退出主循环")
                return True
            img = _grab(context)
            if img is None:
                # step 不递增，所以截图一直失败会在这里空转（比如窗口丢了但任务
                # 还没停）。连失 30 次（约 9 秒）就当作不能继续，退出主循环。
                _noshot[0] += 1
                _log("截图失败，跳过（第 %d 次）" % _noshot[0])
                if _noshot[0] >= 30:
                    _log("连续 30 次截图失败，退出主循环")
                    return False
                time.sleep(0.3)
                continue
            _noshot[0] = 0
            res = B.recognize(img)
            if not res.get("ok"):
                # 还没进棋盘（在活动界面/加载中）：不计步，一直等到认出棋盘为止
                _log("识别失败：%s（等待进入棋盘）" % res.get("reason"))
                fail_dump += 1
                if fail_dump % 10 == 1:      # 每 10 次存一张，方便调坐标
                    try:
                        from PIL import Image as _Img
                        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
                        _Img.fromarray(img[:, :, ::-1]).save(
                            os.path.join(os.path.dirname(_LOG_PATH), "fail_%d.png" % fail_dump))
                        _log("已存失败截图 fail_%d.png 尺寸 %dx%d" % (fail_dump, img.shape[1], img.shape[0]))
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(1.0)
                continue

            # 多帧投票：光效/缩放淡入会让个别格子认不出来（日志里一局出现过
            # 7 个 ?，两个并排的「七」就被漏掉，脚本自然补不上第三个）。
            # 连截 3 帧，每格取「出现过的非未知数字」里票数最多的那个。
            _votes = {}
            _imgs = [img]
            _nq = sum(1 for _v in res["cells"].values() if _v == -2)
            for _vf in range(2 if _nq >= 2 else 0):
                _i2 = _grab(context)
                if _i2 is None:
                    break
                _r2 = B.recognize(_i2)
                if not _r2.get("ok"):
                    break
                _imgs.append(_i2)
                for _k, _v in _r2["cells"].items():
                    if _v != -2:
                        _votes.setdefault(_k, []).append(_v)
                time.sleep(0.12)
            if _votes:
                _merged = dict(res["cells"])
                _fixed = 0
                for _k, vs in _votes.items():
                    if _k not in _merged or _merged[_k] == -2:
                        _merged[_k] = max(set(vs), key=vs.count)
                        _fixed += 1
                if _fixed:
                    _log("        多帧投票补回 %d 个格子" % _fixed)
                res = dict(res, cells=_merged)

            board = dict(res["cells"])
            xs, ys = res["xs"], res["ys"]
            # 场上最高等级 = 「下颗丹药」的天花板（掉落等级只会跟着涨、不跳级）
            # 下一颗的等级可以比场上最高再高一级（游戏允许提前给高一级的球），
            # 所以上界要给 +1。之前直接用场上最高，把正常的 8 判成误读，
            # 然后连续 8 次读不到 → 误判本局结束（用户「任务过一半结算了」）。
            _cap = max([v for v in board.values() if v > 0], default=3)
            _cap = min(9, max(3, _cap + 1))
            ball = _next_ball(img, cap=_cap)
            if ball <= 0:
                # 「下颗丹药」那格被高光/动画盖住时读不出来。重截几张再试，
                # 读不到就一直等着——绝不能拿 ball=0 去猜位置，那会乱放。
                for _try in range(6):
                    time.sleep(0.08)
                    _img2 = _grab(context)
                    if _img2 is None:
                        continue
                    ball = _next_ball(_img2, cap=_cap)
                    if ball > 0:
                        img = _img2
                        _log("        下颗丹药重试 %d 次读到 %d" % (_try + 1, ball))
                        break
                if ball <= 0:
                    # 连续读不到说明画面已经不是「炼丹进行中」了：多半是时间到、
                    # 出了结算页。以前这里只 `continue`，于是无限重试卡死——
                    # 用户看到的「已经结束了但任务不停、也不报分数」就是这段。
                    _nofly[0] += 1
                    _log("        第 %d 步：读不到「下颗丹药」（第 %d 次），重来"
                         % (step, _nofly[0]))
                    # 先直接看一眼是不是结算页：读不到「下颗丹药」最常见的原因
                    # 就是这局已经结束了。原来只会数次数、等 20 次（6~8 秒）
                    # 才收工，用户看到的就是「结算画面出来半天了任务还在点」
                    # （用户 m11238）。命中结算文字就立刻收工。
                    if _is_result_screen(context, img):
                        _log("        识别到结算画面，本局结束")
                        break
                    # 8 次太快了（每次约 0.3 秒，球被高光/连击动画盖住两秒就误判
                    # 本局结束）。20 次约 6~8 秒，才够一次连锁动画走完。
                    if _nofly[0] >= 20:
                        _log("连续 %d 次读不到「下颗丹药」，判定本局已结束" % _nofly[0])
                        break
                    continue
                _nofly[0] = 0
            empties = bot.empty_cells(board)

            if not empties:
                _log("棋盘满了，结束")
                break
            # 只剩一两格、净丹露又用完了：这一局已经没有翻盘空间，主动把空格
            # 填掉让游戏进结算，省下剩下的等待时间（用户 m06659）。
            # 只剩几个格子、又连着几步都拿不到分，说明这局已经到顶了：
            # 再磨下去只是把空格耗光，不如早点结算（用户 m06775 看到
            # 「已经没有可能三连了还在往下放」）。
            if len(empties) <= 4 and _ZERO[0] >= 3 and _score > 0:
                _log("空格只剩 %d 且连续 %d 步没得分，直接收工"
                     % (len(empties), _ZERO[0]))
                board = _fill_until_done(context, xs, ys, board)
                _fin = 0
                for _try3 in range(3):
                    time.sleep(0.6)
                    _fin = _read_score(context, _grab(context))
                    if _fin and _fin >= _LAST_SCORE[0]:
                        break
                _say("===== 本局最终得分：%d 分（目标 %d，共 %d 步）====="
                     % (_fin or _LAST_SCORE[0], TARGET_SCORE, step))
                return True
            if len(empties) <= 2 and len(slag_done) >= 3:
                _log("空格只剩 %d 且净丹露用完，填满收工" % len(empties))
                board = _fill_until_done(context, xs, ys, board)
                _fin = 0
                for _try2 in range(3):
                    time.sleep(0.6)
                    _fin = _read_score(context, _grab(context))
                    if _fin and _fin >= _LAST_SCORE[0]:
                        break
                _say("===== 本局最终得分：%d 分（目标 %d，共 %d 步）====="
                     % (_fin or _LAST_SCORE[0], TARGET_SCORE, step))
                return True

            _b2 = dict(board)
            for _bp in bad:
                _b2[_bp] = -1      # 拉黑的格子伪装成丹渣，plan 会跳过
            # 两步前瞻（对应 V_h 递推）：先看「这一手 + 下一颗按权重展开」的期望分。
            # 精确 DP 状态空间 10^30 算不了，叶子用 plan2 内部的启发式估值。
            if ball > 0:
                _t0 = time.time()
                pos, _v2 = bot.plan2(_b2, ball)
                # 打 top3 明细：以后「为什么下在死路」这类问题能直接从
                # 日志看出是哪一项（room/pair/ext/cw/lay/v2）判歪了。
                # 下颗 >= 5 时把**全部**候选和分项都打出来：5 以上是最容易
                # 出现「明明能三连却不合」的档位（用户 m09202），现场只有
                # top3 的话看不出是哪一项把它压掉了。
                _nd = 8 if ball >= 5 else 3
                for _d in bot.LAST_DETAIL[:_nd]:
                    _log("        候选 %s 值 %.0f ｜ room=%d pair=%d ext=%d "
                         "ring=%d near=%d cw=%d ｜ 当步 %.0f 前瞻 %.0f 布局 %.0f"
                         % (_d[1], _d[0], _d[2], _d[3], _d[4], _d[5], _d[6],
                            _d[7], _d[8], _d[9], _d[10]))
                plan = None
                _log("    前瞻 %.2f 秒 → 放 %s（期望 %.0f）" % (time.time() - _t0, pos, _v2))
            else:
                pos, plan = None, None

            # 丹渣不清白占格子，但净丹露只有 3 个，不能见一个清一个。
            # 只在「清掉它真能换来合成」时才动手：把可清丹渣当成空格再算一遍，
            # 只有算出的分明显更高才花一个净丹露；否则就当它不存在。
            # 判据按用户要求：**清掉这个丹渣后，把当前这颗球放上去能不能合成**。
            # 能 → 花一个净丹露；不能 → 不浪费（净丹露一共就 3 个）。
            _slag_pick = None
            _slag_gain = 0.0
            # 空格只剩两格时，丹渣就是「白占一个位置」，不管能不能马上合都得清：
            # 盘一满就没得走了（用户：「还没到最后60秒就已经把盘铺满了」）。
            _n_empty = len([k for k, v in board.items() if v <= 0 and v != -1])
            # 丹渣占的格子是永久损失：不清就永远是 -1。之前只在「清掉正好
            # 能合」或「空格只剩 2 个」时才动手，结果一局下来 3 个丹渣常常
            # 一个都没清，等于棋盘少三格，76 步就被塞满了（用户 m06509
            # 「这局只有 37583」那局就是这么死的）。改成空格少于 6 格就
            # 开始清，不再死等「正好能合」。
            # 用户 m11333「丹渣改成要能合三连才消除」：正常情况一律要求三连。
            # 只保留「盘马上要满」这一个例外（空格 ≤2 时不清就真的没地方可放），
            # 原来是 <=5，大量净丹露花在了 +6 分的小合成上。
            _tight = _n_empty <= 2
            if ball > 0 and len(slag_done) < 3:
                for _sp in [k for k, v in board.items() if v == -1 and k not in slag_done]:
                    _b3 = dict(_b2)
                    _b3[_sp] = 0
                    _try = bot.simulate(_b3, _sp, ball)   # 直接假设这颗球落在丹渣格
                    # 清掉丹渣等于白捡一个空格，但捡回来的这块地价值不一样：
                    # 中圈能连成线的位置远胜边角（用户 m06695「用净丹露的时候
                    # 是随便用的没有考虑放在哪更好」）。所以给每个丹渣格算一个
                    # 位置分，越靠中心越值钱，再叠到「这一手能拿多少分」上。
                    _r0, _c0 = _sp
                    _pos_val = (10.0 - (abs(_r0 - 2.5) + abs(_c0 - 3.0)) * 3.0)
                    _sel = _try.score + _pos_val
                    if _tight:
                        # 腾位置优先：按「清掉后放下去的总分 + 位置分」挑
                        if _sel >= _slag_gain:
                            _slag_pick, _slag_gain = _sp, _sel
                    else:
                        # 除了「这一手立刻合成」，还要认「清出来能凑成可延长的两连」——
                        # 用户 m08110：眼前就有两连 + 一块丹渣挡在中间，清掉就能把
                        # 第三个放上去，但旧判据只认 merges>0，于是净丹露到死都没用。
                        # 用户 m11333 明确要求：**只有能合三连才花净丹露**。
                        # 原来的判据是 _try.merges>0（放上去会产生任意合成）
                        # 或「凑成可延长的两连」——后者太宽松，经常为了 +6 分
                        # 清掉一个丹渣，反而把真正能三连的位置让掉了。
                        if bot._makes_three(_b3, _sp, ball) and _sel > _slag_gain:
                            _slag_pick, _slag_gain = _sp, _sel
                if _slag_pick is not None and _tight:
                    _log("        丹渣：空格只剩 %d 个，清掉 %s 腾位置" % (_n_empty, _slag_pick))
                # 清渣那条路是**独立**算出来的，它会覆盖 plan2 已经选好的
                # 位置。实测就出过事：21:57:12 那手 plan2 选了 (1,3) 能三连
                # （分 1018），但清渣逻辑发现 (2,2) 的渣清掉后这颗 7 能合
                # （+6 分），于是改放 (2,2)——三连被白白放弃，球跑到拐角
                # （用户 m11305「拐角的那个七为什么不直接放到两个7旁边凑三连」）。
                # 所以清渣只有在「不比原决策差」时才允许抢这一手。
                if (_slag_pick is not None and pos is not None
                        and _slag_gain < _v2 - 1):
                    _log("        丹渣：清 %s 能得 %.0f，但不如原计划 %s（%.0f），"
                         "这次不用净丹露" % (_slag_pick, _slag_gain, pos, _v2))
                    _slag_pick = None
                if _slag_pick is not None:
                    _log("        丹渣：清掉 %s 后这颗 %d 能合成（+%.0f 分），用净丹露" % (_slag_pick, ball, _slag_gain))
                    if _clear_slag(context, img, xs, ys, _slag_pick):
                        slag_done.add(_slag_pick)
                        # 清完必须**把球放回丹渣那一格**。原来这里 continue 重来一轮，
                        # 重新 plan 之后会挑到别的位置，白白浪费一个净丹露
                        # （用户反馈：「用净丹露消除了丹渣却把小球放到其他地方去了」）。
                        time.sleep(0.45)
                        _sx, _sy = xs[_slag_pick[1]], ys[_slag_pick[0]]
                        _log("        丹渣已清，把 %d 放到 (%d,%d)" % (ball, _slag_pick[0], _slag_pick[1]))
                        try:
                            _click(context, _sx, _sy)
                        except Exception as _e:
                            _log("        点击失败 %s" % _e)
                        # 验证一下「清掉后能合成」这个判断到底对不对：如果放下去
                        # 盘面上那颗还是原样、没有升级球出现，说明是识别把某个
                        # 邻居当成了同号（用户 m06841 看到的就是这种情况）。
                        time.sleep(0.6)
                        _vimg = _grab(context)
                        if _vimg is not None:
                            _vres = B.recognize(_vimg)
                            if _vres.get("ok"):
                                _vc = _vres["cells"]
                                _upv = min(9, ball + 1)
                                _got = any(v == _upv and board.get(k) != _upv
                                           for k, v in _vc.items())
                                if not _got:
                                    _log("        [warn] 丹渣格放 %d 后没看到 %d 级"
                                         "，判据可能被识别带偏了" % (ball, _upv))
                        board[_slag_pick] = ball
                        time.sleep(0.75)
                        continue
                    slag_done.add(_slag_pick)   # 拖了没反应说明净丹露用完了，别再试
            _rank = []
            if ball > 0 and len(empties) > 1:
                _rank = []
                for _p in empties:
                    _r = bot.simulate(board, _p, ball)
                    if _r.merges > 0:
                        _k = (1, _r.score, _r.merges, 0)
                    else:
                        _k = (0, bot._pair_bonus(board, _p, ball) * 10
                              + bot._near_same(board, _p, ball) * 3
                              - bot._foreign(board, _p, ball),
                              bot._pair_bonus(board, _p, ball), 0)
                    _rank.append((_k, _p))
                _rank.sort(key=lambda x: x[0], reverse=True)
                _log("    盘面 " + _board_str(board))
                if step <= 6 or step % 5 == 0:
                    try:
                        from PIL import Image as _Img
                        _d = os.path.dirname(_LOG_PATH)
                        os.makedirs(_d, exist_ok=True)
                        _Img.fromarray(img[:, :, ::-1]).save(
                            os.path.join(_d, "step_%02d.png" % step))
                    except Exception:  # noqa: BLE001
                        pass
                _log("    候选 " + " | ".join(
                    "%s:%s" % (_p, ("合%d" % _k[1]) if _k[0] == 1 else ("p%d" % _k[1]))
                    for _k, _p in _rank[:4]))
            if pos is None:
                _log("        第 %d 步：算不出落点，跳过" % step)
                continue

            sig = _sig(board)
            stuck = stuck + 1 if sig == last_sig else 0
            if stuck == 0:
                bad.clear()   # 盘面回到正常节奏，黑名单作废
            last_sig = sig

            _sim = bot.simulate(board, pos, ball) if (pos and ball > 0) else None
            desc = ("放 %s %s" % (pos, bot.describe(_sim))) if _sim else ("放 %s（无连线）" % (pos,))
            # 用户要求在最后 100 秒里每 10 秒才读一次分：那段时间盘面变化
            # 快、每步都 OCR 又慢又容易读到中间帧，收益不高。
            _tleft = _read_time_left(context, img)
            _now = time.time()
            _due = (_tleft < 0) or (_tleft > 100) or (_now - _SCORE_AT[0] >= 10.0)
            if _due:
                _score = _read_score(context, img)
                _SCORE_AT[0] = _now
            else:
                _score = _LAST_SCORE[0]
            # 每落一子游戏都会加分，所以「分数没涨」只在真的读到数字时才有
            # 意义；OCR 没读到的那些步不能算进去（用户 m06792）。
            if _FRESH[0]:
                if _score > _LAST_STEP_SCORE[0]:
                    _ZERO[0] = 0
                else:
                    _ZERO[0] += 1
                _LAST_STEP_SCORE[0] = _score
            _log("分 %d" % _score)   # 过程分只进 run.log，面板上只看最终得分
            _log("[%3d] 分 %5d 余 %3s 空格 %2d 下颗 %s → %s"
                 % (step, _score, _tleft if _tleft >= 0 else "?",
                    len(empties), ball or "?", desc))
            if _score >= TARGET_SCORE:
                _log("已达 %d 分，填满剩余空格再收工" % _score)
                board = _fill_until_done(context, xs, ys, board)
                _fin = 0
                for _try2 in range(3):
                    time.sleep(0.6)
                    _fin = _read_score(context, _grab(context))
                    if _fin and _fin >= _score:
                        break
                _say("===== 本局最终得分：%d 分（目标 %d，已达标）====="
                     % (_fin or _score, TARGET_SCORE))
                return True

            _cx, _cy = xs[pos[1]], ys[pos[0]]
            if step <= 30:
                _log("        点击 (%d,%d) 落 %s" % (_cx, _cy, pos))
            _click(context, _cx, _cy)

            # 等连锁稳定：先让落子/合成的入场动画走完（球是缩放淡入的，
            # 刚落地时数笔画会把「半透明的暗边」当成第二道笔画，一被判成二），
            # 再要求连续两次盘面一致才继续。
            # 判稳要更严：球是严格居中的，偏了只可能是截到了落子/合成的
            # 动画帧（半透明缩放淡入）。所以不仅要求**连续三帧一致**，还要
            # 这几帧里没有认不出来的格子（`?`），否则两帧都糊、也「一致」。
            time.sleep(0.13)
            _t_settle = time.time()
            prev, _stable, _prev_sc = None, 0, None
            for _ in range(SETTLE_TRIES):
                time.sleep(SETTLE_INTERVAL)
                img2 = _grab(context)
                if img2 is None:
                    continue
                r2 = B.recognize(img2)
                if not r2.get("ok"):
                    continue
                cur = _sig(r2["cells"])
                # 判稳要同时满足三件事：盘面字符串一致、**分数也没变**、
                # 而且没有认不出来的格子。只看盘面不够——连锁合成中途
                # 会有「两波之间的静止间隙」，那几帧盘面看起来一样，但其实
                # 下一波还没结算完，这时候截到的盘面少了球。分数是游戏给的，
                # 任何连锁都会加分，分数不动才是真的停了。
                if cur == prev:
                    _sc2 = _read_score(context, img2)
                    if _sc2 == _prev_sc and "?" not in cur:
                        _stable += 1
                        if _stable >= 1:      # 连续两次一致
                            _log("判稳：%d 帧一致、分数 %d 未变，用时 %.2fs"
                                 % (_stable + 1, _sc2, time.time() - _t_settle))
                            break
                    else:
                        _stable = 0
                    _prev_sc = _sc2
                else:
                    _stable = 0
                    _prev_sc = None
                prev = cur

            if stuck >= 2:
                # 点了但盘面没动：这一格多半是识别错了（其实是球或丹渣），
                # 直接拉黑，本局内不再点它——之前只换「第 2 优候选」，
                # 候选只有一个时会退回原格死磕 20 多次。
                bad.add(pos)
                _log("[warn] 盘面没变，拉黑 %s（已拉黑 %d 格）" % (pos, len(bad)))
                stuck = 0

            step += 1

        _final = _read_score(context, _grab(context)) if step else 0
        _say("===== 本局最终得分：%d 分（目标 %d，共 %d 步）====="
             % (_final, TARGET_SCORE, step))
        return True
