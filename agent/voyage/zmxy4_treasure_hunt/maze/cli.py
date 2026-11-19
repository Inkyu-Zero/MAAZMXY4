"""迷宫寻宝参数解析与启动。"""

import argparse
import math


def main(argv=None):
    parser = argparse.ArgumentParser(description="迷宫自动寻宝（DXcam BGR 截图）")
    parser.add_argument("--interval", type=float, default=0.05, help="最小循环间隔，单位秒，默认 0.05")
    parser.add_argument("--debug", action="store_true", help="显示左上角监控图，并在识别或定位失败时保存诊断现场")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval 必须为正数且有限")

    from .ft import FindTreasure

    try:
        FindTreasure(interval=args.interval, debug=args.debug).main()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
