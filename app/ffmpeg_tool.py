# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 定位：打包成 exe 后 PATH 可能不完整，
按「同目录 → 打包内 → PATH → WinGet/常见安装位置」顺序查找。"""
import os
import sys
import glob
import shutil

_CACHE = {}

# 常见安装位置模板（支持 * 通配）
_TEMPLATES = [
    r"{exe_dir}\ffmpeg.exe",
    r"{exe_dir}\bin\ffmpeg.exe",
    r"{exe_dir}\ffmpeg\bin\ffmpeg.exe",
    r"{localappdata}\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\ffmpeg.exe",
    r"{localappdata}\Microsoft\WinGet\Links\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"D:\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]

_TEMPLATES_PROBE = [
    r"{exe_dir}\ffprobe.exe",
    r"{exe_dir}\bin\ffprobe.exe",
    r"{exe_dir}\ffmpeg\bin\ffprobe.exe",
    r"{localappdata}\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\ffprobe.exe",
    r"C:\ffmpeg\bin\ffprobe.exe",
    r"D:\ffmpeg\bin\ffprobe.exe",
    r"C:\ProgramData\chocolatey\bin\ffprobe.exe",
    r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
]


def _base_dirs():
    """exe 所在目录 + 打包临时目录 + 源码项目根"""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        mp = getattr(sys, "_MEIPASS", None)
        if mp:
            dirs.append(mp)
    else:
        dirs.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return dirs


def find(name="ffmpeg"):
    """返回 ffmpeg/ffprobe 的可用路径；找不到时返回 name 本身（调用时报明确错误）"""
    key = name.lower()
    if key in _CACHE:
        return _CACHE[key]

    exe_name = f"{name}.exe" if os.name == "nt" else name

    # 1) exe 同目录 / 打包目录
    for d in _base_dirs():
        p = os.path.join(d, exe_name)
        if os.path.isfile(p):
            _CACHE[key] = p
            return p

    # 2) PATH
    w = shutil.which(name)
    if w:
        _CACHE[key] = w
        return w

    # 3) 常见安装位置（含 WinGet 通配）
    tpls = _TEMPLATES_PROBE if key.startswith("ffprobe") else _TEMPLATES
    localappdata = os.environ.get("LOCALAPPDATA", "")
    for d in _base_dirs():
        for tpl in tpls:
            try:
                pattern = tpl.format(exe_dir=d, localappdata=localappdata)
            except (KeyError, IndexError):
                continue
            if "*" in pattern:
                hits = sorted(glob.glob(pattern))
                if hits:
                    _CACHE[key] = hits[-1]
                    return hits[-1]
            elif os.path.isfile(pattern):
                _CACHE[key] = pattern
                return pattern

    # 4) 源码模式：PATH 里可能有但 that 没找到，最后兜底
    _CACHE[key] = name
    return name


def has_ffmpeg():
    return os.path.isfile(find("ffmpeg"))


def check():
    """返回 (ffmpeg路径, ffprobe路径, 是否可用)"""
    f, p = find("ffmpeg"), find("ffprobe")
    return f, p, os.path.isfile(f) and os.path.isfile(p)


def describe():
    f, p, ok = check()
    if ok:
        return f"ffmpeg 可用: {f}"
    missing = []
    if not os.path.isfile(f):
        missing.append("ffmpeg.exe")
    if not os.path.isfile(p):
        missing.append("ffprobe.exe")
    return f"⚠️ 未找到 {('/'.join(missing))}，画面分析和视频抽音不可用（请安装 ffmpeg 或放到程序目录）"