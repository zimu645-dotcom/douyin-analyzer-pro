# -*- coding: utf-8 -*-
"""登录子进程：python login_worker.py <账号名>。每秒检测，最长 60 秒，成功立即关闭。"""
import sys
import os
import io
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

import accounts
import browser


def main():
    name = accounts.safe_name(sys.argv[1] if len(sys.argv) > 1 else "") or "default"
    force = "--force" in sys.argv
    accounts.ensure(name)
    profile = accounts.profile_dir(name)
    cookies_file = accounts.cookies_path(name)
    print(f"账号: {name}\nprofile: {profile}")

    import browser as bu
    sb = bu.shared()
    ctx, page = sb.new_page(profile, headless=False, args=["--start-maximized"])
    try:
        if force:
            # 更换登录账户：清除旧登录态，强制重新扫码
            try:
                ctx.clear_cookies()
                print("已清除旧登录态，等待重新扫码", flush=True)
            except Exception:
                pass
        print("打开抖音登录页... 请用抖音APP扫码", flush=True)
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        print("等待扫码（实时检测，最长 60 秒）...", flush=True)
        logged = False
        for i in range(60):
            time.sleep(1)
            cookies = ctx.cookies("https://www.douyin.com")
            names = [c["name"] for c in cookies]
            if "sessionid" in names and "ttwid" in names:
                logged = True
                print(f"✅ 登录成功（{i + 1}s）", flush=True)
                break
            if i % 10 == 0:
                print(f"  已等待 {i + 1}s...", flush=True)

        if logged:
            cookies = ctx.cookies("https://www.douyin.com")
            lines = ["# Netscape HTTP Cookie File"]
            for c in cookies:
                domain = c["domain"] if c["domain"].startswith(".") else "." + c["domain"]
                exp = int(c["expires"]) if c["expires"] else 0
                if exp <= 0:
                    exp = 0
                lines.append(f"{domain}\tTRUE\t{c['path']}\t{'TRUE' if c['secure'] else 'FALSE'}"
                             f"\t{exp}\t{c['name']}\t{c['value']}")
            os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
            with open(cookies_file, "w", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
            accounts.set_active(name)
            print(f"✅ cookies 已保存: {cookies_file}", flush=True)
        else:
            print("❌ 超时未检测到登录", flush=True)
    finally:
        sb.shutdown()
    print("浏览器已关闭", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            log = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "login.log")
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write("\n===== " + time.strftime("%Y-%m-%d %H:%M:%S") + " =====\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        time.sleep(8)
        raise
