"""照抄 zmxy4 的寻路：直接引用它的 Map 与 maze_utils。

不再自己写 30×19 的网格逻辑——那是「撞墙徘徊」的根源。
坐标系统一用它的 320×200 像素网格，玩家位置由 940×590 的画布按比例换算。
"""
import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np

# 仓库代码已经 cp 到本项目里（不是 import 外面的路径）
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from zmxy4_treasure_hunt.voyage.core.map import Map as _ZMap          # noqa: E402
from zmxy4_treasure_hunt.voyage.core import maze_utils as _ZU         # noqa: E402
from zmxy4_treasure_hunt.voyage.core.direction import Direction       # noqa: E402

MAZE_W, MAZE_H = 320, 200
EXIT_UP, EXIT_DN = int(round(0.40 * MAZE_H)), int(round(0.54 * MAZE_H))
EXIT_LF, EXIT_RT = int(round(0.46 * MAZE_W)), int(round(0.57 * MAZE_W))

# route_list 的方向码：0=右 1=下 2=上 3=左 4=无
_CODE_SIDE = {0: "右", 1: "下", 2: "上", 3: "左", 4: None}

_MAP = _ZMap(render_enabled=False)


def route_codes(map_index: int) -> list[int]:
    return list(_MAP.route_list[map_index])


def reset(map_index: int) -> None:
    _MAP.reset(map_index)


def exit_region(code: int) -> np.ndarray:
    """出口带做成目标区域（照 init_map_template 的 EXIT_* 比例）。"""
    # init_map_template 是这样挖的：
    #   map_template[exit_up:exit_dn, :] = 0   → 整行挖掉，左右两边敞开
    #   map_template[:, exit_lf:exit_rt] = 0   → 整列挖掉，上下两边敞开
    # 所以「下出口」是 x∈[147,182] 这一整条竖带的最底端，不是中部。
    region = np.zeros((MAZE_H, MAZE_W), dtype=np.bool_)
    if code == 1:      # 下
        region[MAZE_H - 10:MAZE_H, EXIT_LF:EXIT_RT] = True
    elif code == 2:    # 上
        region[0:10, EXIT_LF:EXIT_RT] = True
    elif code == 3:    # 左
        region[EXIT_UP:EXIT_DN, 0:10] = True
    elif code == 0:    # 右
        region[EXIT_UP:EXIT_DN, MAZE_W - 10:MAZE_W] = True
    return region


def next_direction(px: float, py: float, code: int, last_dir=None):
    """给玩家在 940×590 里的像素坐标，返回要走的方向（Direction）或 None。"""
    maze = _MAP.get_maze()
    dmaze = _MAP.get_distance_maze()
    player = SimpleNamespace(x=int(round(px)), y=int(round(py)))
    if not maze[player.y, player.x]:
        near = _ZU.find_nearest_passable_point(maze, (player.x, player.y))
        if near is None:
            return None
        player.x, player.y = int(near[0]), int(near[1])
    region = exit_region(code)
    path = _ZU.find_best_path_to_region(
        maze, (player.x, player.y), region,
        distance_maze=dmaze, hate_factor=5, last_direction=last_dir,
    )
    if not path:
        return None
    return _MAP.get_direction_from_next_point(player, path[0])


def to_keys(direction) -> str:
    """Direction → 'w'/'s'/'a'/'d'/'sd' 这类按键串。"""
    if direction is None:
        return ""
    return str(direction.value).lower()


def scale(x: float, y: float, w: int = 940, h: int = 590) -> tuple[float, float]:
    """画布像素 → 320×200 网格像素。"""
    return (x / w * MAZE_W, y / h * MAZE_H)


if __name__ == "__main__":
    for idx in range(7):
        reset(idx)
        codes = route_codes(idx)
        print(f"布局{idx + 1} 路线码 {codes} → "
              + " / ".join(_CODE_SIDE[c] or "无" for c in codes))
