# -*- coding: utf-8 -*-
"""worker 子进程入口：python worker_main.py <mode> <args...>"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 初始化日志：控制台带时间戳 + data/app.log 存档
import log as applog
applog.setup()

import tasks


def _mark_failed(msg):
    """worker 崩溃兜底：把状态从 running 改成 error，避免界面一直转圈"""
    try:
        tasks._write_status(state="error", message=msg[:120])
    except Exception:
        pass


if __name__ == "__main__":
    try:
        tasks.worker_main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:
        # 控制台只打一行关键信息，完整堆栈写进 app.log
        applog.err(f"任务异常终止: {type(e).__name__}: {str(e)[:180]}")
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "data", "app.log"), "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} worker 崩溃 =====\n")
                f.write(traceback.format_exc())
                f.write("\n")
        except Exception:
            pass
        _mark_failed(f"任务异常: {type(e).__name__}: {str(e)[:80]}")
        sys.exit(1)