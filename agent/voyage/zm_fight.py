"""照抄 zmxy4 的寻路 + 战斗：直接用它自己的 Map / Player / Enemy / Bullet。

不是我设计的：整条链路就是 env.py 的
    fire_direction = map.fire_direction(player, enemy_list)
    direction, path, flag = map.get_shot_place_direction(player, last_direction)
两块拼起来，外面只负责把 YOLO 的框换成它要的形状。
"""
from zmxy4_treasure_hunt.voyage.core.map import Map
from zmxy4_treasure_hunt.voyage.core.entities import Bullet, Enemy, EnemyList, Player

ZM = Map(render_enabled=False)
_ENEMIES = EnemyList()
_BULLETS = []


class Box:
    """模仿 ultralytics 的检测框：只要 xywhn / xyxyn。"""

    def __init__(self, x1, y1, x2, y2, w, h):
        self.xywhn = ((x1 + x2) / 2.0 / w, (y1 + y2) / 2.0 / h, (x2 - x1) / w, (y2 - y1) / h)
        self.xyxyn = (x1 / w, y1 / h, x2 / w, y2 / h)


def start(map_index: int) -> None:
    ZM.reset(int(map_index))


def bump_level() -> None:
    ZM.next_level()


def _boxes(dets, w, h, label):
    out = []
    for d in dets:
        if d["label"] != label:
            continue
        x, y, bw, bh = d["box"]
        out.append(Box(x, y, x + bw, y + bh, w, h))
    return out


def step(dets, w, h, last_dir=None, prev_bullets=None):
    """照 env.py 的顺序算一次：开火方向 + 移动方向。"""
    maze = ZM.get_maze()
    pbox = _boxes(dets, w, h, "player")
    if not pbox:
        return None, None
    player = Player(pbox[0], maze)

    enemies = EnemyList()
    for b in _boxes(dets, w, h, "enemy"):
        enemies.append(Enemy(b, maze))

    bullets = []
    prev = {(round(b[1], 2), round(b[2], 2)): b for b in (prev_bullets or [])}
    for d in dets:
        if d["label"] != "bullet":
            continue
        x, y, bw, bh = d["box"]
        xn, yn = (x + bw / 2) / w, (y + bh / 2) / h
        axis = 0
        best = None
        for (px, py), pb in prev.items():
            dist = abs(px - xn) + abs(py - yn)
            if dist < 0.06 and (best is None or dist < best[0]):
                best = (dist, pb)
        if best is not None:
            axis = 0 if abs(xn - best[1][1]) >= abs(yn - best[1][2]) else 1
        bullets.append(Bullet((xn, yn, axis)))

    ZM.update(player, bullets, enemies)
    fire_dir = ZM.fire_direction(player, enemies)
    direction, path, flag = ZM.get_shot_place_direction(player, last_direction=last_dir)
    keys = direction.value.lower() if direction is not None else ""
    return keys, fire_dir
