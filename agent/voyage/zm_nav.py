"""照抄 zmxy4 的赶路逻辑：直接调它 Map.get_next_direction()。

不自己算路、不自己找门——仓库怎么走就怎么走。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zmxy4_treasure_hunt.voyage.core.constants import (   # noqa: E402
    EXIT_DOWN, EXIT_LEFT, EXIT_RIGHT, EXIT_UP, MAZE_HEIGHT, MAZE_WIDTH,
)
from zmxy4_treasure_hunt.voyage.core.direction import Direction  # noqa: E402
from zmxy4_treasure_hunt.voyage.core.map import Map              # noqa: E402

ZM = Map(render_enabled=False)


class Player:
    """仓库 Player 的最小替身：只要 x / y / xn / yn。"""

    def __init__(self, px: float, py: float):
        self.x = int(round(px))
        self.y = int(round(py))
        self.xn = self.x / float(MAZE_WIDTH)
        self.yn = self.y / float(MAZE_HEIGHT)

    def relative_center_position(self):
        dx = dy = 0
        if self.xn > EXIT_RIGHT:
            dx = 1
        elif self.xn < EXIT_LEFT:
            dx = -1
        if self.yn > EXIT_DOWN:
            dy = 1
        elif self.yn < EXIT_UP:
            dy = -1
        return dx, dy


_last: Direction | None = None


def start(map_index: int) -> None:
    global _last
    ZM.reset(map_index)
    _last = None


def next_keys(cell_x: float, cell_y: float, grid_cols: int = 30, grid_rows: int = 19) -> str:
    """给 30×19 的格子坐标，返回该按的键（'w'/'s'/'a'/'d'/'wd'…），空串表示不动。"""
    global _last
    px = cell_x / float(grid_cols) * MAZE_WIDTH
    py = cell_y / float(grid_rows) * MAZE_HEIGHT
    player = Player(px, py)
    direction, _path = ZM.get_next_direction(player, [], last_direction=_last)
    if direction is None:
        return ""
    _last = direction
    return str(direction.value).lower()


def bump_level() -> None:
    """换了一间房：level+1，next_exit 跟着更新。"""
    ZM.level = min(ZM.level + 1, len(ZM.route) - 1)
    ZM.next_exit = ZM.route[ZM.level]


if __name__ == "__main__":
    for idx in range(7):
        start(idx)
        keys = next_keys(14.8, 16.5)
        print(f"布局{idx + 1} 起点(14.8,16.5) -> 键 {keys!r}")
