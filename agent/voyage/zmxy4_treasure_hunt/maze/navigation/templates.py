"""按北京时间的星期编号读取完整地图，星期日为 0。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from importlib.resources import files
from pathlib import Path

import numpy as np

from .. import maps

CHINA_TIME = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class MapTemplate:
    weekday: int
    name: str
    cells: np.ndarray


def current_weekday(day=None):
    day = day if day is not None else datetime.now(CHINA_TIME).date()
    return day.isoweekday() % 7


def load_weekday_map(weekday=None, maps_dir=None):
    weekday = current_weekday() if weekday is None else weekday
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("weekday 必须为 0（星期日）至 6（星期六）")
    directory = files(maps) if maps_dir is None else Path(maps_dir)
    path = directory.joinpath(f"{weekday}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("weekday") != weekday:
        raise ValueError(f"地图版本或星期编号不匹配：{path}")
    if data.get("size") != [29, 29] or len(data["grid"]) != 29 or any(len(row) != 29 for row in data["grid"]):
        raise ValueError(f"地图必须为 29×29：{path}")
    if data.get("legend") != {"#": 2, ".": 1, "+": 3, "T": 4}:
        raise ValueError(f"地图编码不正确：{path}")
    try:
        cells = np.array([[data["legend"][cell] for cell in row] for row in data["grid"]], dtype=np.uint8)
    except KeyError as exc:
        raise ValueError(f"地图包含未知字符：{path}") from exc
    if (not np.all(cells[[0, -1], :] == 2) or not np.all(cells[:, [0, -1]] == 2)
            or not np.all(cells[::2, ::2] == 2)
            or not np.all(np.isin(cells[1::2, 1::2], [1, 4]))):
        raise ValueError(f"地图边界或房间格不正确：{path}")
    if np.argwhere(cells == 4).tolist() != data.get("treasures"):
        raise ValueError(f"宝箱坐标与地图不匹配：{path}")
    cells.flags.writeable = False
    return MapTemplate(weekday, data["name"], cells)
