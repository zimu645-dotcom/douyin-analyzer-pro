# -*- coding: utf-8 -*-
"""exe 入口：默认启动服务；--worker / --login 走子命令。
PyInstaller 打包后 sys.executable 就是本 exe，子进程用参数区分模式。"""
import os
import sys

if not getattr(sys, "frozen", False):
    # 仅源码模式需要；打包模式下 _MEIPASS 已在 sys.path，强插会命中"半成品"目录
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 显式导入全部核心子模块：确保 PyInstaller 静态分析把 core 包完整打进去
# （否则打包后会出现 ImportError: cannot import name 'douyin' from 'core'）
try:
    import core.douyin          # noqa: F401
    import core.asr             # noqa: F401
    import core.llm             # noqa: F401
    import core.vision          # noqa: F401
    import core.feishu          # noqa: F401
    import core.stats           # noqa: F401
    import core.exporters       # noqa: F401
    import core.models          # noqa: F401
    import ffmpeg_tool          # noqa: F401
    import tasks                # noqa: F401
    import config               # noqa: F401
    import accounts             # noqa: F401
except Exception as _e:      # 源码调试时允许部分失败
    print(f"[entry] 预导入警告: {_e}", flush=True)


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""

    if cmd == "--worker":
        import log as applog
        applog.setup()
        import tasks
        try:
            tasks.worker_main(argv[1:])
        except SystemExit:
            raise
        except Exception as e:
            import traceback, time as _t, os as _os
            applog.err(f"任务异常终止: {type(e).__name__}: {str(e)[:180]}")
            try:
                from paths import DATA_DIR
                with open(_os.path.join(DATA_DIR, "app.log"), "a", encoding="utf-8") as f:
                    f.write(f"\n===== {_t.strftime('%Y-%m-%d %H:%M:%S')} worker 崩溃 =====\n")
                    f.write(traceback.format_exc())
                    f.write("\n")
            except Exception:
                pass
            try:
                tasks._write_status(state="error",
                                    message=f"任务异常: {type(e).__name__}: {str(e)[:80]}")
            except Exception:
                pass
            sys.exit(1)
        return

    if cmd == "--login":
        import log as applog
        applog.setup()
        import login_worker
        name = argv[1] if len(argv) > 1 else ""
        sys.argv = ["login_worker.py", name]
        login_worker.main()
        return

    # 默认：启动 Web 服务
    import main as app_main
    app_main.run()


if __name__ == "__main__":
    main()