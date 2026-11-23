# -*- coding: utf-8 -*-
"""多账号管理：accounts/<名称>/profile（浏览器登录态）+ cookies.txt。
active.json 记录当前账号。"""
import os
import json
import re

from paths import BASE
ACCOUNTS_DIR = os.path.join(BASE, "accounts")


def safe_name(name):
    name = (name or "").strip()
    return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "", name)[:32]


def _has_session(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return "sessionid" in f.read()
    except OSError:
        return False


def list_accounts():
    accs = []
    if os.path.isdir(ACCOUNTS_DIR):
        for n in sorted(os.listdir(ACCOUNTS_DIR)):
            d = os.path.join(ACCOUNTS_DIR, n)
            ck = os.path.join(d, "cookies.txt")
            if os.path.isdir(d) and n != "active.json":
                accs.append({"name": n, "logged_in": _has_session(ck),
                             "profile": os.path.join(d, "profile")})
    return accs


def active_name():
    try:
        return safe_name(json.load(open(os.path.join(ACCOUNTS_DIR, "active.json"),
                                        encoding="utf-8")).get("active"))
    except Exception:
        return ""


def set_active(name):
    name = safe_name(name)
    if name not in {a["name"] for a in list_accounts()}:
        raise ValueError("账号不存在")
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    json.dump({"active": name}, open(os.path.join(ACCOUNTS_DIR, "active.json"),
                                     "w", encoding="utf-8"), ensure_ascii=False)
    return name


def ensure(name):
    name = safe_name(name)
    if not name:
        raise ValueError("账号名无效")
    os.makedirs(os.path.join(ACCOUNTS_DIR, name, "profile"), exist_ok=True)
    return name


def profile_dir(name=None):
    name = safe_name(name) if name else active_name()
    if not name:
        name = "default"
    return os.path.join(ACCOUNTS_DIR, name, "profile")


def cookies_path(name=None):
    name = safe_name(name) if name else active_name()
    if not name:
        name = "default"
    return os.path.join(ACCOUNTS_DIR, name, "cookies.txt")


def _rmtree(path, retries=5):
    """删除目录（Chrome 刚关闭时文件可能被占用，重试几次）"""
    import shutil
    import time
    for i in range(retries):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=False)
            return True
        except Exception:
            if i == retries - 1:
                shutil.rmtree(path, ignore_errors=True)
                return not os.path.isdir(path)
            time.sleep(1.0)
    return False


def logout(name=None, remove_dir=False):
    """退出登录：清除浏览器登录态（profile + cookies），强制下次重新扫码。
    remove_dir=True 时连同账号目录一起删除。"""
    name = safe_name(name) if name else (active_name() or "default")
    pdir = os.path.join(ACCOUNTS_DIR, name, "profile")
    ck = os.path.join(ACCOUNTS_DIR, name, "cookies.txt")
    ok_profile = _rmtree(pdir)
    try:
        if os.path.exists(ck):
            os.remove(ck)
    except Exception:
        pass
    # 重建空 profile 目录，保证后续能直接登录
    try:
        os.makedirs(pdir, exist_ok=True)
    except Exception:
        pass
    if remove_dir:
        _rmtree(os.path.join(ACCOUNTS_DIR, name))
        # 当前账号被删则回落到 default
        if active_name() == name:
            try:
                os.remove(os.path.join(ACCOUNTS_DIR, "active.json"))
            except Exception:
                pass
    return {"name": name, "profile_cleared": ok_profile}
