# 每周迷宫地图

从 `images/maze/ft0.png` 至 `ft6.png` 解析，按用户约定编号：

| 文件 | 星期 | 来源 |
| --- | --- | --- |
| `0.json` | 星期日 | `ft0.png` |
| `1.json` | 星期一 | `ft1.png` |
| `2.json` | 星期二 | `ft2.png` |
| `3.json` | 星期三 | `ft3.png` |
| `4.json` | 星期四 | `ft4.png` |
| `5.json` | 星期五 | `ft5.png` |
| `6.json` | 星期六 | `ft6.png` |

每张图包含 14×14 个房间，转换为现有导航使用的 29×29 格网。
坐标均为 `[行, 列]`，从 0 开始；奇数行、奇数列对应房间中心。

| 字符 | 数值 | 含义 |
| --- | --- | --- |
| `#` | 2 | 墙壁或格网交点 |
| `.` | 1 | 普通房间 |
| `+` | 3 | 两个相邻房间之间的可通行连接 |
| `T` | 4 | 宝箱候选位置 |

`grid` 保存字符串地图，`legend` 保存字符到现有数值编码的映射；
`treasures` 保存 8 个宝箱的格网坐标，与 `grid` 中的 `T` 一致。
人物起点由程序初始化为 `(1, 1)`，地图文件不保存 `start` 字段。宝箱真假无法从这些图片判断。
交点按现有导航约定统一记为墙格；这是房间连通图，不是像素级碰撞图。

## 读取

自动程序已接入这些模板：启动时按北京时间加载当日地图，不再探索。
局部画面只用于定位和移动纠偏，触发假宝箱后只更新本局内存状态。

资源随 `zmxy4_treasure_hunt.maze.maps` 包分发，不依赖当前工作目录或外置 `maps/` 目录。
读取当天地图：

```python
from zmxy4_treasure_hunt.maze.navigation import load_weekday_map

template = load_weekday_map()  # 北京时间，星期日为 0
maze = template.cells.copy()  # 本局可变状态；原模板只读
```

也可使用 `load_weekday_map(0)` 显式读取星期日地图。
本局假宝箱消失时修改内存中的 `maze`，保留 JSON 模板供下次运行加载。

## 分发与校验

七张 JSON 位于 `src/zmxy4_treasure_hunt/maze/maps/`。
读取使用 `importlib.resources`，支持源码、安装包和 PyInstaller 冻结程序。
项目自带 PyInstaller hook 收集七张 JSON 及 `assets/edge_mask.png`，详见
[打包说明](../../../../docs/maze.md)。

地图由 `images/maze/ft0.png` 至 `ft6.png` 解析：按格网连接位置区分沙路与石墙，
再用 `ft0.png` 的宝箱纹理识别其余宝箱。每张图的 196 个房间均连通，宝箱候选数量为 8。
`tests/maze/test_map_templates.py` 校验地图尺寸、编码、星期、宝箱坐标和连通性。
