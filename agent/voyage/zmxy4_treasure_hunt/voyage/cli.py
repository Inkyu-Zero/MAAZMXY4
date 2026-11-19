"""航海寻宝参数解析与启动；解析帮助时不初始化游戏环境。"""

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="航海寻宝，支持监控窗口和只读模式")
    parser.add_argument("target", nargs="?", type=int, default=3, help="目标胜利次数，默认 3")
    parser.add_argument("--debug", action="store_true", help="显示监控窗口，不改变自动操作模式")
    parser.add_argument("--read-only", action="store_true", help="只更新感知和建议路径，不发送鼠标或键盘指令")
    parser.add_argument("--map-index", type=int, choices=range(7), help="只读模式下指定大地图编号，默认从画面识别")
    parser.add_argument("--level", type=int, choices=range(5), default=0, help="只读模式下的起始关卡，默认 0")
    args = parser.parse_args(argv)
    if args.target <= 0:
        parser.error("target 必须为正整数")

    import cv2
    from .env import Voyager, auto_loop

    voyager = Voyager(render_enabled=args.debug)
    voyager.init_window(focus_game=not args.read_only)
    if args.read_only:
        map_index = args.map_index if args.map_index is not None else voyager.game_window.get_map_index_at_start()
        voyager.map.reset(map_index)
        for _ in range(args.level):
            voyager.map.next_level()
        print("只读调试：请手动操作游戏，Ctrl+C 退出。")
        try:
            while voyager.debug_step() in (0, -3):
                pass
        except KeyboardInterrupt:
            return 130
        finally:
            if args.debug:
                cv2.destroyWindow("Monitor")
    else:
        auto_loop(voyager, args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
