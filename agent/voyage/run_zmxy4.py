"""照抄跑法：用仓库自己的 Voyager + auto_loop。

只替换两个 IO 层（逻辑一行没动）：
  1. 截图：dxcam 的 region 偏移在这台机器上不生效 → 换成 PrintWindow 抓窗口自己
  2. 按键：win32api.keybd_event 是全局键、窗口不在前台就打空 → 换成 PostMessage
另外在 get_next_direction / get_shot_place_direction 外面包了一层打印（只打印，不改返回值）。
"""
import sys
import time

sys.argv = [sys.argv[0]]

import cv2

import real_adapter as R
from zmxy4_treasure_hunt.voyage import game_window as GW
from zmxy4_treasure_hunt.voyage.core import map as ZMAP
from zmxy4_treasure_hunt.voyage.core import action_manager as AM
from zmxy4_treasure_hunt.voyage import env as EV
from zmxy4_treasure_hunt.voyage.env import Voyager, auto_loop

def _pick_game_window():
    """挑出真正的游戏窗口。

    关键字「造梦」会连资源管理器一起命中（文件夹名就叫 MAA造梦西游4-release），
    而被最小化的窗口客户区是 0x0，后面 cv2.resize 会直接断言失败、任务「瞬间结束」。
    所以按「标题像游戏 > 面积大 > 坐标正常」挑一个，并且把候选打出来便于排查。
    """
    cands = R.list_windows("造梦")
    good = []
    for hwnd, title, box in cands:
        w, h = box[2] - box[0], box[3] - box[1]
        if w < 200 or h < 150 or box[0] < -5000 or box[1] < -5000:
            continue
        if ("资源管理器" in title) or ("Explorer" in title):
            continue
        score = 2 if "造梦盒子" in title else 1
        good.append((score, w * h, hwnd, title, box))
    if not good:
        raise SystemExit(
            "没找到游戏窗口。标题含「造梦」的可见窗口：%s"
            % [(t, b) for _, t, b in cands]
        )
    good.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return good[0][2], good[0][3], good[0][4]


HWND, TITLE, BOX = _pick_game_window()
_HWND_CACHE = {"hwnd": HWND}
W, H = BOX[2] - BOX[0], BOX[3] - BOX[1]


# 仓库假设「传给它的画面 = 可玩区域」，它内部用 MAP_LEFT/RIGHT/UP/DOWN 比例
# 把可玩区域映射成 320x200 的迷宫。我们给的是整窗 940x590（含边框和底部技能栏），
# 两边坐标就不一致，玩家位置会系统性偏移。所以这里先按同样的比例裁一刀。
_MAP_L, _MAP_R, _MAP_U, _MAP_D = 0.066, 0.95, 0.105, 0.88


_frame_cache = {"bgr": None, "t": 0.0}


_PROF = {"cap": 0.0, "det": 0.0, "upd": 0.0, "dec": 0.0, "n": 0, "gwu": 0.0, "vp": 0.0}


def _prof_dump(tag):
    n = _PROF["n"]
    if n and n % 30 == 0:
        print("[耗时] 截图 %.0fms 更新 %.0fms 决策 %.0fms 感知 %.0fms 环境 %.0fms  (n=%d)" % (
            _PROF["cap"] * 1000 / n, _PROF["upd"] * 1000 / n,
            _PROF["dec"] * 1000 / n, _PROF["gwu"] * 1000 / n, _PROF["vp"] * 1000 / n, n),
            flush=True)
        for k in ("cap", "det", "upd", "dec", "gwu", "vp"):
            _PROF[k] = 0.0


# 用户要求：截图只用 MaaFramework 的 FramePool，不要 PrintWindow。
# 实测（同一台机器、同一窗口）：PrintWindow 40~74ms/帧（均值 49），
# FramePool 15~61ms/帧（均值 29）—— 光这一项就值 20ms。
import zm_grab as _zg


def _capture(box=None):
    _t0 = time.perf_counter()
    img = _zg.grab(HWND, W, H)
    _PROF["cap"] += time.perf_counter() - _t0
    # 顺手缓存一帧 BGR：门检测和白块同步都从这里拿，
    # 别再自己截图 —— 一次 PrintWindow 要 40~500ms，多截一次就把主循环拖死。
    _frame_cache["bgr"] = img
    _frame_cache["t"] = time.time()
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


GW.GameWindow.capture_screen = lambda self, box=None: _capture(box)

# 按键同理：win32api.keybd_event 是全局键，窗口不在前台就全打在别的窗口上。
# 换成 PostMessage 直接投给游戏窗口。
import ctypes as _ct

import win32api
import win32con

_u32 = _ct.windll.user32
_orig_keybd = win32api.keybd_event


def _keybd(vk, bscan=0, dwflags=0, extra=0):
    if dwflags & win32con.KEYEVENTF_KEYUP:
        _u32.PostMessageW(HWND, 0x101, vk, 0)     # WM_KEYUP
    else:
        _u32.PostMessageW(HWND, 0x100, vk, 0)     # WM_KEYDOWN


win32api.keybd_event = _keybd


def _clear_keys():
    """把 WASD+J 全部松一遍（清掉上一次被 kill 时残留的按住状态）。"""
    for ch in ("W", "A", "S", "D", "J"):
        _u32.PostMessageW(HWND, 0x101, ord(ch), 0)


def in_voyage(image) -> bool:
    """这一帧像不像航海副本的房间（主城/背包几乎没海面）。"""
    h, w = image.shape[:2]
    play = image[int(0.07 * h):int(0.90 * h), int(0.03 * w):int(0.97 * w)]
    if play.size == 0:
        return False
    hsv = cv2.cvtColor(play, cv2.COLOR_BGR2HSV)
    hch, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    sea = (((hch > 15) & (hch < 40)) | ((hch > 85) & (hch < 115))) & (sat > 90) & (val > 120)
    return float(sea.mean()) > 0.25


def _grab_bgr():
    # 只用 FramePool（用户要求，不要 PrintWindow）
    return _zg.grab(HWND, W, H)


_DET = R.OnnxDetector("weights/best.onnx")

# ---------------------------------------------------------------- 地图套数识别
# 仓库原来的 get_map_index_at_start() 只拿「小白块的归一化坐标」去和 7 个写死的点比最近邻，
# 一个点区分 7 套必然撞车，撞了也不报警 → 拿错静态障碍图 → 一路撞礁石。
# 换成 zm_mapid：先数小地图几行几列，再看白块落在第几行第几列，
# 同尺寸的兄弟（3x3 两套 / 3x4 两套 / 4x3 两套）用「整张灰格的 rooms 形状」区分。
import zm_mapid as _MM

# 仓库的 can_next_level() 只看「这一帧的 enemy_list / bullets 是不是空的」，
# 而 YOLO 偶尔漏一帧敌人 → 它立刻以为打完了，转去赶路 → 被剩下的敌人打死。
# 这里要求「连续 _EMPTY_NEED 帧都没有敌人才算清场」。
# 连续多少帧「零敌人零子弹」才算这关清干净。
# 原来写 6，但判定只查了「历史长度够不够」，没查「这 6 条是不是全 0」——
# 前面 5 帧都有敌人、这一帧 YOLO 漏检一次，条件照样成立，
# 于是战斗没结束就往出口跑，跑的路上还顺手拉走别的船的仇恨。
# 现在改成「最近 N 条必须全 0」，并按 7 FPS 折算 N=15（约 2 秒）。
_EMPTY_NEED = 15
_empty_hist = []


def _install_can_next_level_patch(EV):
    _orig = EV.Voyager.can_next_level

    def _cnl(self):
        try:
            n = len(self.enemy_list) + len(self.bullets)
        except Exception:
            return _orig(self)
        _empty_hist.append(n)
        if len(_empty_hist) > _EMPTY_NEED:
            _empty_hist.pop(0)
        if n > 0:
            return False
        if len(_empty_hist) < _EMPTY_NEED:
            return False
        if any(v != 0 for v in _empty_hist):
            return False          # 历史里只要有一帧见过东西，就不算清场
        return bool(_orig(self))

    EV.Voyager.can_next_level = _cnl

import maa_white as _MW

_orig_gmi = GW.GameWindow.get_map_index_at_start


_MAP_LIST = [
    (0.940, 0.0776),
    (0.914, 0.108),
    (0.931, 0.098),
    (0.931, 0.108),
    (0.948, 0.118),
    (0.931, 0.118),
    (0.922, 0.0776),
]


_MM_DECIDED = {"ok": False}
_MAP_LOCK = {"idx": None, "level": -1}




def _gmi_by_shape(self):
    """白块位置 → 最近邻 7 个点（照抄仓库的 map_list）。

    不再自己切格：手写行列投影数出来的列数经常错一格，
    而 MaaFramework 的 ColorMatch 找白块一向很稳。
    """
    if _MAP_LOCK["idx"] is not None:
        return _MAP_LOCK["idx"]          # 已经认过，直接复用
    try:
        img = _frame_cache.get("bgr")
        if img is None:
            return _orig_gmi(self)
        pos = _MW.white_center(img, _HWND_CACHE.get("hwnd"))
    except Exception as exc:                       # noqa: BLE001
        print(f"[地图] MAA 白块识别异常，退回仓库原方法：{exc}", flush=True)
        return _orig_gmi(self)
    if pos is None:
        print("[地图] MAA 没找到白块，退回仓库原方法", flush=True)
        return _orig_gmi(self)

    # 用户 m26803：光有白块位置不够 —— 先把小地图是「几列 x 几行」和「白块在哪一格」读出来，
    # 用 (列数, 行数) 把 7 套 layouts 砍到 1~2 套，再看白块格对不对得上那套的起点格。
    try:
        _g = _MM.read_grid(img)
    except Exception as exc:                       # noqa: BLE001
        _g = None
        print(f"[地图] 小地图格阵识别异常：{exc}", flush=True)
    if _g:
        _cols, _rows = int(_g["cols"]), int(_g["rows"])
        _wr, _wc = _g["white"]
        _MM_DECIDED["ok"] = False
        print("[地图] 小地图 %d列x%d行 白块(row%d,col%d)" % (_cols, _rows, _wr, _wc),
              flush=True)
        _lvl2 = 0
        try:
            _lvl2 = int(getattr(getattr(_VOYAGER_REF[0], "map", None), "level", 0) or 0)
        except Exception:
            _lvl2 = 0
        _same = [k for k, _lay in enumerate(_MM.LAYOUTS)
                 if int(_lay["cols"]) == _cols and int(_lay["rows"]) == _rows]
        if _same:
            # 白块 = 「我现在在哪一格」，要按当前 level 沿路线走到的格来比，
            # 不是只比起点（用户 m26864：3x4 那两套起点格不同、白块还会随进度移动）。
            _repo2lay = {v: k for k, v in _MM.LAYOUT_TO_MAP.items()}
            _rank = []
            for _k in _same:
                _lay = _MM.LAYOUTS[_k]
                _rooms = [int(v) for v in _lay["rooms"]]
                _ci = _wr * _cols + _wc
                if _ci >= len(_rooms) or _rooms[_ci] < 0:
                    _rank.append((99, _k))          # 白块落在没房间的格子上，这套不可能
                    continue
                _try = []
                for _r in range(7):
                    if _repo2lay.get(_r) != _k:
                        continue
                    _cell = _MM.cell_at_level(_k, _r, _lvl2)
                    if _cell is None:
                        _try.append(99)
                    else:
                        _try.append(abs(_cell[0] - _wr) + abs(_cell[1] - _wc))
                _rank.append((min(_try) if _try else 99, _k))
            _rank.sort()
            # 距离 >1 说明白块根本不在起点格上（可能读错行/列，也可能是路线中段），
            # 这种情况不能只凭它定案，交给「白块坐标 + 缺口」和后面的礁石形状校验。
            if _rank[0][0] > 1:
                print("[地图] 白块与路线第 %d 步不符（最近差 %d 格），先不定案" % (
                    _lvl2, _rank[0][0]), flush=True)
            elif len(_rank) == 1 or _rank[1][0] > _rank[0][0]:
                _idx2 = _MM.LAYOUT_TO_MAP.get(_rank[0][1])
                if _idx2 is not None:
                    _MAP_LOCK["idx"] = _idx2
                    _MM_DECIDED["ok"] = True
                    print("[地图] %d列x%d行 + 白块(row%d,col%d) -> 第 %d 套（候选 %s）" % (
                        _cols, _rows, _wr, _wc, _idx2, [k for _, k in _rank]), flush=True)
                    return _idx2
            else:
                print("[地图] %d列x%d行 分不开（候选 %s），继续用白块坐标+缺口" % (
                    _cols, _rows, [k for _, k in _rank]), flush=True)
    xr, yr = pos
    dists = [((xr - x) ** 2 + (yr - y) ** 2) ** 0.5 for x, y in _MAP_LIST]

    # 光看白块位置不够：map_list 七个点挤在 (0.91~0.95, 0.077~0.118) 里，
    # 一个位置常常同时贴近好几个点（实测最近邻会挑到「第一步往上」那套，
    # 而这局的出口明明在左边）。所以再用「这一侧画面里真有缺口」过滤：
    # route 第一步的方向必须和实测缺口方向一致。
    _dir_of_code = {0: "右", 1: "下", 2: "左", 3: "上"}
    try:
        sides = {s[0] for s in R.find_doors(img)}
    except Exception:
        sides = set()
    cands = list(range(len(dists)))
    if sides:
        filtered = [k for k in cands if _dir_of_code.get(_MM.ROUTE_LIST[k][0]) in sides]
        if filtered:
            cands = filtered
    idx = int(min(cands, key=lambda k: dists[k]))
    # 同一局内锁定：白块在 0/6 两套之间只差 0.0007，重判会来回跳，
    # map_index 一跳 route/静态墙图全变，表现就是「朝墙一直撞」。
    # 最近和第二近差太少就说明这两套分不清（实测 0/6 两套只差 0.0007），
    # 这种时候不锁定，等下一帧白块位置更清楚再判，别一开局就锁错。
    _sorted = sorted(dists[k] for k in cands)
    if len(_sorted) >= 2 and (_sorted[1] - _sorted[0]) < 0.002 and len(cands) > 1:
        print(f"[地图] 两套分不清（差 {_sorted[1] - _sorted[0]:.4f}），先不锁定、等下一帧", flush=True)
        return _orig_gmi(self)
    _MAP_LOCK["idx"] = idx
    print(
        f"[地图] 白块({xr:.3f}, {yr:.3f}) 缺口={sorted(sides)} -> 第 {idx} 套 "
        f"(最近 {_MAP_LIST[idx]}, 距离 {dists[idx]:.4f}, 候选 {cands})",
        flush=True,
    )
    return idx


GW.GameWindow.get_map_index_at_start = _gmi_by_shape

# ---------------------------------------------------------------- 战斗走位：近身时不许斜着贴脸
# 仓库的 get_direction_from_next_point() 是纯几何的：A* 路径第一点同时有 dx 和 dy 就给斜向键。
# 结果就是敌人贴上来时，它照着斜线往敌人怀里凑。
# 用户 m23842/m24054：方向一律八向全开，这里只做「顶住墙就横挪一根」，不改方向。
from zmxy4_treasure_hunt.voyage.core.direction import Direction as _Dir

_DIAG = {_Dir.DownRight, _Dir.DownLeft, _Dir.UpRight, _Dir.UpLeft}
_DANGER_PX = 60.0

_DIR_VEC = {_Dir.Right: (1, 0), _Dir.Left: (-1, 0), _Dir.Up: (0, -1), _Dir.Down: (0, 1),
            _Dir.DownRight: (1, 1), _Dir.DownLeft: (-1, 1),
            _Dir.UpRight: (1, -1), _Dir.UpLeft: (-1, -1)}          # 敌人进入这个距离就算「危险」（320x200 空间，约 5.6 格）
_near = {"dx": 0.0, "dy": 0.0, "dist": 1e9}

# 反查：(dx 符号, dy 符号) -> Direction。用来把「往哪边挪」直接翻译成
# 斜向方向（仓库的 Direction.value 支持 "WA"/"SD" 这类双键）。
_VEC2DIR = {v: k for k, v in _DIR_VEC.items()}


def _dir_of(sx, sy):
    """把 (±1/0, ±1/0) 换成 Direction；全 0 返回 None。"""
    if sx == 0 and sy == 0:
        return None
    return _VEC2DIR.get((sx, sy))


def _retreat_plan(dx, dy):
    """贴身后退的候选方向，按优先级排。

    用户 m26159：「移动方向太死板了，应该允许例如按着 aw 然后中途松开 w 微调」。
    人打的时候是「一边退、一边顺手把身位对到同排/同列」，不是只按一根轴。
    所以这里给三轮：
      1) 主要轴后退 + 副轴同时脱离（斜向，如敌在右上就按 SA），偏差大时才用；
      2) 只按主要轴后退（副轴偏差已经很小，靠仓库的 modify_direction 自动松开
         另一根键 —— 这就是用户 m26159 说的「按着 aw 中途松开 w 微调」）；
      3) 实在不行换另一根轴退（保命优先）。
    """
    out = []
    if abs(dx) >= abs(dy):
        main = (1 if dx > 0 else -1, 0)      # 敌人右侧 -> 往左退
        main = (-main[0], 0)
        fine = (0, 1 if dy < 0 else -1)      # 往上对齐（敌人偏上就往下挪）
        if abs(dy) > 12:
            d = _dir_of(main[0], fine[1])
            if d is not None:
                out.append(d)
        d = _dir_of(main[0], 0)
        if d is not None:
            out.append(d)
        d = _dir_of(0, fine[1])
        if d is not None:
            out.append(d)
    else:
        main_v = -1 if dy > 0 else 1         # 敌人在下方 -> 往上退
        fine = (1 if dx < 0 else -1, 0)
        if abs(dx) > 12:
            d = _dir_of(fine[0], main_v)
            if d is not None:
                out.append(d)
        d = _dir_of(0, main_v)
        if d is not None:
            out.append(d)
        d = _dir_of(fine[0], 0)
        if d is not None:
            out.append(d)
    return out


# ---------------------------------------------------------------- 白块位置当「我现在在哪」的证据
# 用户要求：「地图上亮度最高的白色格子代表现在所在的房间，可以加个重新导航
# 并且回到原来的房间，然后再去本该去的下一个房间」
# 做法：每 5 帧读一次小地图白块，反推它落在 route 的第几步；
# 和仓库自己记的 level 不一致，就以白块为准（白块是画面上的直接证据）。
_lvl_sync = {"n": 0}


def _sync_level_from_minimap(self):
    try:
        mid, info = _MM.read_map_id(_grab_bgr())
        if mid is None or not info.get("white"):
            return
        # 切格置信度太低时（分差为负、或候选为空），白块坐标不可信 ——
        # 这时改 level 只会把它改得更错（实测会从第 1 关跳到第 5 关）。
        scores = info.get("scores") or []
        if not scores or scores[0][0] < 10.0:
            return
        lay = info.get("layout_index")
        if lay is None:
            return
        exp = _MM.level_from_white(info["white"], int(lay), int(mid))
        if exp is None:
            return
        if exp != self.level:
            print(f"[导航] 白块在第 {exp + 1} 关（仓库记第 {self.level + 1} 关），"
                  f"以白块为准，改去 {self.route[exp]}", flush=True)
            self.level = exp
            self.next_exit = self.route[exp]
    except Exception as exc:
        print(f"[导航] 读白块失败：{exc}", flush=True)


# ---------------------------------------------------------------- 出口要用画面实测的门
# 仓库 nearly_exit() 在 level==0 时按写死的出口带判断：只要在带子里就认为
# 「我在出口正前方，往那边开就出去了」。但那个位置实际常常是礁石 ——
# 这就是「一直往左顶墙」的源头。所以这里拿画面实测的缺口去校验它。
_door_cache = {"t": 0.0, "sides": set()}
_SIDE_OF_DIR = {_Dir.Right: "右", _Dir.Left: "左", _Dir.Up: "上", _Dir.Down: "下"}
_SIDE_CN = {"右": "右", "左": "左", "上": "上", "下": "下"}


def _door_info():
    """返回 {方位: 缺口中心的 maze 像素坐标}，0.5 秒缓存一次。"""
    now = time.time()
    if now - _door_cache["t"] < 0.5:
        return _door_cache.get("info", {})
    _door_cache["t"] = now
    info = {}
    img = _frame_cache.get("bgr")
    if img is None:
        return info
    try:
        for side, cell, _px in R.find_doors(img):
            info[side] = (cell[0] * 320.0 / 30.0, cell[1] * 200.0 / 19.0)
    except Exception:
        info = {}
    _door_cache["info"] = info
    return info


# ---------------------------------------------------------------- 卡住了就横着挪
# 前面那些「用画面找门」的判断在这游戏上不可靠：画面最底部有水，但船被礁石带
# 挡着过不去，find_doors 会误判成「下面有门」。所以再加一层纯反应式的兜底：
# 连续若干帧位置几乎没变，就认为顶住了，强制换一根轴挪开。
_stuck = {"x": None, "y": None, "n": 0, "flip": 0}
_STUCK_PX = 3.0
_EDGE_PX = 45.0        # 离边界 45 像素内算「贴边」，贴边就不许再往外退
_STUCK_FRAMES = 8


def _note_pos(px, py):
    if _stuck["x"] is None:
        _stuck["x"], _stuck["y"] = px, py
        return
    if abs(px - _stuck["x"]) < _STUCK_PX and abs(py - _stuck["y"]) < _STUCK_PX:
        _stuck["n"] += 1
    else:
        _stuck["n"] = 0
    _stuck["x"], _stuck["y"] = px, py


def _toward_center(maze, px, py):
    """朝地图中心走，但必须先看墙。

    用户 m23941：「在往中间走的路上有礁石，结果又卡死了」——
    之前是闷头往中心指，撞上礁石照样卡。
    这里按「主轴 -> 次轴 -> 四个方向里离中心最近的那个可走方向」逐级退让。
    """
    if maze is None:
        return None
    h, w = maze.shape
    nx, ny = w / 2.0 - px, h / 2.0 - py
    primary = (_Dir.Right if nx > 0 else _Dir.Left) if abs(nx) >= abs(ny) \
        else (_Dir.Down if ny > 0 else _Dir.Up)
    secondary = (_Dir.Down if ny > 0 else _Dir.Up) if abs(nx) >= abs(ny) \
        else (_Dir.Right if nx > 0 else _Dir.Left)
    for cand in (primary, secondary):
        if _dir_free(maze, px, py, cand):
            return cand
    # 两个轴都堵：在四个方向里挑一个走得通、且离中心更近的
    best = None
    for cand in (_Dir.Right, _Dir.Down, _Dir.Left, _Dir.Up):
        if not _dir_free(maze, px, py, cand):
            continue
        vx, vy = _DIR_VEC.get(cand, (0, 0))
        after = ((w / 2.0) - (px + vx * 30)) ** 2 + ((h / 2.0) - (py + vy * 30)) ** 2
        if best is None or after < best[0]:
            best = (after, cand)
    return best[1] if best else None


def _stuck_escape_direction(direction):
    """顶住了的时候给一个方向。

    在角落（贴两条边）时，横挪往往还是沿墙滑，于是永远出不来 ——
    所以贴边就统一往「地图中心」走，这是唯一能确定离开角落的方向。
    """
    if _stuck["n"] < _STUCK_FRAMES:
        return direction
    # 贴边就先朝中间走（用户 m23922：又卡到左上角角落）
    px, py = _near.get("px"), _near.get("py")
    maze = _near.get("maze")
    if px is not None and py is not None and maze is not None:
        h, w = maze.shape
        at_edge = px < _EDGE_PX or px > w - _EDGE_PX or py < _EDGE_PX or py > h - _EDGE_PX
        if at_edge:
            cand = _toward_center(maze, px, py)
            if cand is not None:
                return cand
    _stuck["flip"] += 1
    horiz = _stuck["flip"] % 2 == 0
    if direction in (_Dir.Up, _Dir.Down):
        return _Dir.Right if horiz else _Dir.Left
    if direction in (_Dir.Left, _Dir.Right):
        return _Dir.Down if horiz else _Dir.Up
    # 斜向：取它的一根轴
    if direction in (_Dir.UpRight, _Dir.DownRight):
        return _Dir.Right if horiz else _Dir.Left
    return _Dir.Up if horiz else _Dir.Down


_orig_nearly = ZMAP.Map.nearly_exit


def _nearly_exit_checked(self, player):
    # 用户 m23319：不要管「这一侧有没有缺口」，直接信仓库给的方向。
    # 但**出口在哪一段**还是得知道，否则会贴着礁石顶（实测偏 17px 就撞死）。
    # 所以只做位置校验：离实测出口太远就返回 None，让 A* 先过去对准。
    d = _orig_nearly(self, player)
    if d is None:
        return None
    side = _SIDE_OF_DIR.get(d)
    hit = _door_info().get(side)
    if hit is None:
        return d
    if side in ("上", "下"):
        off = abs(hit[0] - player.x)
    else:
        off = abs(hit[1] - player.y)
    if off > 8.0:
        return None
    return d


ZMAP.Map.nearly_exit = _nearly_exit_checked


# 用户 m23319：「把整个地图视为只有路径的出入口有缺口，其他都是封闭的」。
# 静态迷宫图四周边界全封闭，A* 到不了 next_exit 指向的那条边，
# 所以在返回给寻路之前，把那一侧的出口带挖开一段。
_EXIT_BANDS = {
    0: (80, 108),    # 右：y 方向
    1: (147, 182),   # 下：x 方向
    2: (80, 108),    # 左
    3: (147, 182),   # 上
}
_OPEN = 5


def _punch_exit(maze, exit_code):
    band = _EXIT_BANDS.get(exit_code)
    if band is None:
        return maze
    h, w = maze.shape
    a, b = band
    if exit_code in (1, 3):
        a = max(0, min(a, w - 1)); b = max(a + 1, min(b, w))
        if exit_code == 1:
            maze[h - _OPEN:, a:b] = False
        else:
            maze[:_OPEN, a:b] = False
    else:
        a = max(0, min(a, h - 1)); b = max(a + 1, min(b, h))
        if exit_code == 0:
            maze[a:b, w - _OPEN:] = False
        else:
            maze[a:b, :_OPEN] = False
    return maze


_orig_get_maze = ZMAP.Map.get_maze


def _get_maze_with_exit(self):
    try:
        m = _orig_get_maze(self).copy()
        return _punch_exit(m, int(getattr(self, "next_exit", 4)))
    except Exception:
        return _orig_get_maze(self)


ZMAP.Map.get_maze = _get_maze_with_exit

# 白块同步也要在「赶路」时跑（原来只挂在 map.update，而赶路走 get_next_direction 不调 update）
_orig_gnd = ZMAP.Map.get_next_direction


def _gnd_with_sync(self, player, falling_items, last_direction=None):
    _lvl_sync["n"] += 1
    if _lvl_sync["n"] % 30 == 0:
        _sync_level_from_minimap(self)
    # 赶路走的是 get_next_direction，根本不经过 map.update()，
    # 所以「卡住检测」得在这里也记一笔，否则 _stuck 永远是 0。
    try:
        _note_pos(float(player.x), float(player.y))
    except Exception:
        pass
    return _orig_gnd(self, player, falling_items, last_direction=last_direction)


ZMAP.Map.get_next_direction = _gnd_with_sync

_orig_map_update = ZMAP.Map.update


def _update_near(self, player, bullets, enemies):
    _t_upd = time.perf_counter()
    _lvl_sync["n"] += 1
    if _lvl_sync["n"] % 30 == 0:
        _sync_level_from_minimap(self)
    try:
        _near["maze"] = self.get_maze()
        _near["px"], _near["py"] = int(player.x), int(player.y)
        _note_pos(float(player.x), float(player.y))
    except Exception:
        _near["maze"] = None
    _near["enemies"] = list(enemies)
    best = None
    for e in enemies:
        d = ((e.x - player.x) ** 2 + (e.y - player.y) ** 2) ** 0.5
        if best is None or d < best[0]:
            best = (d, e.x - player.x, e.y - player.y)
    if best is None:
        _near["dist"] = 1e9
    else:
        _near["dist"], _near["dx"], _near["dy"] = best
    try:
        return _orig_map_update(self, player, bullets, enemies)
    finally:
        _PROF["upd"] += time.perf_counter() - _t_upd


ZMAP.Map.update = _update_near









def _free_at(x, y) -> bool:
    maze = _near.get("maze")
    if maze is None:
        return True
    h, w = maze.shape
    if not (0 <= x < w and 0 <= y < h):
        return False
    return not bool(maze[int(y), int(x)])


def _step_ok(direction, steps=14) -> bool:
    """沿这个方向走 steps 像素，中途每一小段都得是可走的。"""
    px, py = _near.get("px"), _near.get("py")
    if px is None or py is None:
        return True
    d = _DIR_VEC[direction]
    for k in range(2, steps + 1, 4):
        if not _free_at(px + d[0] * k, py + d[1] * k):
            return False
    return True


_orig_take = AM.ActionManager.take_action_direction


def _take_no_diag_when_close(self, direction):
    """只做一件事：顶住了就横着挪。

    用户的定位很清楚：「核心是为了更好的躲敌人的子弹，而不是自限移动方向」。
    所以这里**不再限制方向** —— 斜向该走就走，8 向全开。
    躲子弹交给仓库的 flee_region 判断；贴脸交给 _install_keep_distance_patch 主动退。
    这个函数只保留最后那道纯反应式兜底：连着 N 帧位置没变就横挪一根。
    """
    if direction is not None and _stuck["n"] >= _STUCK_FRAMES:
        esc = _stuck_escape_direction(direction)
        if esc != direction:
            print(f"[卡住] 连着 {_stuck['n']} 帧没动，{direction.name} -> 横着挪 {esc.name}", flush=True)
            _stuck["n"] = 0
            direction = esc
    return _orig_take(self, direction)


# 停用：近身不许斜向（战斗走位补丁，改回仓库原逻辑）
# AM.ActionManager.take_action_direction = _take_no_diag_when_close


# ---------------------------------------------------------------- 别被逼进角落
# 敌人逼近时仓库会往外躲（flee_region_enemy），几艘船从不同方向压过来，
# 就一直往同一个方向的墙角退，退到角落无路可走只能吃伤害。
# 这里补一条：贴着边（离边界 < 45 像素）还想往那边走，就改成朝中间走。
_DIR_H = {"D": 1, "A": -1, "S": 0, "W": 0}
_DIR_V = {"D": 0, "A": 0, "S": 1, "W": -1}


# ---------------------------------------------------------------- 勾引（bait）
# 仓库的战术只有「站安全位 / 靠近就躲」，不会主动诱敌开炮。
# 这里补一层：按每个敌人自己的装填进度决定进退。
#   progress = 1 - fire_cool_down / cool_down_time
#   0 = 刚打完（装填开始），1 = 快开炮了
#   · 有人刚打完（progress < 0.35）-> 冲那个敌人（趁装填打）
#   · 全都快开炮（progress > 0.75）-> 退到射程边缘等它开火（勾引）
#   · 其他 -> 不干预，交给仓库
_BAIT_ATTACK = 0.35
_BAIT_HOLD = 0.75
# 勾引的「靠近」有下限：贴到 130px（约 12 格，敌人射程 8.4 格之外）就停，
# 再近就是拿血换输出。用户 m23916：「也不能为了勾引而贴到脸上」。
_BAIT_BOSS_NEAR = 150.0   # 勾引补丁专用（已停用），与下面的 _BOSS_NEAR 分开
_BAIT_BOSS_FAR = 210.0
_BAIT_WAIT_MAX = 1.5     # 等诱最多等这么久，超时就主动冲（否则会一直在射程边缘来回晃=挂机）
_bait_wait = {"t": 0.0}
_BAIT_STOP_PX = 130.0
_BAIT_BACK_PX = 105.0     # 已经比这个还近，边装填也别停，先退回来
_ENGAGE_PX = 200.0      # 约 18 格：太远就先靠近，进了这个范围才谈进退


def _install_bait_patch():
    _orig = ZMAP.Map.get_shot_place_direction

    def _gsd_bait(self, player, last_direction=None):
        direction, path, flag = _orig(self, player, last_direction)
        _near["flag"] = flag
        if flag == 1:                      # 正在躲子弹，绝不干预
            return direction, path, flag
        ens = _near.get("enemies") or []
        if not ens:
            return direction, path, flag
        maze = _near.get("maze")
        if maze is None:
            return direction, path, flag

        fresh = None       # 刚打完、正在装填的敌人
        soon = 0           # 快开炮的敌人数量
        boss_present = False
        for e in ens:
            if e is None:
                continue
            _w = float(getattr(e, "x2n", 0.0)) - float(getattr(e, "x1n", 0.0))
            if _w > 0.07 or float(getattr(e, "cool_down_time", 3)) <= 1.5:
                boss_present = True
            total = float(getattr(e, "cool_down_time", 0.0) or 0.0)
            left = float(getattr(e, "fire_cool_down", 0.0) or 0.0)
            if total <= 0:
                continue
            prog = 1.0 - max(0.0, min(1.0, left / total))
            if prog < _BAIT_ATTACK and fresh is None:
                fresh = e
            if prog > _BAIT_HOLD:
                soon += 1

        px, py = float(player.x), float(player.y)

        # 打 boss 的勾引 = 保持距离 + 站到它的弹道线上（同一行或同一列）。
        # 不是凑到脸上：距离锁在 _BAIT_BOSS_NEAR ~ _BAIT_BOSS_FAR，超出就往回拉，
        # 距离合适后再把「偏移小的那根轴」对齐，让它的十字炮弹沿着这条线飞过来。
        # 用户 m23978：「勾引只是要保持距离贴到boss弹道那条线上，而不是直接凑到脸上挨打」。
        if boss_present:
            b = None
            bd = 1e9
            for e in ens:
                if e is None:
                    continue
                d2 = ((e.x - px) ** 2 + (e.y - py) ** 2) ** 0.5
                if d2 < bd:
                    bd, b = d2, e
            if b is not None:
                dx, dy = b.x - px, b.y - py
                if bd < _BAIT_BOSS_NEAR:              # 太近：先退
                    cands = ([_Dir.Left if dx > 0 else _Dir.Right,
                              _Dir.Up if dy > 0 else _Dir.Down] if abs(dx) >= abs(dy)
                             else [_Dir.Up if dy > 0 else _Dir.Down,
                                   _Dir.Left if dx > 0 else _Dir.Right])
                    for away in cands:
                        if _dir_free(maze, px, py, away):
                            print(f"[BOSS] 离 boss {bd:.0f}px 太近 -> 退 {away.name}", flush=True)
                            return away, [], 6
                elif bd > _BAIT_BOSS_FAR:             # 太远：靠近到射程边缘
                    cand = (_Dir.Right if dx > 0 else _Dir.Left) if abs(dx) >= abs(dy) \
                        else (_Dir.Down if dy > 0 else _Dir.Up)
                    if _dir_free(maze, px, py, cand):
                        print(f"[BOSS] 离 boss {bd:.0f}px 太远 -> 靠近 {cand.name}", flush=True)
                        return cand, [], 6
                else:                            # 距离合适：对齐弹道线
                    if abs(dy) <= abs(dx):       # 以横向为主 -> 对齐成同一行
                        off, cand = dy, (_Dir.Down if dy > 0 else _Dir.Up)
                    else:                        # 以纵向为主 -> 对齐成同一列
                        off, cand = dx, (_Dir.Right if dx > 0 else _Dir.Left)
                    if abs(off) > 10.0 and _dir_free(maze, px, py, cand):
                        print(f"[BOSS] 距 {bd:.0f}px 合适，对齐弹道线（偏 {off:+.0f}）-> {cand.name}", flush=True)
                        return cand, [], 6
                return direction, path, flag

        # ① 有人刚打完 -> 朝它靠近（趁装填）
        if fresh is not None:
            dx, dy = fresh.x - px, fresh.y - py
            dist = (dx * dx + dy * dy) ** 0.5
            # 除了目标敌人，别的敌人也不能靠太近 —— 否则「冲一个、贴另一个」。
            # 用户 m23960：「还是在斜向贴脸勾引」。
            others = [e for e in ens if e is not None and e is not fresh]
            near_other = 1e9
            for e in others:
                d2 = ((e.x - px) ** 2 + (e.y - py) ** 2) ** 0.5
                if d2 < near_other:
                    near_other = d2
            if near_other < _BAIT_STOP_PX:
                print(f"[勾引] 目标在装填，但另一个敌人只有 {near_other:.0f}px -> 不冲", flush=True)
                return direction, path, flag
            if dist > _BAIT_STOP_PX:        # 还离得远，靠近到射程外沿就停
                if abs(dx) >= abs(dy):
                    cand = _Dir.Right if dx > 0 else _Dir.Left
                else:
                    cand = _Dir.Down if dy > 0 else _Dir.Up
                if _dir_free(maze, px, py, cand):
                    print(f"[勾引] 敌人装填中（剩 {getattr(fresh,'fire_cool_down',0):.1f}s），"
                          f"离 {dist:.0f}px -> 靠近到 {_BAIT_STOP_PX:.0f}px 为止 {cand.name}"
                          f"（最近的别的敌人 {near_other:.0f}px）", flush=True)
                    return cand, [], 6
            elif dist < _BAIT_BACK_PX:      # 已经贴太近，装填中也要先退
                away = _Dir.Left if dx > 0 else _Dir.Right
                if abs(dy) > abs(dx):
                    away = _Dir.Up if dy > 0 else _Dir.Down
                if _dir_free(maze, px, py, away):
                    print(f"[勾引] 装填中但已经在 {dist:.0f}px（<{_BAIT_BACK_PX:.0f}）-> 先退 {away.name}", flush=True)
                    return away, [], 6
            return direction, path, flag

        # ② 全都快开炮 -> 退到射程外沿，等它开火
        if soon and soon == len([e for e in ens if e is not None]):
            nd = _near.get("dist", 1e9)
            now = time.time()
            # 等诱别无限等：一直退会变成「在射程边缘来回晃」= 挂机。
            # 等够 _BAIT_WAIT_MAX 就直接压上去逼它开炮。
            if nd < 150.0 and (now - _bait_wait["t"]) < _BAIT_WAIT_MAX:
                if _bait_wait["t"] == 0.0:
                    _bait_wait["t"] = now
                dx, dy = _near.get("dx", 0.0), _near.get("dy", 0.0)
                if abs(dx) >= abs(dy):
                    cands = [_Dir.Left if dx > 0 else _Dir.Right,
                             _Dir.Up if dy > 0 else _Dir.Down]
                else:
                    cands = [_Dir.Up if dy > 0 else _Dir.Down,
                             _Dir.Left if dx > 0 else _Dir.Right]
                for away in cands:
                    if _dir_free(maze, px, py, away):
                        print(f"[勾引] 敌人快开炮（{soon} 个），退到射程边缘等诱 -> {away.name}", flush=True)
                        return away, [], 6
            elif nd < 150.0:
                # 等超时：直接朝它冲，逼它开火
                dx, dy = _near.get("dx", 0.0), _near.get("dy", 0.0)
                cand = (_Dir.Right if dx > 0 else _Dir.Left) if abs(dx) >= abs(dy) \
                    else (_Dir.Down if dy > 0 else _Dir.Up)
                if _dir_free(maze, px, py, cand):
                    print(f"[勾引] 等诱超时 {_BAIT_WAIT_MAX}s，主动压上去逼它开炮 -> {cand.name}", flush=True)
                    _bait_wait["t"] = 0.0
                    return cand, [], 6
        else:
            _bait_wait["t"] = 0.0
        return direction, path, flag

    ZMAP.Map.get_shot_place_direction = _gsd_bait


def _install_anti_corner_patch():
    _orig = AM.ActionManager.take_action_direction

    def _take_anti_corner(self, direction):
        if direction is not None:
            px, py = _near.get("px"), _near.get("py")
            maze = _near.get("maze")
            if px is not None and py is not None and maze is not None:
                h, w = maze.shape
                vx = _DIR_H.get(direction.value[0], 0) or (1 if "D" in direction.value else (-1 if "A" in direction.value else 0))
                vy = _DIR_V.get(direction.value[0], 0) or (1 if "S" in direction.value else (-1 if "W" in direction.value else 0))
                at_left = px < _EDGE_PX
                at_right = px > w - _EDGE_PX
                at_top = py < _EDGE_PX
                at_bottom = py > h - _EDGE_PX
                push_in = False
                if (vx < 0 and at_left) or (vx > 0 and at_right) or (vy < 0 and at_top) or (vy > 0 and at_bottom):
                    push_in = True
                if push_in:
                    cand = _toward_center(maze, px, py)
                    if cand is not None:
                        print(f"[角落] 在({int(px)},{int(py)})还要往 {direction.name} 退 -> 改成回中间 {cand.name}", flush=True)
                        direction = cand
        return _orig(self, direction)

    AM.ActionManager.take_action_direction = _take_anti_corner

# ---------------------------------------------------------------- 卡在墙里要自己挪出来
# 仓库 get_shot_place_direction() 有一支：
#   if (not moveable_maze[player.y, player.x]      # 我在墙里
#           and flee_region[...] == 0
#           and target_region[...] > 0):
#       return None, None, 0                       # ← 原地不动
# 也就是「被挤进墙里之后它就站着不动了」，再也出不来。
# 这里补一支：发现自己在墙里，就朝「外面有路」的方向挪出去。
_orig_gsd = ZMAP.Map.get_shot_place_direction


# ---------------------------------------------------------------- 保持距离 + 防抖
# 仓库的 get_shot_place_direction() 里，射击位评分是
#     decay_array = MAZE_HEIGHT / (distance + MAZE_HEIGHT)
# 距离越近分越高 —— 它天生爱贴脸。这里在外面补一层：
#   · 站在安全射击位（flag=0）但离敌人太近 → 主动退到舒适距离
#   · 0.25 秒内不许反向（子弹一动 flee_region 就变，不然会左右抖）
_MIN_DIST = 30.0         # 像素，约 3 格（只在快撞上时才退，别破坏勾引）
_MAX_DIST = 175.0
_SWITCH_COOLDOWN = 0.25
_last_dir_change = {"t": 0.0, "dir": None}
_OPPOSITE = {"D": "A", "A": "D", "S": "W", "W": "S",
             "SD": "WA", "WA": "SD", "SA": "WD", "WD": "SA",
             "DS": "AW", "AS": "DW", "DW": "AS", "AW": "DS"}


# 仓库的 click() 是 win32api.SetCursorPos + mouse_event —— 会抢用户光标。
# 用户明确要求「不许动光标」，这里把它换成空操作，只打印想点哪里。
def _install_no_cursor_patch():
    def _fake_click(x, y):
        print(f"[跳过点击] 仓库想点 ({x}, {y})，不动光标", flush=True)

    def _fake_set_cursor(x, y):
        pass

    if hasattr(GW, "click"):
        GW.click = _fake_click
    if hasattr(GW, "set_cursor_pos"):
        GW.set_cursor_pos = _fake_set_cursor
    # action_manager / controls 里也各有一份
    for _mod in (AM,):
        if hasattr(_mod, "click"):
            _mod.click = _fake_click
        if hasattr(_mod, "set_cursor_pos"):
            _mod.set_cursor_pos = _fake_set_cursor
    try:
        from zmxy4_treasure_hunt.maze.runtime import controls as _CT
        if hasattr(_CT, "click"):
            _CT.click = _fake_click
        if hasattr(_CT, "set_cursor_pos"):
            _CT.set_cursor_pos = _fake_set_cursor
    except Exception:
        pass


_install_no_cursor_patch()


def _dir_free(maze, x, y, direction, steps=None):
    """沿这个方向每 4 像素采一个点，一路探到 40 像素都通才算通。

    只探 3 个稀疏点会漏掉中间的礁石（8 和 20 都空、12 有石头），
    判成「通」然后一头撞上去 —— 这就是之前「顶着墙更严重」的来源。
    """
    vx, vy = _DIR_VEC.get(direction, (0, 0))
    h, w = maze.shape
    if steps is None:
        steps = range(4, 41, 4)
    for k in steps:
        nx, ny = int(x) + vx * k, int(y) + vy * k
        if not (0 <= nx < w and 0 <= ny < h):
            return False
        if maze[ny, nx]:          # maze 里 1=障碍、0=可走；碰到障碍才不通
            return False
    return True


_ALL4 = None


def _safe_away(maze, px, py, ens, probe=40.0, need_dist=None):
    """挑一个「不撞墙 + 不会靠近任何一个敌人」的单轴方向（当前未被调用，保留备用）。

    用户 m23893：「在躲避一个敌人的时候会贴到另一个敌人脸上」——
    所以不能只看最近那个敌人，要走一步之后重新算「到所有敌人的最小距离」，
    只有不变得更近的方向才算安全。
    """
    global _ALL4
    if _ALL4 is None:
        _ALL4 = [_Dir.Right, _Dir.Down, _Dir.Left, _Dir.Up]
    vec = {_Dir.Right: (1, 0), _Dir.Down: (0, 1), _Dir.Left: (-1, 0), _Dir.Up: (0, -1)}

    def min_dist(x, y):
        best = 1e9
        for e in ens:
            if e is None:
                continue
            d = ((e.x - x) ** 2 + (e.y - y) ** 2) ** 0.5
            if d < best:
                best = d
        return best

    cur = min_dist(px, py)
    scored = []
    for d in _ALL4:
        if not _dir_free(maze, px, py, d):
            continue
        vx, vy = vec[d]
        nx, ny = px + vx * probe, py + vy * probe
        after = min_dist(nx, ny)
        if after < cur - 1.0:          # 走一步反而更近，排除
            continue
        if need_dist is not None and after < need_dist:
            continue
        scored.append((after, d))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    return scored[0][1]


_OPEN = {"t": 0.0, "path": None}


def _install_open_space_patch():
    """被敌人堵在墙角、四周只剩一个方向能走时，绕大圈跑去空旷点。

    用户 m24070：「被敌人堵在墙角没空间躲了应该尝试绕大圈跑到空旷的位置去」。
    只在「贴身(<60px) 且 可走方向 <= 1」时触发，其余情况一律交给仓库原逻辑。
    """
    _orig = ZMAP.Map.get_shot_place_direction

    def _gsd_open_space(self, player, last_direction=None):
        d, p, flag = _orig(self, player, last_direction)
        if flag == 1:                                   # 正在躲子弹，不插手
            return d, p, flag
        nd = _near.get("dist")
        if nd is None or nd > 60.0:                     # 没被贴身
            _OPEN["t"] = 0.0
            _OPEN["path"] = None
            return d, p, flag
        ens = [(float(e.x), float(e.y)) for e in (_near.get("enemies") or []) if e is not None]
        if not ens:
            return d, p, flag
        px, py = float(player.x), float(player.y)
        maze = self.get_maze()
        free = [c for c in _DIR_VEC if _dir_free(maze, px, py, c)]
        if len(free) > 1:                               # 还有退路，常规处理
            _OPEN["t"] = 0.0
            _OPEN["path"] = None
            return d, p, flag
        now = time.time()
        if _OPEN["path"] is None or now - _OPEN["t"] > 1.5:
            best, best_score = None, -1.0
            h, w = maze.shape
            for gy in range(6, h - 6, 10):
                for gx in range(6, w - 6, 10):
                    if maze[gy, gx]:      # 跳过障碍格
                        continue
                    sc = min(((gx - ex) ** 2 + (gy - ey) ** 2) ** 0.5 for ex, ey in ens)
                    if sc > best_score:
                        best_score, best = sc, (gx, gy)
            if best is not None:
                try:
                    path = _ZU.find_path_to_point(maze, (int(px), int(py)), best, stride=8)
                except Exception:                       # noqa: BLE001
                    path = None
                if path:
                    _OPEN["path"] = [(float(a), float(b)) for a, b in path]
                    _OPEN["t"] = now
                    print(f"[脱离] 被堵在 ({px:.0f},{py:.0f})，绕去空旷点 {best}（离敌 {best_score:.0f}）", flush=True)
        path = _OPEN["path"]
        if not path:
            return d, p, flag
        tgt = None
        for a, b in path:
            if ((a - px) ** 2 + (b - py) ** 2) ** 0.5 >= 20.0:
                tgt = (a, b)
                break
        if tgt is None:
            _OPEN["path"] = None
            return d, p, flag
        dx, dy = tgt[0] - px, tgt[1] - py
        want = _Dir.Right if dx > 0 else _Dir.Left
        if abs(dy) > abs(dx):
            want = _Dir.Down if dy > 0 else _Dir.Up
        print(f"[脱离] 往空旷点走 -> {want.name}", flush=True)
        return want, path, 7

    ZMAP.Map.get_shot_place_direction = _gsd_open_space


def _install_keep_distance_patch():
    _orig = ZMAP.Map.get_shot_place_direction

    def _gsd_keep_distance(self, player, last_direction=None):
        direction, path, flag = _orig(self, player, last_direction)
        nd = _near.get("dist")
        if nd is None:
            return direction, path, flag
        # 仓库正在躲子弹（flag=1）时绝不插手 —— 我一旦覆盖方向，
        # 就会把船从「它算好的安全位」推到弹道上，这也是「往弹道上撞」的成因。
        if flag == 1:
            return direction, path, flag
        # 保持距离优先于攻击：只要离得近，无论仓库想进攻还是想站着，都先退。
        # 用户原话：「保持距离的优先级比攻击更高，不要抱着同归于尽的打法」。
        if nd < _MIN_DIST:
            dx, dy = _near.get("dx", 0.0), _near.get("dy", 0.0)
            if abs(dx) >= abs(dy):
                cands = [(_Dir.Left if dx > 0 else _Dir.Right),
                         (_Dir.Up if dy > 0 else _Dir.Down)]
            else:
                cands = [(_Dir.Up if dy > 0 else _Dir.Down),
                         (_Dir.Left if dx > 0 else _Dir.Right)]
            maze = self.get_maze()
            for away in cands:
                if _dir_free(maze, player.x, player.y, away):
                    print(f"[距离] 离敌人 {nd:.0f}px 太近 -> 退 {away.value}", flush=True)
                    return away, [], 5
            # 两侧都是墙：不硬撞，保持仓库原判断
            print(f"[距离] 离敌人 {nd:.0f}px 太近，但两侧都是墙，保持 "
                  f"{(direction.value if direction else '不动')}", flush=True)
            return direction, path, flag
        return direction, path, flag

    ZMAP.Map.get_shot_place_direction = _gsd_keep_distance


def _install_antishake_patch(set_flag=None):
    _orig = AM.ActionManager.take_action_direction

    def _take_antishake(self, direction):
        now = time.time()
        prev = _last_dir_change["dir"]
        if direction is not None and prev is not None and direction.value != prev:
            if _OPPOSITE.get(direction.value) == prev and now - _last_dir_change["t"] < _SWITCH_COOLDOWN:
                # 刚反过向，冷却期内先保持原方向，避免在弹道上抖
                return _orig(self, _Dir(prev) if not isinstance(prev, _Dir) else prev)
            _last_dir_change["t"] = now
            _last_dir_change["dir"] = direction.value
        return _orig(self, direction)

    AM.ActionManager.take_action_direction = _take_antishake


def _gsd_escape_wall(self, player, last_direction=None):
    d, path, flag = _orig_gsd(self, player, last_direction)
    if d is None and flag == 0:
        try:
            maze = self.get_maze()
            h, w = maze.shape
            px, py = int(player.x), int(player.y)
            if 0 <= px < w and 0 <= py < h and maze[py, px]:
                # 在墙里：四个方向各往外走 6 / 14 / 22 像素，看哪边先出来
                best = None
                for cand, (dx, dy) in _DIR_VEC.items():
                    if len(cand.value) != 1:
                        continue
                    for step in (6, 14, 22, 30):
                        nx, ny = px + dx * step, py + dy * step
                        if 0 <= nx < w and 0 <= ny < h and not maze[ny, nx]:
                            if best is None or step < best[0]:
                                best = (step, cand)
                            break
                if best is not None:
                    print(f"[卡墙] 玩家在墙里 ({px},{py})，往 {best[1].name} 挪出去", flush=True)
                    return best[1], None, 4
        except Exception as exc:
            print(f"[卡墙] 处理失败：{exc}", flush=True)
    return d, path, flag


# 停用：卡墙挪出（它把「站在空地」误判成「在墙里」，每帧把方向改成往右）
# ZMAP.Map.get_shot_place_direction = _gsd_escape_wall

_orig_next = ZMAP.Map.get_next_direction
_step = [0]


def _next_logged(self, player, falling_items, last_direction=None):
    d, path = _orig_next(self, player, falling_items, last_direction)
    _step[0] += 1
    if _step[0] % 10 == 1:
        print(
            f"[导航] level={self.level} next_exit={self.next_exit} "
            f"玩家=({player.x},{player.y}) rel={player.relative_center_position()} "
            f"-> {d} path0={path[0] if len(path) else None}",
            flush=True,
        )
    return d, path


ZMAP.Map.get_next_direction = _next_logged

# ---------------------------------------------------------------- 房间形状校验
# 白块位置只能把范围缩到 4~5 套（7 个坐标点挤在一起），真正区分要靠礁石形状：
# 每套 layout 的每一关对应 task_maps 里一张 7x13 障碍图，拿画面里的礁石去比 IoU。
# 实测正确房间 IoU 0.86、次优 0.30，区分度足够。
def _rock_grid(img, rows=7, cols=13):
    """把一帧画面裁成游玩区，按 7x13 分格，返回「这格是不是礁石」的布尔图。"""
    import numpy as np        # 这个文件只在主流程里 import numpy，函数里自己拿一份
    h, w = img.shape[:2]
    crop = img[int(_MAP_U * h):int(_MAP_D * h), int(_MAP_L * w):int(_MAP_R * w)]
    B = crop[:, :, 0].astype(np.int16)
    G = crop[:, :, 1].astype(np.int16)
    R = crop[:, :, 2].astype(np.int16)
    V = crop.max(axis=2).astype(np.int16)
    # 礁石：偏暗 + 青绿（B、G 明显高于 R）；海水亮得多，船是浅木色不满足 G-R。
    mask = (V < 165) & ((B - R) > 20) & ((G - R) > 10)
    ch, cw = mask.shape[0] / rows, mask.shape[1] / cols
    grid = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        for c in range(cols):
            grid[r, c] = mask[int(r * ch):int((r + 1) * ch),
                              int(c * cw):int((c + 1) * cw)].mean() > 0.28
    return grid


# 换关回调里拿不到 voyager，用一个槽把它存出来
_VOYAGER_REF = ["0"]


def _verify_map_index(v, tag="", img=None, level=None):
    if _MM_DECIDED.get("ok"):
        # 开局小地图已经按「白块格 + 路线起点」定案了，别再用礁石形状改判：
        # 礁石图只有 7x13，两间房的相似度经常咬得很近，中途改判会把 next_exit
        # 换掉，船就会「先往下走、进房后又掉头往上」。
        return
    """用当前房间的礁石形状核对 map_index，不一致且证据够强就改判。

    img / level 可以由调用方传进来（换关回调里刚抓的那一帧），
    否则这里会再抓一次 —— 两次抓帧之间场景可能已经变了，证据就对不上了。
    """
    m = getattr(v, "map", None)
    if m is None or getattr(m, "next_exit", None) is None:
        return
    lvl = int(level if level is not None else (getattr(m, "level", 0) or 0))
    if lvl > 3:
        return
    try:
        grid = _rock_grid(img if img is not None else _grab_bgr())
    except Exception as exc:
        print("[地图] 房间校验取帧失败 %s" % exc, flush=True)
        return
    scores = []
    for k in range(7):
        room = int(m.tasks_list[k][lvl])
        tpl = m.task_maps[room]
        inter = int((grid & tpl).sum())
        union = int((grid | tpl).sum()) or 1
        scores.append((inter / union, k))
    scores.sort(reverse=True)
    best_iou, best_k = scores[0]
    # 同一张房间图可能同时属于几套（房间类型跨套复用，实测 layouts6/4/0 都是房间 6），
    # 分数打平时用小地图的 (列数, 行数) 破平局：套的尺寸必须和小地图一致。
    _tie = [k for s, k in scores if s >= best_iou - 0.02]
    if len(_tie) > 1:
        try:
            _g2 = _MM.read_grid(_grab_bgr())
        except Exception:
            _g2 = None
        if _g2:
            _repo2lay = {v: k for k, v in _MM.LAYOUT_TO_MAP.items()}
            _fit = []
            for k in _tie:                        # k 是仓库索引，要转成 scenario 的 layout 下标
                _lay = _MM.LAYOUTS.get(_repo2lay.get(k, -1))
                if _lay and int(_lay["cols"]) == int(_g2["cols"]) \
                        and int(_lay["rows"]) == int(_g2["rows"]):
                    _fit.append(k)
            if len(_fit) == 1:
                best_k = _fit[0]
                print("[地图] 房间形状打平 %s，按小地图 %d列x%d行 取套 %d" % (
                    _tie, _g2["cols"], _g2["rows"], best_k), flush=True)
            else:
                print("[地图] 房间形状打平 %s，小地图 %d列x%d行 也定不下来" % (
                    _tie, _g2["cols"], _g2["rows"]), flush=True)
    cur = getattr(m, "map_index", None)
    cur_iou = next((s for s, k in scores if k == cur), 0.0)
    print("[地图] 房间校验%s level=%d 当前套=%s(IoU %.2f) 最佳套=%d(IoU %.2f)" % (
        ("（" + tag + "）") if tag else "", lvl, cur, cur_iou, best_k, best_iou), flush=True)
    if best_iou < 0.60:
        print("[地图] 房间形状对不上（最高 IoU %.2f < 0.60），这次不改判" % best_iou, flush=True)
        return
    if best_k != cur and best_iou >= 0.60 and best_iou - cur_iou >= 0.20:
        m.map_index = best_k
        m.route = list(m.route_list[best_k])
        m.next_exit = int(m.route[lvl]) if lvl < len(m.route) else m.next_exit
        print("[地图] -> 按房间形状改判为第 %d 套 (next_exit=%d)" % (best_k, m.next_exit), flush=True)


_orig_nl = ZMAP.Map.next_level


def _nl_logged(self):
    getattr(self, "next_level")  # noqa
    before = self.level
    _orig_nl(self)
    print(f"[换关] level {before} -> {self.level}  next_exit={self.next_exit}", flush=True)
    # 新房间的帧不在这里抓：等「丢船 -> 重见玩家」这个三步骤走完再抓，
    # 否则很可能抓到切场景的过渡帧（上一间还没消失 / 黑屏）。
    _SCENE["phase"] = "wait_lost"
    _SCENE["t"] = time.time()
    _SCENE["lost"] = False
    _SCENE["want"] = (int(self.level), None)


# ---- 失败判定：单帧不算，连续 3 帧才认 ----
# 切场景、开大招、黑屏过场都可能让左侧那条灰带短暂命中，单帧就判 -2 会把
# 正常的一局直接毙掉（日志里「进入第 4 关」下一行就是 Failed.）。
_orig_is_failed = GW.GameWindow.is_failed
_FAIL_HITS = {"n": 0}


def _is_failed_stable(self, image):
    ok = _orig_is_failed(self, image)
    _FAIL_HITS["n"] = _FAIL_HITS["n"] + 1 if ok else 0
    if ok and _FAIL_HITS["n"] <= 3:
        print("[失败判定] 命中 %d/3" % _FAIL_HITS["n"], flush=True)
    return _FAIL_HITS["n"] >= 3


GW.GameWindow.is_failed = _is_failed_stable

ZMAP.Map.next_level = _nl_logged

_orig_shot = ZMAP.Map.get_shot_place_direction
_shot_n = [0]


# ---- boss 战保持距离 ----
# 仓库的射击位评分是 MAZE_HEIGHT/(距离+MAZE_HEIGHT)，越近分越高，
# 所以它天生爱贴着 boss 打。这里只给第 5 关（level==4）加一条：
# 离 boss 小于 _BOSS_NEAR 就朝远离方向退，退够 _BOSS_FAR 再交给仓库开炮。
# 退的方向加 0.6 秒滞回，否则 dx 符号一翻就左右摆（之前就是这么抖的）。
_KEEP_NEAR = 40.0     # 普通关：只在快贴身时才退（60 会让它一直退、够不到射击位）
_BOSS_NEAR = 60.0     # boss 关：留更大余量
_BOSS_FAR = 115.0     # 退到这个距离以上才停
_BOSS_KD = {"dir": None, "t": 0.0, "hold": 0.6}


def _boss_keep_distance(self, player):
    """离敌人太近就朝远离方向退。对所有关生效，boss 关留更大余量。

    仓库的射击位评分是 MAZE_HEIGHT/(距离+MAZE_HEIGHT)，越近分越高，
    所以它开局就一路往敌人脸上贴（用户 m25988「开局不勾引直接往敌人脸上贴」）。
    这里只拦「已经比 near 还近」的情况，退到 near 以上就交回仓库；
    flag==0（已经站在能开火的位子上）不会进这个函数，所以不会变成只躲不打。
    """
    lvl = int(getattr(self, "level", 0) or 0)
    near = _BOSS_NEAR if lvl == 4 else _KEEP_NEAR
    nd = _near.get("dist")
    if nd is None or nd >= near:
        _BOSS_KD["dir"] = None
        return None, False
    now = time.time()
    maze = _near.get("maze")
    if maze is None:
        return None, False
    # 滞回：0.6 秒内继续用上次定下的方向，别每帧翻
    if (_BOSS_KD["dir"] is not None and now - _BOSS_KD["t"] < _BOSS_KD["hold"]
            and _dir_free(maze, player.x, player.y, _BOSS_KD["dir"])):
        return _BOSS_KD["dir"], True
    dx, dy = _near.get("dx", 0.0), _near.get("dy", 0.0)
    if abs(dx) >= abs(dy):
        cands = [_Dir.Left if dx > 0 else _Dir.Right,
                 _Dir.Up if dy > 0 else _Dir.Down]
    else:
        cands = [_Dir.Up if dy > 0 else _Dir.Down,
                 _Dir.Left if dx > 0 else _Dir.Right]
    for c in cands:
        if _dir_free(maze, player.x, player.y, c):
            _BOSS_KD["dir"], _BOSS_KD["t"] = c, now
            print("[保持距离] 离敌 %.0f < %.0f，退 %s" % (nd, near, c.name), flush=True)
            return c, True
    return None, False


# ---- 躲子弹别跑太远 ----
# 仓库躲子弹是「找最近的安全格」，有时会挑到很远的地方，船一路狂奔，
# 结果冲到别的敌人面前把仇恨拉过来（用户 m26001「躲敌人子弹一直跑跑太远导致吸引到别的仇恨了」）。
# 这里只在「本来就在躲(flag==1) 且离最近敌人已经超过 _FLEE_MAX」时，
# 把方向换成与远离轴垂直的切向，绕着敌人躲，不再往外冲。
_FLEE_MAX = 90.0      # 320 坐标单位，超过就改成绕行
_FLEE = {"dir": None, "t": 0.0, "hold": 0.4}


# ---- 勾引 ----
# 敌人逼近时不能一味后退（用户 m26034「在敌人逼近的时候一直躲，没有勾引的行为」）。
# 勾引的正确节奏靠敌人自己的装填进度判断：
#   prog = 1 - fire_cool_down / cool_down_time   （0=刚打完，1=马上开炮）
#   prog > 0.7 且距离近 -> 退，把它这一炮骗出来
#   prog < 0.35          -> 它正在装填，压上去打
#   其余                 -> 交回仓库（保持距离那条兜底）
_BAIT_DIST = 45.0
_MIN_HUG = 30.0          # 贴到 30 以内且瞄不上才退（再大就会一直退、到不了射击位）     # 320 坐标单位，贴到这个距离就沿对齐轴退开
_BAIT = {"dir": None, "t": 0.0, "hold": 0.35}


def _pick_away(maze, px, py, ens, cands, step=40.0):
    """在候选方向里挑一个「走一步之后离所有敌人最远」的。

    之前只算「离最近的那个敌人」的远离方向，结果躲开一艘正好撞进另一艘
    （日志里表现为 距 42 -> 41 -> 36 -> 30 一路缩小，然后贴脸被打死）。
    这里对每个候选走 step 像素，算它到**每一个**敌人的最小距离，取最大的。
    顺带优先不通墙的方向。
    """
    best, best_key = None, None
    for c in cands:
        v = _DIR_VEC.get(c)
        if v is None:
            continue
        free = _dir_free(maze, px, py, c)
        nx, ny = px + v[0] * step, py + v[1] * step
        dmin = 1e9
        for e in ens:
            d = ((e.x - nx) ** 2 + (e.y - ny) ** 2) ** 0.5
            if d < dmin:
                dmin = d
        # 通的关键优先（1000 的加权），其次才是离所有敌人更远
        key = (1 if free else 0, dmin)
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best


def _bait_direction(self, player):
    """贴身时沿「对齐轴」后退，别的都不管。

    之前这里管得太多（压上 / 等炮 / 换轴找空位），结果它一直在绕和退，
    船头永远对不准敌人，仓库的 fire_direction 一直是 None —— 日志表现就是
    「全程一炮不开、一直撞墙」（用户 m26074）。

    仓库开火要求同排或同列，所以后退必须**保持对齐轴**：
      · 敌人在横向（|dx|>=|dy|）-> 只用左/右退，绝不换上下
      · 敌人在纵向                 -> 只用上/下退，绝不换左右
    对齐轴上退不了（贴边/礁石）就干脆不接管，让仓库自己去它的射击位。
    """
    nd = _near.get("dist")
    if nd is None or nd >= _BAIT_DIST:
        return None
    ens = [e for e in (_near.get("enemies") or []) if e is not None]
    if not ens:
        return None
    px, py = float(player.x), float(player.y)
    best = None
    for e in ens:
        d = ((e.x - px) ** 2 + (e.y - py) ** 2) ** 0.5
        if best is None or d < best[0]:
            best = (d, e)
    nd2, e = best
    maze = _near.get("maze")
    if maze is None:
        return None
    dx, dy = e.x - px, e.y - py
    now = time.time()
    if (_BAIT["dir"] is not None and now - _BAIT["t"] < _BAIT["hold"]
            and _dir_free(maze, px, py, _BAIT["dir"])):
        return _BAIT["dir"]
    cands = _retreat_plan(dx, dy)
    # 先只在这些候选里挑；都不通再退回「八向里离所有敌人最远的」
    pick = _pick_away(maze, px, py, ens, cands)
    if pick is not None and _dir_free(maze, px, py, pick):
        _BAIT["dir"], _BAIT["t"] = pick, now
        print("[贴身] 距 %.0f < %.0f，退 %s" % (nd2, _BAIT_DIST, pick.name), flush=True)
        return pick
    pick = _pick_away(maze, px, py, ens, list(_DIR_VEC.keys()))
    if pick is not None and _dir_free(maze, px, py, pick):
        _BAIT["dir"], _BAIT["t"] = pick, now
        print("[贴身] 距 %.0f < %.0f，原方向被堵，退 %s" % (nd2, _BAIT_DIST, pick.name), flush=True)
        return pick
    return None


def _tangent_away(self, player):
    """与「敌人->玩家」垂直的方向（绕行），返回可直接用的 Direction 或 None。"""
    maze = _near.get("maze")
    if maze is None:
        return None
    dx, dy = _near.get("dx", 0.0), _near.get("dy", 0.0)
    now = time.time()
    if (_FLEE["dir"] is not None and now - _FLEE["t"] < _FLEE["hold"]
            and _dir_free(maze, player.x, player.y, _FLEE["dir"])):
        return _FLEE["dir"]
    # 敌人在横向上 -> 上下绕；敌人在纵向上 -> 左右绕
    if abs(dx) >= abs(dy):
        cands = [_Dir.Up, _Dir.Down] if dy >= 0 else [_Dir.Down, _Dir.Up]
    else:
        cands = [_Dir.Left, _Dir.Right] if dx >= 0 else [_Dir.Right, _Dir.Left]
    for c in cands:
        if _dir_free(maze, player.x, player.y, c):
            _FLEE["dir"], _FLEE["t"] = c, now
            print("[绕行] 躲子弹已跑出 %.0f 单位(>%.0f)，改绕 %s" % (
                _near.get("dist", 0.0), _FLEE_MAX, c.name), flush=True)
            return c
    return None


def _escape_when_stuck(self, player):
    """仓库在 flag=3/-1 时会返回方向 None（找不到去射击位的路）→ 站着挨打。
    这里只在「它给不出方向」时补一个「远离最近敌人」的移动，不抢它的正常决策。"""
    nd = _near.get("dist")
    if nd is None or nd >= _ESCAPE_DIST:
        return None
    maze = _near.get("maze")
    if maze is None:
        return None
    now = time.time()
    if (_ESC["dir"] is not None and now - _ESC["t"] < _ESC["hold"]
            and _dir_free(maze, player.x, player.y, _ESC["dir"])):
        return _ESC["dir"]
    dx, dy = _near.get("dx", 0.0), _near.get("dy", 0.0)
    if abs(dx) >= abs(dy):
        cands = [_Dir.Left if dx > 0 else _Dir.Right,
                 _Dir.Up if dy > 0 else _Dir.Down]
    else:
        cands = [_Dir.Up if dy > 0 else _Dir.Down,
                 _Dir.Left if dx > 0 else _Dir.Right]
    for c in cands:
        if _dir_free(maze, player.x, player.y, c):
            _ESC["dir"], _ESC["t"] = c, now
            print("[脱困] 仓库给不出方向(距敌 %.0f) -> 往 %s 挪" % (nd, c.name), flush=True)
            return c
    return None


_ESCAPE_DIST = 70.0    # 320 坐标单位，敌人比这近又找不到射击位才挪
_ESC = {"dir": None, "t": 0.0, "hold": 0.5}


def _shot_logged(self, player, last_direction=None):
    # flee_mazes 只有 Map.update() 里才生成，而 env 的 move_to_enemy_and_update()
    # 是「先 get_shot_place_direction、后 self.update()」，进新关 reset() 之后
    # 第一帧就会拿 None 去解包 -> TypeError: cannot unpack non-iterable NoneType。
    # Voyager.update() 在切场景/结算时还会提前 return，也补不上。
    # 所以这里兜底：地图没算过就这一帧不给方向（上层 take_action_direction(None)
    # 不按任何键，紧接着的 self.update() 会把 flee_mazes 算出来）。
    if getattr(self, "flee_mazes", None) is None:
        return None, [], 0
    _t_dec = time.perf_counter()
    _PROF["n"] += 1
    try:
        return _shot_impl(self, player, last_direction)
    finally:
        _PROF["dec"] += time.perf_counter() - _t_dec
        _prof_dump("shot")


def _shot_impl(self, player, last_direction=None):
    # 优先级（从高到低）：
    #   1) 仓库给得出方向 -> 一律放行。它去射击位、绕墙、对齐炮口都靠这个方向，
    #      以前这里被「贴身就退」抢走，船永远到不了射击位、船头对不准敌人，
    #      仓库的 fire_direction 一直是 None，一炮都打不出来，最后被磨死。
    #   2) 只有「贴到脸上（< _MIN_HUG）且这一刻确实瞄不上」才退一口。
    #   3) 仓库给不出方向（flag 3/-1）时才用贴身退 + 脱困兜底。
    #   4) 正在躲子弹时，跑太远就改绕行，别冲进别的敌人怀里。
    d, path, flag = _orig_shot(self, player, last_direction=last_direction)
    _nd = _near.get("dist")

    if flag == 1:
        if _nd is not None and _nd > _FLEE_MAX:
            _tg = _tangent_away(self, player)
            if _tg is not None:
                return _tg, [], 1
        return d, path, flag

    if d is not None:
        if _nd is not None and _nd < _MIN_HUG:
            _can_aim = False
            try:
                _can_aim = self.fire_direction(player, _near.get("enemies") or []) is not None
            except Exception:
                _can_aim = False
            if not _can_aim:
                _bt = _bait_direction(self, player)
                if _bt is not None:
                    print("[贴身] 距 %.0f < %.0f 且瞄不上 -> 退 %s" % (
                        _nd, _MIN_HUG, _bt.name), flush=True)
                    return _bt, [], 5
        return d, path, flag

    if _nd is not None and _nd < _BAIT_DIST:
        _bt = _bait_direction(self, player)
        if _bt is not None:
            return _bt, [], 5
    if flag in (3, -1):
        _e = _escape_when_stuck(self, player)
        if _e is not None:
            return _e, [], 5
    # 这里再兜一层：仓库给不出方向、贴身退也退不了的时候，至少朝「离所有敌人最远」
    # 的一格挪一下 —— 站着不动等于白挨打（用户 m26985：第四关见了 boss 没动被打死）。
    if _nd is not None:
        _ens = [e for e in (_near.get("enemies") or []) if e is not None]
        if _ens:
            _mz = self.get_maze()
            _mv = _pick_away(_mz, player.x, player.y, _ens, list(_DIR_VEC))
            if _mv is not None and _dir_free(_mz, player.x, player.y, _mv):
                _shot_n[0] += 1
                if _shot_n[0] % 5 == 1:
                    print("[无方向] flag=%d 距敌 %.0f，仓库不给方向 -> 挪 %s" % (
                        flag, _nd, _mv.name), flush=True)
                return _mv, [], 5
    _shot_n[0] += 1
    if _shot_n[0] % 10 == 1:
        print(
            f"[战斗] 玩家=({player.x},{player.y}) 敌={len(_near.get('enemies') or [])} "
            f"-> 方向={d} flag={flag}",
            flush=True,
        )
    return d, path, flag


ZMAP.Map.get_shot_place_direction = _shot_logged

# 上一次被 kill 时 KEYUP 可能没发出去，游戏里会残留「一直按着某个方向」。
# 启动先无条件把所有键松一遍。
_clear = _ct.windll.user32
for _ch in ("W", "A", "S", "D", "J"):
    _clear.PostMessageW(HWND, 0x101, ord(_ch), 0)
    _clear.PostMessageW(HWND, 0x101, ord(_ch), 0xC0000001)
print("窗口", TITLE, BOX, flush=True)
v = Voyager(render_enabled=False)
_VOYAGER_REF[0] = v
# 传可玩区域当 window_box：仓库内部按这个尺寸把画面映射成 320x200，
# 传整窗会把边框算进去，玩家坐标系统性偏移。
# ---- 丢船保护 ----
# 仓库的 filter_player 在认不到玩家船时会沿用上一次的框（game_window.py:251），
# 于是它拿着旧坐标继续按同一个方向键，船就一路顶着墙。
# 这里只加一个「连续丢船就先松手停住」的保护，不改它的决策逻辑。
_lost_pl = {"n": 0}
_orig_filter_player = GW.GameWindow.filter_player


# ---- 切场景「三步骤」判定（用户 m27025 的提法）----
# 换关的过程一定是：还能看到玩家 -> 看不到玩家（加载）-> 又能看到玩家（新房间好了）。
# 与其拍脑袋 sleep(1.5) 猜「新房间渲染完没有」，不如就等这个三步骤走完：
# 走到第三步时才抓帧做房间校验，顺便把 new_level 放掉。
_SCENE = {"phase": "idle", "t": 0.0, "lost": False, "want": None}


def _scene_on_player(has_player):
    """每帧喂一次「这一帧认没认到玩家」，驱动三步骤状态机。"""
    now = time.time()
    if _SCENE["phase"] != "wait_lost":
        return
    if not has_player:
        if not _SCENE["lost"]:
            _SCENE["lost"] = True
            print("[场景] 认不到玩家了（切场景中）", flush=True)
        return
    if _SCENE["lost"]:
        _scene_ready(now, "玩家重新出现")
    elif now - _SCENE["t"] > 2.5:
        _scene_ready(now, "2.5 秒内没出现「丢船」直接放行")


def _scene_ready(now, why):
    _SCENE["phase"] = "idle"
    print("[场景] 新房间就绪（%s，耗时 %.2fs）" % (why, now - _SCENE["t"]), flush=True)
    v = _VOYAGER_REF[0]
    if getattr(v, "new_level", False):
        v.new_level = False
    if _SCENE["want"] is not None:
        lvl, _ = _SCENE["want"]
        _SCENE["want"] = None
        try:
            img = _zg.grab(HWND, W, H)
            cv2.imwrite("debug/real/level_%d.png" % lvl, img)
            print("[场景] 已存 debug/real/level_%d.png" % lvl, flush=True)
            _verify_map_index(v, "换关", img=img, level=lvl)
        except Exception as exc:
            print("[场景] 新房间抓帧失败 %s" % exc, flush=True)


def _patched_filter_player(self, det):
    _has = len(det.get("player") or []) > 0
    if _has:
        _lost_pl["n"] = 0
    else:
        _lost_pl["n"] += 1
    _scene_on_player(_has)
    return _orig_filter_player(self, det)


GW.GameWindow.filter_player = _patched_filter_player

# 逐帧耗时拆解：GameWindow.update() 才是真正的「感知」（截图+YOLO+状态判断），
# Voyager.update() 包着它并追加实体跟踪。之前只量了 Map.update，所以
# FPS 8 却对不上账 —— 这里把这两段也计进去。
_orig_gw_update = GW.GameWindow.update


def _timed_gw_update(self):
    _t = time.perf_counter()
    try:
        return _orig_gw_update(self)
    finally:
        _PROF["gwu"] += time.perf_counter() - _t


GW.GameWindow.update = _timed_gw_update

_orig_voyager_update = EV.Voyager.update


def _timed_voyager_update(self):
    _t = time.perf_counter()
    try:
        return _orig_voyager_update(self)
    finally:
        _PROF["vp"] += time.perf_counter() - _t


EV.Voyager.update = _timed_voyager_update


_orig_take_action = AM.ActionManager.take_action_direction


def _patched_take_action(self, direction):
    # 这里原来写的是「连续 2 帧没认到玩家就清键并 return」——
    # 等于每次 boss 贴脸挡住玩家船时，把方向键和开火全部吞掉，船原地挨打。
    # 现在只提醒，照常动作。
    if _lost_pl["n"] >= 2:
        if not _lost_pl.get("_warned"):
            print("[丢船] 连续 %d 帧没认到玩家，仍按上一帧位置继续操作" % _lost_pl["n"], flush=True)
            _lost_pl["_warned"] = True
    elif _lost_pl.get("_warned"):
        _lost_pl.pop("_warned", None)
    return _orig_take_action(self, direction)


AM.ActionManager.take_action_direction = _patched_take_action

v.init_window((int(_MAP_L * W), int(_MAP_U * H), int(_MAP_R * W), int(_MAP_D * H)),
              focus_game=False)
print("窗口初始化完成", v.window_size, flush=True)
import time as _t

def _auto_loop_end_on_success(voyager, target=1):
    """照仓库 auto_loop 跑，但**一旦通关（status == 1）就直接收工**。

    仓库的 auto_loop 在成功之后还会调 `recover(1)`：点结算面板、再自动点「起航」
    重新进航海，然后才返回 —— 对「跑一局就结束任务」来说是多余动作，而且会把游戏
    丢在起航界面上。这里成功就 return，让 CustomAction 返回 True、pipeline 走
    StopTask 收掉整个任务。
    失败（-2 / -3 / -4）仍按原样 recover 后重试。
    """
    success_count = 0
    print("Start Loop", flush=True)
    while success_count < target:
        print("Success %d/%d" % (success_count, target), flush=True)
        status_code = voyager.auto_run()
        if status_code == 1:
            print("[完成] 挑战成功，结束任务（不回地图、不再起航）", flush=True)
            return True
        time.sleep(1)
        voyager.game_window.recover(status_code)
    return False


_idx = None
for _i in range(12):
    try:
        _idx = v.game_window.get_map_index_at_start()
        break
    except Exception as exc:
        print(f"识别大地图第 {_i + 1} 次失败：{exc}", flush=True)
        _t.sleep(0.4)
if _idx is None:
    raise SystemExit("小地图一直认不出白块")
import os as _os
_force = _os.environ.get("ZM_MAP_INDEX")
if _force is not None:
    _idx = int(_force)
    print(f"强制 map_index = {_idx}", flush=True)
print("识别到大地图：", _idx, flush=True)
print("本次 map_index =", v.game_window.map_index, flush=True)
# 先确认在副本里 —— 主城/背包/活动面板一律不按键。
# 之前没有这道检查，在主城画面上把头像认成玩家船，对着它按了 280 帧。
# 开局可能在加载页/过场，等最多 12 秒，别一上来就判死。
_probe = _grab_bgr()
_waited = 0.0
while not in_voyage(_probe) and _waited < 12.0:
    _clear_keys()
    time.sleep(0.5)
    _waited += 0.5
    _probe = _grab_bgr()
if not in_voyage(_probe):
    _clear_keys()
    print("等了 %.0f 秒还没进副本（海面太少），退出" % _waited, flush=True)
    raise SystemExit(0)
print("在副本里（等了 %.1f 秒）" % _waited, flush=True)
print("在副本里，海面占比 %.2f" % (
    __import__("numpy").mean(
        (((__import__("cv2").cvtColor(_probe, __import__("cv2").COLOR_BGR2HSV)[:, :, 0] > 15)
          & (__import__("cv2").cvtColor(_probe, __import__("cv2").COLOR_BGR2HSV)[:, :, 0] < 40))
         | ((__import__("cv2").cvtColor(_probe, __import__("cv2").COLOR_BGR2HSV)[:, :, 0] > 85)
            & (__import__("cv2").cvtColor(_probe, __import__("cv2").COLOR_BGR2HSV)[:, :, 0] < 115))))), flush=True)
_verify_map_index(v, "进副本")

try:
    # 开跑前先自检按键通路：按 D 0.6 秒，看玩家船有没有动。
# 不动就说明 PostMessage 没进游戏（窗口句柄变了 / 游戏在加载 / 有键残留），
# 这时候硬跑只会看到「撞墙不动」，不如直接停下报错。
    def _player_px(image):
        for d in _DET(image):
            if d["label"] == "player":
                bx, by, bw, bh = d["box"]
                return bx + bw / 2, by + bh / 2
        return None

    def _selftest():
        _clear_keys()
        time.sleep(0.3)
        a = _player_px(_grab_bgr())
        if a is None:
            return False, "自检失败：认不到玩家船"
        # 船可能正贴着某一侧的墙，按那一边当然不动 —— 四个方向轮流试，
        # 只要有一个方向能挪动就说明按键通路是好的。
        for ch in ("D", "A", "S", "W"):
            _u32.PostMessageW(HWND, 0x100, ord(ch), 0)
            time.sleep(0.5)
            _u32.PostMessageW(HWND, 0x101, ord(ch), 0)
            time.sleep(0.2)
            b = _player_px(_grab_bgr())
            if b is None:
                continue
            moved = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            if moved > 5.0:
                return True, f"按键自检：按 {ch} 位移 {moved:.1f} 像素"
            a = b
        return False, "自检失败：四个方向都推不动（可能贴死在角落或按键没进游戏）"

    _ok, _msg = _selftest()
    print(_msg, flush=True)
    if not _ok:
        raise SystemExit("按键没进游戏，先别跑（检查窗口句柄/是否有加载遮挡）")
    _install_can_next_level_patch(EV)
    # _install_open_space_patch()
    # _install_keep_distance_patch()
    # _install_antishake_patch()
    # anti_corner（贴边就朝中间）已停用 —— 用户要求「只有卡死了才往中间走」，
    # 那件事由 _stuck_escape_direction 里的贴边判断负责。
    # bait（勾引开炮）停用：它的「等诱超时 -> 主动压上去逼它开炮」每帧重置计时器，
    # 实际表现是锁死一个方向持续往敌人身上贴，和 keep_distance 的「太近就退」
    # 来回拉锯（日志里 [勾引] 压上去 Left 与 [距离] 退 D 交替），直接导致贴脸挨打。
    # _install_bait_patch()
    _auto_loop_end_on_success(v, 1)
finally:
    # 进程被 kill 时 keybd_event 的 KEYUP 不会发出，游戏里会残留「一直按着某个方向」
    # → 船自己顶着墙跑。这里无条件把 WASD+J 全部松掉。
    for _ch in ("W", "A", "S", "D", "J"):
        _u32.PostMessageW(HWND, 0x101, ord(_ch), 0)
        _u32.PostMessageW(HWND, 0x101, ord(_ch), 0xC0000001)
    print("已释放所有按键", flush=True)
