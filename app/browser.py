# -*- coding: utf-8 -*-
"""无头浏览器管理：系统 Chrome 优先（抖音抓取更稳），线程内复用常驻会话。"""
import os
import time
import threading

from paths import BASE

_local = threading.local()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--mute-audio"]
VIEWPORT = {"width": 1440, "height": 900}


def _launch(p, profile, headless=True, **kwargs):
    os.makedirs(profile, exist_ok=True)
    last = None
    # Chrome 同一 user_data_dir 不能被两个进程同时打开：
    # 并行任务（如音频下载与画面分析）会短暂争用，自动等待重试。
    # 只用系统 Chrome / Edge：打包成 exe 后没有内置 Chromium，回退到无 channel 必然失败。
    for attempt in range(20):
        lock_hit = False
        for channel in ("chrome", "msedge"):
            try:
                return p.chromium.launch_persistent_context(
                    user_data_dir=profile, headless=headless, channel=channel, **kwargs)
            except Exception as e:
                last = e
                m = str(e).lower()
                if ("process singleton" in m or "already in use" in m or "lock" in m
                        or "target closed" in m or "failed to launch" in m):
                    lock_hit = True
        if lock_hit and attempt < 19:
            time.sleep(1.5)
            continue
        break
    raise last


class Shared:
    """线程内复用的浏览器：同线程多次抓取不重复启动（每次启动约 10~15 秒）。"""

    def __init__(self):
        self._pw = None
        self._ctx = None
        self._profile = None

    def new_page(self, profile, headless=True, **kwargs):
        if self._ctx is not None and self._profile == profile:
            try:
                _ = self._ctx.pages  # 探活
                return self._ctx, self._ctx.new_page()
            except Exception:
                self._ctx = None
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        self._ctx = _launch(self._pw, profile, headless=headless, **kwargs)
        self._profile = profile
        return self._ctx, self._ctx.new_page()

    def close_page(self, page):
        try:
            page.close()
        except Exception:
            pass

    def shutdown(self):
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._pw = self._profile = None


def shared():
    sb = getattr(_local, "sb", None)
    if sb is None:
        sb = Shared()
        _local.sb = sb
    return sb
