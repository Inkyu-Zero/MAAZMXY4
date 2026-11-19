# 用户要求过的修复清单（改任何东西前，逐条核对）

| # | 要求（原话或原意） | 实现位置 | 状态 |
|---|---|---|---|
| 1 | **不要锁单轴，八向全开** | 无任何方向压制代码（`_take_no_diag_when_close` 只做「顶住就横挪」，不改方向） | **硬性规则** |
| 2 | 不要撞墙 / 不要无脑顶墙 | `_gsd_escape_wall`（卡在墙里朝外挪）+ `_stuck_escape_direction`（连着 8 帧没动就横挪） | 有 |
| 3 | 不要贴脸，保持距离（优先级高于攻击） | `_install_keep_distance_patch`（`_MIN_DIST = 30`，只躲贴脸；`flag==1` 躲子弹时不碰） | 有 |
| 4 | 战斗没结束不许走 | `_install_can_next_level_patch`（连续 `_EMPTY_NEED = 6` 帧空场才算清场） | 有 |
| 5 | 不要在敌人弹道上鬼畜 | `_install_antishake_patch`（0.25s 内不许反向） | 有 |
| 6 | 丢船要停住，别拿旧坐标乱按 | `filter_player` 包裹 + 连续 2 帧丢船就清键 | 有 |
| 7 | 只按固定路径走，不要自己找门 | `ROUTE_LIST` + `Map.get_next_direction` | 有 |
| 8 | 地图套数要用 MAA 判（白块 + 灰格形状） | `_gmi_by_shape` + `maa_white.white_center`，开局锁定一次 | 有 |
| 9 | 开局不在副本就等，别直接退 | `in_voyage` 最多等 12 秒 | 有 |
| 10 | 被逼到墙角要绕大圈去空旷处 | `_install_open_space_patch`（贴脸 + 可走方向 ≤ 1 才触发） | 有 |
| 11 | 勾引 = 保持距离站弹道线，不是凑脸 | `_install_bait_patch`（`_BOSS_NEAR=150` / `_BOSS_FAR=210` / 对齐弹道线） | 有 |
| 12 | 子弹危险区要覆盖整条射线 | `get_bullet_danger_bounds` 改成到墙 | 有 |
| 13 | 识别图都要给用户看 | 每次贴 `debug/real/*.png` | 有 |
| 14 | 不抢焦点、不动光标 | 仓库的 `click`/`set_cursor_pos` 全局替换成空操作；按键走 PostMessage | 有 |
| 14b | **截图只要 FramePool，不要三层兜底** | `_capture` 只调 `_grab_via_maa()`；`Win32Controller(..., screencap_method=FramePool)`（默认 Background=FramePool|PrintWindow 会偷偷退到 PrintWindow） | 有 |
| 17 | **不许随便加兜底来掩盖报错** | 失败就报错，让问题暴露出来；不许 try/except 吞掉、不许 fallback 成别的方式 | **硬性规则** |
| 15 | 全程中文思考 | — | 有 |
| 16 | 不许自己起进程，用 MFA 跑 | — | 有 |

## 改代码前的流程

1. 先读这张表，确认要改的地方不违反 1、3、4、14
2. 改完跑 `python -c "import voyage"`（只 import 不跑主逻辑）确认没有 NameError / TypeError
3. 交回用户测，**不自己起进程**

## 已经犯过的错（不许再犯）

- **给抓图加三层兜底（MAA→PrintWindow→dxcam）掩盖了「窗口一移动 PrintWindow 就失败」这个真问题** → 用户 m24529「以后不要随便加别的兜底来掩盖报错」
- 把已确认有问题的机制（仓库贴脸逻辑）当兜底垫在新机制下面（m24418）


- 锁单轴 / 把斜向压成四向 → 用户要八向全开（两次）
- 改了 `real_adapter.py` 但 MFA 跑的是 `run_zmxy4.py`（改错文件）
- 换实现基线时没把已修好的东西迁过去（保持距离/避墙全丢）
- 内联代码后没检查「先用后定义」（连崩 wt / user32 / GW / EV / OnnxDetector 五次）

## 删文件前必做（2026-11-20 教训）

删掉 `_old/` 时只把 5 个 `.py` 拷回，漏了 `zm_nav.py` / `zm_fight.py` / `zm_route.py`，
而 `real_adapter.py:27` 是 `import zm_nav as _zn` → 任务一启动就
`ModuleNotFoundError: No module named 'zm_nav'`。

所以删任何文件之前，先跑这个检查（列出所有本地 import 是否有对应文件）：

```python
import ast, io, glob, os
ALLOW = {'cv2','numpy','ctypes','win32api','win32con','onnxruntime','dxcam','scipy','PIL',
         'os','sys','time','json','ast','glob','io','math','random','typing','pathlib',
         'subprocess','threading','collections','re','shutil','heapq','argparse','types',
         '__future__','maa','voyage'}
bad = {}
for f in glob.glob('*.py'):
    src = io.open(f, encoding='utf-8').read()
    for n in ast.walk(ast.parse(src)):
        name = None
        if isinstance(n, ast.Import):
            for a in n.names:
                name = a.name.split('.')[0]
                if name not in ALLOW and not (os.path.exists(name + '.py') or os.path.isdir(name)):
                    bad.setdefault(name, []).append(f)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            name = n.module.split('.')[0]
            if name not in ALLOW and not (os.path.exists(name + '.py') or os.path.isdir(name)):
                bad.setdefault(name, []).append(f)
print('缺失:', bad or '无')
```
