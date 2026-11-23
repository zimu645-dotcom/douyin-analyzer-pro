# -*- coding: utf-8 -*-
"""统一日志：同时输出到 cmd 控制台与 data/app.log。
控制台每行带 HH:MM:SS 时间戳，阶段用分隔线和小标签区分，便于肉眼跟读。"""
import os
import sys
import time

from paths import BASE, DATA_DIR
LOG_DIR = DATA_DIR
LOG_FILE = os.path.join(LOG_DIR, "app.log")

_initialized = {"done": False, "orig": None, "fh": None}


class _Tee:
    """写 stdout 的同时落盘，并给每行加时间戳"""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh
        self._at_line_start = True

    def write(self, msg):
        if not msg:
            return 0
        out = []
        for ch in msg:
            if self._at_line_start and ch not in ("\n", "\r"):
                stamp = time.strftime("[%H:%M:%S] ")
                out.append(stamp)
                self._at_line_start = False
            out.append(ch)
            if ch == "\n":
                self._at_line_start = True
        text = "".join(out)
        try:
            if self._stream:
                self._stream.write(text)
                self._stream.flush()
        except Exception:
            pass
        try:
            if self._fh:
                self._fh.write(text)
                self._fh.flush()
        except Exception:
            pass
        return len(msg)

    def flush(self):
        try:
            if self._stream:
                self._stream.flush()
        except Exception:
            pass
        try:
            if self._fh:
                self._fh.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._stream and self._stream.isatty())
        except Exception:
            return False

    def __getattr__(self, name):
        # 转发 encoding / buffer 等属性给原始 stdout
        return getattr(self._stream, name)


def setup(force=False):
    """初始化日志（幂等）：控制台带时间戳 + data/app.log 存档"""
    if _initialized["done"] and not force:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass
    # 原 stdout 需要先取出真实底层流，避免被之前的包装套娃
    raw = getattr(sys.stdout, "_stream", sys.stdout)
    try:
        if hasattr(raw, "reconfigure"):
            raw.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fh = None
    try:
        fh = open(LOG_FILE, "a", encoding="utf-8")
    except Exception:
        fh = None
    _initialized["orig"] = raw
    _initialized["fh"] = fh
    sys.stdout = _Tee(raw, fh)
    try:
        sys.stderr = sys.stdout
    except Exception:
        pass
    _initialized["done"] = True


def banner(text, char="="):
    """打印醒目分隔线，用于区分任务/阶段"""
    line = char * 60
    print(line, flush=True)
    print(f"  {text}", flush=True)
    print(line, flush=True)


def step(text):
    """阶段小标题"""
    print(f"▶ {text}", flush=True)


def ok(text):
    print(f"✅ {text}", flush=True)


def warn(text):
    print(f"⚠️ {text}", flush=True)


def err(text):
    print(f"❌ {text}", flush=True)