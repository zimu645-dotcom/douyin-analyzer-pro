# -*- coding: utf-8 -*-
"""抖音视频拆解分析系统 Pro — FastAPI 服务入口"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import log as applog
applog.setup()

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from pathlib import Path

import config
import accounts
import tasks
from core import douyin, feishu, exporters, asr

DATA_DIR = tasks.DATA_DIR
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

app = FastAPI(title="抖音视频拆解分析系统 Pro", version="2.0")


# ---------------- 页面 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(Path(os.path.join(WEB_DIR, "index.html")).read_text(encoding="utf-8"))


# ---------------- 任务 ----------------
class SingleJob(BaseModel):
    url: str
    modules: dict = {}


class BatchJob(BaseModel):
    url: str
    count: int = 3
    modules: dict = {}


@app.post("/api/analyze")
def api_analyze(job: SingleJob):
    if tasks.status().get("state") == "running":
        return JSONResponse({"ok": False, "error": "已有任务在运行"}, status_code=409)
    tasks.spawn(["single", job.url, json.dumps(job.modules)])
    return {"ok": True}


@app.post("/api/batch")
def api_batch(job: BatchJob):
    if tasks.status().get("state") == "running":
        return JSONResponse({"ok": False, "error": "已有任务在运行"}, status_code=409)
    tasks.spawn(["batch", job.url, str(job.count), json.dumps(job.modules)])
    return {"ok": True}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    if tasks.status().get("state") == "running":
        return JSONResponse({"ok": False, "error": "已有任务在运行"}, status_code=409)
    upload_dir = os.path.join(DATA_DIR, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    import time as _t
    # 只取文件名，防止路径穿越（../ 之类）
    safe = os.path.basename((file.filename or "audio.m4a").replace("\\", "/"))
    fname = f"up_{int(_t.time())}_{safe}"
    path = os.path.join(upload_dir, fname)
    with open(path, "wb") as f:
        f.write(await file.read())
    title = os.path.splitext(safe)[0]
    tasks.spawn(["local", path, title])
    return {"ok": True}


@app.post("/api/stop")
def api_stop():
    tasks.stop()
    return {"ok": True, "message": "已停止"}


@app.get("/api/status")
def api_status():
    return tasks.status()


@app.get("/api/live")
def api_live():
    return tasks.live()


@app.get("/api/events")
async def api_events(request: Request):
    """SSE 事件流：100ms 增量读取（按字节偏移，不重读全文），保证流式顺滑"""
    import asyncio
    events_file = os.path.join(DATA_DIR, "events.jsonl")
    last_seq = 0
    if "last-event-id" in request.headers:
        try:
            last_seq = int(request.headers["last-event-id"])
        except ValueError:
            pass

    async def gen():
        seq = last_seq
        offset = 0
        idle = 0
        buf = b""
        while True:
            if os.path.exists(events_file):
                try:
                    size = os.path.getsize(events_file)
                    if size < offset:      # 文件被重置（新任务）
                        offset = 0
                        buf = b""
                    if size > offset:
                        with open(events_file, "rb") as f:
                            f.seek(offset)
                            chunk = f.read(size - offset)
                            offset = size
                        buf += chunk
                        # 按行切分，最后一段可能不完整留到下次
                        parts = buf.split(b"\n")
                        buf = parts.pop()
                        out = []
                        for raw in parts:
                            if not raw.strip():
                                continue
                            try:
                                ev = json.loads(raw.decode("utf-8", "replace"))
                            except Exception:
                                continue
                            if ev.get("seq", 0) > seq:
                                seq = ev["seq"]
                                out.append(ev)
                        if out:
                            idle = 0
                            payload = "".join(
                                f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in out)
                            yield payload
                except Exception:
                    pass
            st = tasks.status()
            if st.get("state") in ("done", "error", "idle"):
                idle += 1
                if idle > 200:      # 约 20 秒无事件才收尾（保持连接不丢事件）
                    yield "data: {\"type\": \"stream_end\"}\n\n"
                    break
                if idle % 50 == 0:   # 每 5 秒发心跳保活
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/report/{video_id}")
def api_report(video_id: str):
    r = tasks.report(video_id)
    if not r:
        raise HTTPException(404, "报告不存在")
    return r


@app.get("/api/history")
def api_history():
    return tasks.history()


@app.delete("/api/history/{video_id}")
def api_delete_history(video_id: str):
    import json as _json
    tasks._log_history  # noqa
    hfile = os.path.join(DATA_DIR, "history.json")
    hist = tasks.history()
    hist = [h for h in hist if h.get("video_id") != video_id]
    _json.dump(hist, open(hfile, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"ok": True}


@app.post("/api/export/feishu/{video_id}")
def api_export_feishu(video_id: str):
    """手动导出到飞书多维表格"""
    rep = tasks.report(video_id)
    if not rep:
        return {"ok": False, "error": "报告不存在，请先完成拆解"}
    try:
        r = feishu.export_report(rep, config.load().get("feishu", {}))
        return {"ok": True, "message": r.get("message", "已导出到飞书")}
    except Exception as e:
        print(f"[export] 飞书导出失败: {str(e)[:200]}", flush=True)
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/export/{video_id}/{fmt}")
def api_export(video_id: str, fmt: str):
    """导出 Excel / Markdown（feishu 走上面的 POST 接口）"""
    if fmt not in ("md", "xlsx"):
        raise HTTPException(400, f"不支持的格式: {fmt}（仅 md / xlsx）")
    try:
        path = exporters.export(video_id, fmt)
        return FileResponse(path, filename=os.path.basename(path))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- 配置 ----------------
@app.get("/api/config")
def api_get_config():
    return config.public()


@app.post("/api/config")
def api_save_config(req: dict):
    cfg = config.load()
    rw = req.get("rewrite") or {}
    if "rewrite" in req:
        for eng in ("primary", "fallback"):
            e = (req["rewrite"] or {}).get(eng) or {}
            for field in ("provider", "base_url", "model", "key"):
                if field in e and e[field]:
                    cfg.setdefault("rewrite", {}).setdefault(eng, {})[field] = e[field]
    if "vision" in req:
        v = req["vision"]
        for field in ("provider", "base_url", "model", "key", "fps", "enabled"):
            if field in v and v[field] not in (None, ""):
                cfg.setdefault("vision", {})[field] = v[field]
        if "fps" in v:
            try:
                cfg["vision"]["fps"] = max(0.1, min(10.0, float(v["fps"])))
            except (TypeError, ValueError):
                pass
    if "asr" in req:
        a = req["asr"]
        for field in ("base_url", "model", "key"):
            if field in a and a[field] not in (None, ""):
                cfg.setdefault("asr", {})[field] = a[field]
    if "feishu" in req:
        f = req["feishu"]
        # 粘贴整条链接自动解析
        fu = (f.get("feishu_url") or "").strip()
        if fu:
            at, tid = feishu.parse_bitable_url(fu)
            if at:
                cfg.setdefault("feishu", {})["app_token"] = at
            if tid:
                cfg["feishu"]["table_id"] = tid
        for field in ("app_id", "app_token", "table_id", "feishu_url"):
            # 空值不覆盖已有配置（避免只改模型名时把飞书配置清空）
            if field in f and (f[field] or "").strip():
                cfg.setdefault("feishu", {})[field] = f[field].strip()
        if f.get("app_secret"):
            cfg["feishu"]["app_secret"] = f["app_secret"]
        if "auto_export" in f:
            # 只有飞书配置完整时才允许开启自动导出
            fc = cfg.get("feishu", {})
            ready = all((fc.get(k) or "").strip() for k in
                        ("app_id", "app_secret", "app_token", "table_id"))
            cfg["feishu"]["auto_export"] = bool(f["auto_export"]) and ready
    if "remember_key" in req:
        cfg["remember_key"] = bool(req["remember_key"])
    config.save(cfg)
    return {"ok": True}


# ---------------- 账号 ----------------
@app.get("/api/accounts")
def api_accounts():
    return {"ok": True, "active": accounts.active_name() or "default",
            "accounts": accounts.list_accounts()}


@app.post("/api/accounts/switch")
def api_switch(req: dict):
    try:
        return {"ok": True, "active": accounts.set_active(req.get("name", ""))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/accounts/logout")
def api_logout(req: dict):
    """退出登录：清空该账号的浏览器登录态，下次必须重新扫码。
    delete_account=True 时连账号一起删除。"""
    if tasks.status().get("state") == "running":
        return {"ok": False, "error": "任务运行中，请先停止再退出登录"}
    name = accounts.safe_name(req.get("name", "")) or accounts.active_name() or "default"
    try:
        r = accounts.logout(name, remove_dir=bool(req.get("delete_account")))
        print(f"[account] 已退出登录: {name}（登录态已清除）", flush=True)
        return {"ok": True, "message": f"「{name}」已退出登录，请重新扫码", "detail": r}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


@app.post("/api/accounts/create")
def api_create_account(req: dict):
    """新建账号并立即打开扫码登录"""
    import subprocess
    name = accounts.safe_name(req.get("name", ""))
    if not name:
        return {"ok": False, "error": "账户名不能为空（仅支持中英文数字下划线横线）"}
    if name in {a["name"] for a in accounts.list_accounts()}:
        return {"ok": False, "error": f"账户「{name}」已存在"}
    accounts.ensure(name)
    try:
        accounts.set_active(name)
    except Exception:
        pass
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--login", name]
        cwd = os.path.dirname(sys.executable)
    else:
        cmd = [sys.executable, os.path.join(app_dir, "login_worker.py"), name]
        cwd = app_dir
    try:
        subprocess.Popen(cmd, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE
                         if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0)
        return {"ok": True, "message": f"已新建账户「{name}」，请扫码登录"}
    except Exception as e:
        return {"ok": False, "error": f"启动登录失败: {e}"}


@app.post("/api/accounts/login")
def api_login(req: dict):
    """打开扫码登录窗口。force=True 时先清空旧登录态，确保必然重新扫码。"""
    import subprocess
    name = accounts.safe_name(req.get("name", "")) or accounts.active_name() or "default"
    accounts.ensure(name)
    if req.get("force"):
        try:
            accounts.logout(name)
            print(f"[account] 已清除 {name} 的旧登录态，等待重新扫码", flush=True)
        except Exception as e:
            print(f"[account] 清除旧登录态失败: {str(e)[:80]}", flush=True)
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--login", name]
        cwd = os.path.dirname(sys.executable)
    else:
        cmd = [sys.executable, os.path.join(app_dir, "login_worker.py"), name]
        cwd = app_dir
    try:
        subprocess.Popen(cmd, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE
                         if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0)
        return {"ok": True, "message": f"已打开 {name} 的登录窗口，请扫码（最长 60 秒）"}
    except Exception as e:
        return {"ok": False, "error": f"登录启动失败: {e}"}


# ---------------- 飞书测试 ----------------
@app.get("/api/feishu/test")
def api_feishu_test():
    return feishu.test_connection(config.load().get("feishu", {}))


# ---------------- 模型列表（读官方 /models） ----------------
@app.get("/api/models")
def api_models(provider: str = "rewrite_primary"):
    """动态读取服务商官方模型列表，保证名称与官方一致。
    provider: rewrite_primary / rewrite_fallback / vision / custom
    url+key: custom 时通过 query 传入"""
    from core import models as models_mod
    cfg = config.load()
    try:
        if provider == "rewrite_primary":
            e = (cfg.get("rewrite") or {}).get("primary") or {}
        elif provider == "rewrite_fallback":
            e = (cfg.get("rewrite") or {}).get("fallback") or {}
        elif provider == "vision":
            e = cfg.get("vision") or {}
        else:
            return {"ok": False, "error": "未知 provider"}
        names = models_mod.list_models(e.get("base_url"), e.get("key"), chat_only=True)
        return {"ok": True, "provider": provider, "models": names,
                "current": e.get("model") or ""}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------- 启动 ----------------
def run():
    import uvicorn
    # 首次运行：确保存在默认账户目录，前端下拉框不至于为空
    try:
        if not accounts.list_accounts():
            accounts.ensure("default")
    except Exception:
        pass
    cfg = config.load()
    rw = (cfg.get("rewrite") or {}).get("primary") or {}
    vis = cfg.get("vision") or {}
    asr_c = cfg.get("asr") or {}
    fs = cfg.get("feishu") or {}
    applog.banner("抖音视频拆解分析系统 Pro")
    print("  地址     : http://127.0.0.1:8321", flush=True)
    print(f"  改写引擎 : {rw.get('model') or '未配置'}  "
          f"Key={'已配置' if (rw.get('key') or '').strip() else '未配置'}", flush=True)
    print(f"  语音转写 : {asr_c.get('model') or '未配置'}  "
          f"Key={'已配置' if (asr_c.get('key') or '').strip() else '未配置'}", flush=True)
    print(f"  画面分析 : {vis.get('model') or '未配置'}  fps={vis.get('fps')}  "
          f"Key={'已配置' if (vis.get('key') or '').strip() else '未配置'}", flush=True)
    print(f"  飞书导出 : {'已配置' if (fs.get('app_token') and fs.get('table_id')) else '未配置'}  "
          f"自动导出={'开' if fs.get('auto_export') else '关'}", flush=True)
    try:
        import ffmpeg_tool
        print(f"  {ffmpeg_tool.describe()}", flush=True)
    except Exception:
        pass
    print(f"  日志文件 : {os.path.join(DATA_DIR, 'app.log')}", flush=True)
    applog.banner("服务启动中，等待浏览器访问…", "-")
    uvicorn.run(app, host="127.0.0.1", port=8321, log_level="warning")


if __name__ == "__main__":
    run()
