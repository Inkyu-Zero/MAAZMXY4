# -*- coding: utf-8 -*-
import io

P = r"E:\maa\MAA造梦西游4\agent\voyage\run_zmxy4.py"
src = io.open(P, encoding="utf-8").read()

OLD = '''        status_code = voyager.auto_run()
        if status_code == 1:
            print("[完成] 挑战成功，结束任务（不回地图、不再起航）", flush=True)
            return True
        time.sleep(1)
        voyager.game_window.recover(status_code)
    return False'''

NEW = '''        status_code = voyager.auto_run()
        if status_code == 1:
            print("[完成] 挑战成功，结束任务（不回地图、不再起航）", flush=True)
            return True
        # 挑战失败不再自己 recover：recover 想点的按钮在这套「不动光标」的
        # PostMessage 模式下会被跳过，只会原地死循环。直接收工，由 pipeline
        # 的「收尾」节点去点挑战失败页上的「返回地图」。
        print("[结束] 挑战失败（status=%s），交给 pipeline 点返回地图" % status_code, flush=True)
        return False
    return False'''

assert OLD in src, "loop not found"
src = src.replace(OLD, NEW, 1)

OLD2 = "    _auto_loop_end_on_success(v, 1)\nfinally:"
NEW2 = "    _ok = _auto_loop_end_on_success(v, 1)\n    sys.exit(0 if _ok else 2)\nfinally:"
assert OLD2 in src, "tail not found"
src = src.replace(OLD2, NEW2, 1)

io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("patched")
