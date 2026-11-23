# -*- coding: utf-8 -*-
"""统一路径：源码模式与 PyInstaller 打包(exe)模式都指向正确位置。
- BASE    : 程序数据根目录（exe 旁边 / 源码为项目根）
- RES_DIR : 只读资源目录（打包后是临时解包目录，源码为 app 目录）
打包后 data/accounts 等运行时数据写在 exe 同目录，便于备份和迁移。"""
import os
import sys


def _frozen():
    return bool(getattr(sys, "frozen", False))


def base_dir():
    if _frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def res_dir():
    """只读资源（web/index.html 等）"""
    if _frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


BASE = base_dir()
RES = res_dir()
DATA_DIR = os.path.join(BASE, "data")
ACCOUNTS_DIR = os.path.join(BASE, "accounts")
WEB_DIR = os.path.join(RES, "web")


def ensure_dirs():
    for d in (DATA_DIR, ACCOUNTS_DIR,
              os.path.join(DATA_DIR, "audio"), os.path.join(DATA_DIR, "video"),
              os.path.join(DATA_DIR, "tmp"), os.path.join(DATA_DIR, "meta"),
              os.path.join(DATA_DIR, "reports"), os.path.join(DATA_DIR, "transcripts"),
              os.path.join(DATA_DIR, "rewritten"), os.path.join(DATA_DIR, "exports"),
              os.path.join(DATA_DIR, "uploads")):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass