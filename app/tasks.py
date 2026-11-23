# -*- coding: utf-8 -*-
"""任务运行器：单视频 / 批量 / 本地上传。事件写 data/events.jsonl，状态写 data/status.json。"""
import os
import sys
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import config
import accounts
from core import douyin, asr, llm, vision, stats, feishu, exporters

from paths import BASE, DATA_DIR, ensure_dirs

AUDIO_DIR = os.path.join(DATA_DIR, "audio")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")
LIVE_FILE = os.path.join(DATA_DIR, "live.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

ensure_dirs()

_LOCK = threading.Lock()
_WORKER = None
_EVENT_SEQ = [0]
_live = {}
_last_error = []

# 事件文件常驻句柄 + 写入节流（token 级别高频写入时避免反复开关文件）
_ev_fh = None
_ev_pending = []
_ev_last_flush = [0.0]
_ev_lock = threading.Lock()


def _flush_events(force=False):
    """把缓冲的 token 事件批量写入并 flush（默认 150ms 一批，保证流畅且不过度 IO）"""
    now = time.time()
    if not force and (now - _ev_last_flush[0]) < 0.15:
        return
    global _ev_fh
    with _ev_lock:
        if not _ev_pending:
            return
        batch = _ev_pending[:]
        _ev_pending.clear()
        _ev_last_flush[0] = now
        try:
            if _ev_fh is None:
                _ev_fh = open(EVENTS_FILE, "a", encoding="utf-8")
            _ev_fh.write("".join(batch))
            _ev_fh.flush()
        except Exception:
            pass


def _write_event(ev):
    _EVENT_SEQ[0] += 1
    ev["seq"] = _EVENT_SEQ[0]
    ev["ts"] = time.time()
    line = json.dumps(ev, ensure_ascii=False) + "\n"
    with _ev_lock:
        _ev_pending.append(line)
    # token 类事件批量刷；状态类事件立即刷
    if ev.get("type") in ("analysis_token", "vision_token", "polish_batch",
                          "transcribe_segment"):
        _flush_events()
    else:
        _flush_events(force=True)


_live_last_write = [0.0]


def _write_live(force=False):
    """写 live.json：节流到最多每 400ms 一次，避免 token 级高频全量序列化"""
    now = time.time()
    if not force and (now - _live_last_write[0]) < 0.4:
        return
    _live_last_write[0] = now
    with _LOCK:
        try:
            _live["updated"] = time.time()
            with open(LIVE_FILE, "w", encoding="utf-8") as f:
                json.dump(_live, f, ensure_ascii=False, indent=1)
        except Exception:
            pass


def _write_status(state=None, message=None, video_id=None, **fields):
    with _LOCK:
        try:
            s = json.load(open(STATUS_FILE, encoding="utf-8")) if os.path.exists(STATUS_FILE) else {}
        except Exception:
            s = {}
        if state:
            s["state"] = state
        if message:
            s["message"] = message
        if video_id:
            s.setdefault("videos", {}).setdefault(video_id, {}).update(fields)
        s["updated"] = time.time()
        if message or fields.get("stage") or fields.get("percent") is not None:
            try:
                _write_event({"type": "status_update", "message": message or "",
                              "video_id": video_id,
                              **{k: v for k, v in fields.items()
                                 if k in ("stage", "percent", "status", "progress", "title")}})
            except Exception:
                pass
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)


def status():
    _merge_worker_status()
    with _LOCK:
        st = json.loads(json.dumps(_STATUS, ensure_ascii=False))
    # 兜底：worker 进程已退出但状态还挂在 running（崩溃没写状态）→ 标记失败，避免界面一直转圈
    if st.get("state") == "running" and _WORKER is not None:
        try:
            if _WORKER.poll() is not None:
                msg = "任务进程意外退出（详见 data/app.log）"
                st["state"] = "error"
                st["message"] = msg
                with _LOCK:
                    _STATUS.update({"state": "error", "message": msg})
        except Exception:
            pass
    return st


_STATUS = {"state": "idle", "message": "", "videos": {}}


def _merge_worker_status():
    global _STATUS
    try:
        if os.path.exists(STATUS_FILE):
            ws = json.load(open(STATUS_FILE, encoding="utf-8"))
            with _LOCK:
                _STATUS.update({k: v for k, v in ws.items()
                                if k in ("state", "message", "videos", "modules")})
    except Exception:
        pass


def live():
    """前端实时数据：worker 进程写 data/live.json，这里直接读盘"""
    try:
        if os.path.exists(LIVE_FILE):
            return json.load(open(LIVE_FILE, encoding="utf-8"))
    except Exception:
        pass
    return {"video_id": None, "step": "idle"}


def spawn(args):
    """启动 worker 子进程（输出写入 data/worker.log 便于排查）"""
    global _WORKER
    _reset_task_files()
    with _LOCK:
        _STATUS.update({"state": "running", "message": "启动中...", "videos": {}})
        # 记录本次任务的模块勾选，供前端刷新后恢复显示
        try:
            mods = json.loads(args[-1]) if args else {}
            if isinstance(mods, dict):
                _STATUS["modules"] = mods
        except Exception:
            pass
        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(_STATUS, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    import sys
    import subprocess
    if getattr(sys, "frozen", False):
        # 打包为 exe：自身以 --worker 模式启动
        cmd = [sys.executable, "--worker"] + args
        cwd = os.path.dirname(sys.executable)
    else:
        worker_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_main.py")
        cmd = [sys.executable, worker_py] + args
        cwd = os.path.dirname(worker_py)
    # 输出继承父进程控制台：worker 日志直接显示在 cmd 窗口（同时由 log 模块写入 data/app.log）
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=None, stderr=None)
    except Exception as e:
        with _LOCK:
            _STATUS.update({"state": "error", "message": f"worker 启动失败: {e}"})
        raise
    _WORKER = proc
    return True


def _reset_task_files():
    for f in (STATUS_FILE, EVENTS_FILE, LIVE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass


def stop():
    global _WORKER
    proc = _WORKER
    _WORKER = None
    if proc and proc.poll() is None:
        import subprocess
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10)
    _reset_task_files()
    with _LOCK:
        _STATUS.update({"state": "idle", "message": "任务已停止", "videos": {}})


def _log_history(report):
    try:
        hist = []
        if os.path.exists(HISTORY_FILE):
            hist = json.load(open(HISTORY_FILE, encoding="utf-8"))
        st = report.get("stats") or {}
        hist = [h for h in hist if h.get("video_id") != report["video_id"]]
        hist.insert(0, {"video_id": report["video_id"], "title": report.get("title", ""),
                        "url": report.get("url", ""), "time": time.strftime("%Y-%m-%d %H:%M"),
                        "duration": round(report.get("duration", 0)),
                        "like": st.get("like", "-"), "comment": st.get("comment", "-"),
                        "collect": st.get("collect", "-"), "share": st.get("share", "-"),
                        "author": st.get("nickname") or st.get("channel_name", ""),
                        "fans": st.get("fans", "-")})
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def history():
    try:
        return json.load(open(HISTORY_FILE, encoding="utf-8"))
    except Exception:
        return []


def report(video_id):
    f = os.path.join(REPORT_DIR, f"{video_id}.json")
    if os.path.exists(f):
        return json.load(open(f, encoding="utf-8"))
    return None


def process_single(vid, source_url, modules, emit, write_status, live_update):
    """完整单视频流水线（在 worker 进程中运行）"""
    import config as cfg_mod
    import log as applog
    conf = cfg_mod.load()
    use_analysis = modules.get("analysis", True)
    use_rewrite = modules.get("rewrite", True)
    use_vision = modules.get("vision", False)
    vision_conf = conf.get("vision", {})
    use_vision = use_vision and (vision_conf.get("key") or "").strip() and vision_conf.get("enabled", True)
    _t0 = time.time()
    times = {}

    write_status(state="running", message="下载中...", video_id=vid,
                 status="downloading", progress=5, stage="downloading")
    live_update(video_id=vid, url=source_url, step="downloading", segments=[],
                rewritten=[], vision=[], stats={})

    applog.banner(f"开始处理  {vid}")
    print(f"  链接   : {source_url}", flush=True)
    print(f"  模块   : 改写={'开' if use_rewrite else '关'} "
          f"分析={'开' if use_analysis else '关'} "
          f"画面={'开' if use_vision else '关'}", flush=True)

    # 一次抓取 → 音频/视频并行下载（勾选画面分析时同时下视频，不再二次开浏览器）
    applog.step("阶段 1/5  下载媒体")
    write_status(video_id=vid, stage="downloading", message="阶段 1/5 下载媒体")
    _td = time.time()
    def _dl_progress(msg):
        write_status(video_id=vid, message=msg)
    prep = douyin.prepare_media(vid, want_video=use_vision, on_progress=_dl_progress)
    if not prep.get("ok"):
        err = prep.get("error") or "下载失败"
        applog.err(f"下载失败: {err}")
        write_status(state="running", video_id=vid, status="failed",
                     progress="下载失败", percent=0)
        emit({"type": "video_failed", "video_id": vid, "reason": err[:80]})
        live_update(step="failed")
        return False
    times["download"] = round(time.time() - _td, 1)

    api_title = prep.get("title") or vid
    video_bytes = prep.get("video_bytes")
    meta_stats = prep.get("stats") or {}
    _asz = os.path.getsize(prep["audio_path"]) if prep.get("audio_path") and os.path.exists(prep["audio_path"]) else 0
    applog.ok(f"下载完成 {times['download']}s  音频={_asz // 1024}KB"
              + (f"  视频={len(video_bytes) // 1024 // 1024}MB" if video_bytes else ""))
    print(f"  标题   : {api_title[:50]}", flush=True)

    write_status(video_id=vid, status="transcribing", title=api_title,
                 progress="转写中", percent=20, stage="transcribing", message=f"转写中: {api_title[:16]}")
    emit({"type": "video_start", "video_id": vid, "title": api_title,
          "url": source_url, "canonical_url": f"https://www.douyin.com/video/{vid}"})

    # ===== 音频就绪立即分两路并行 =====
    # 路线B：画面分析（用已并行下载好的 video_bytes，直接压缩+托管+MiMo）
    vision_pool = None
    vision_future = None
    if use_vision:
        def _vision_early():
            def emit_v(ev):
                ev["video_id"] = vid
                emit(ev)
            try:
                return vision.analyze_video(vid, 0, vision_conf, conf.get("feishu", {}),
                                            emit=emit_v, video_bytes=video_bytes)
            except Exception as e:
                print(f"[task] 画面分析失败: {str(e)[:120]}", flush=True)
                return []
        vision_pool = ThreadPoolExecutor(max_workers=1)
        vision_future = vision_pool.submit(_vision_early)
        applog.step("阶段 5/5  画面分析（与转写并行启动）")
        write_status(video_id=vid, stage="vision", message="画面分析启动（与转写并行）")

    def on_seg(seg):
        _live.setdefault("segments", []).append(seg)
        _write_live()
        emit({"type": "transcribe_segment", "video_id": vid, "segment": seg})

    applog.step("阶段 2/5  云端语音转写")
    write_status(video_id=vid, stage="transcribing", message="阶段 2/5 云端语音转写")
    t = time.time()
    try:
        data = asr.transcribe(os.path.join(AUDIO_DIR, f"{vid}.m4a"), vid, api_title,
                              source_url, conf.get("asr", {}), on_segment=on_seg)
    except Exception as e:
        applog.err(f"转写失败: {str(e)[:150]}")
        write_status(video_id=vid, status="failed", title=api_title,
                     progress="转写失败", percent=0)
        emit({"type": "video_failed", "video_id": vid, "title": api_title,
              "reason": f"转写失败: {str(e)[:80]}"})
        live_update(step="failed")
        return False
    times["transcribe"] = round(time.time() - t, 1)
    applog.ok(f"转写完成 {times['transcribe']}s  {len(data['segments'])} 段  "
              f"音频 {round(data.get('duration') or 0)}s")
    live_update(title=api_title, duration=data["duration"], full_text=data["full_text"],
                segments=data["segments"], step="rewriting")
    emit({"type": "transcribe_done", "video_id": vid, "title": api_title,
          "segments": data["segments"], "full_text": data["full_text"],
          "duration": data["duration"], "transcribe_time": times["transcribe"],
          "segment_count": len(data["segments"])})
    write_status(video_id=vid, status="rewriting", progress="AI 改写分析中",
                 percent=50, duration=data["duration"], stage="rewriting")

    if not (data.get("full_text") or "").strip():
        write_status(state="running", video_id=vid, status="skipped",
                     progress="无语音(跳过分析)", percent=100)
        emit({"type": "video_skipped", "video_id": vid, "reason": "no_audio"})
        report = {"video_id": vid, "title": api_title, "url": source_url,
                  "duration": data.get("duration", 0), "language": data.get("language", ""),
                  "skipped": True, "original_segments": data.get("segments", [])}
        os.makedirs(REPORT_DIR, exist_ok=True)
        json.dump(report, open(os.path.join(REPORT_DIR, f"{vid}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        _log_history(report)
        return True

    rewrite_conf = conf.get("rewrite", {})
    if not any(e.get("key") for e in llm._engines(rewrite_conf)):
        emit({"type": "video_failed", "video_id": vid,
              "reason": "未配置改写引擎 API Key"})
        write_status(video_id=vid, status="failed", progress="未配置改写引擎", percent=0)
        return False

    def _polish():
        def on_batch(batch):
            for seg in batch:
                _live.setdefault("rewritten_segments", []).append(seg)
            _write_live()
            emit({"type": "polish_batch", "video_id": vid, "segments": batch})
        return llm.polish_segments(data["segments"], rewrite_conf, on_batch=on_batch)

    def _analyze():
        def on_tok(tok):
            _live["analysis"] = {"text": (_live.get("analysis", {}) or {}).get("text", "") + tok}
            _write_live()
            emit({"type": "analysis_token", "video_id": vid, "tok": tok})
        return llm.analyze(data["full_text"], rewrite_conf, stream=use_analysis, on_token=on_tok)

    applog.step("阶段 3/5  润色改写  +  阶段 4/5  AI 分析（并行）")
    write_status(video_id=vid, stage="rewriting", message="润色改写 + AI 分析（并行）")
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_polish = ex.submit(_polish) if use_rewrite else None
        f_analyze = ex.submit(_analyze) if use_analysis else None
        _tp = time.time()
        rw = f_polish.result() if f_polish else []
        times["polish"] = round(time.time() - _tp, 1) if use_rewrite else 0
        if use_rewrite:
            applog.ok(f"润色完成 {times['polish']}s  {len(rw)} 段")
        if use_analysis:
            write_status(video_id=vid, status="rewriting", progress="AI 分析中",
                         percent=75, stage="analysis_gen")
        _ta = time.time()
        analysis = f_analyze.result() if f_analyze else {}
        times["analysis"] = round(time.time() - _ta, 1) if use_analysis else 0
        if use_analysis:
            hl = len(analysis.get("highlights") or [])
            im = len(analysis.get("improvements") or [])
            if analysis.get("summary") or hl:
                applog.ok(f"AI 分析完成 {times['analysis']}s  亮点 {hl} 条  改进 {im} 条")
            else:
                applog.warn(f"AI 分析未返回有效内容"
                            + (f": {analysis.get('error', '')[:80]}" if analysis.get("error") else ""))
        # 画面分析是提前启动的独立分支，这里只等待它完成
        if vision_future:
            write_status(video_id=vid, status="rewriting", progress="等待画面分析完成",
                         percent=85, stage="vision_gen")
            _tv = time.time()
            try:
                vision_data = vision_future.result(timeout=1800)
            except Exception as e:
                applog.warn(f"画面分析等待失败: {str(e)[:100]}")
                vision_data = []
            times["vision"] = round(time.time() - _tv, 1)
            if vision_data:
                bad_hints = ("拦截", "失败", "无法", "跳过", "未找到")
                okc = sum(1 for x in vision_data
                          if x.get("analysis") and not any(h in str(x.get("analysis")) for h in bad_hints))
                if okc:
                    applog.ok(f"画面分析完成 {times['vision']}s  {len(vision_data)} 段（有效 {okc}）")
                else:
                    reason = str((vision_data[0] or {}).get("analysis") or "")[:80]
                    applog.warn(f"画面分析未成功（{reason}）")
            else:
                applog.warn("画面分析未返回内容")
        else:
            vision_data = []
        live_update(vision=vision_data, step="finalizing")
    if vision_pool:
        vision_pool.shutdown(wait=False)

    stats_data = {"like": "-", "comment": "-", "collect": "-", "share": "-",
                  "play": "-", "channel_name": "", "channel_id": "",
                  "fans": "-", "following": "-", "total_likes": "-", "works": "-",
                  "nickname": ""}
    emit({"type": "stats_done", "video_id": vid, "stats": stats_data, "pending": True})

    emit({"type": "polish_done", "video_id": vid, "time": times.get("polish", 0)})
    emit({"type": "analysis_done", "video_id": vid, "analysis": analysis,
          "time": times.get("analysis", 0)})

    source_url = source_url
    canonical = f"https://www.douyin.com/video/{vid}"
    report = {"video_id": vid, "title": api_title, "url": source_url,
              "source_url": source_url, "canonical_url": canonical,
              "duration": data["duration"], "language": data.get("language", ""),
              "original_segments": data["segments"], "full_text": data["full_text"],
              "rewritten_segments": rw, "analysis": analysis,
              "stats": stats_data, "vision": vision_data}
    json.dump(report, open(os.path.join(REPORT_DIR, f"{vid}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    _log_history(report)
    live_update(step="done")
    total = round(time.time() - _t0, 1)
    applog.banner(f"处理完成  {vid}")
    print(f"  标题     : {api_title[:50]}", flush=True)
    print(f"  下载     : {times.get('download', 0)}s", flush=True)
    print(f"  转写     : {times.get('transcribe', 0)}s   {len(data['segments'])} 段", flush=True)
    print(f"  润色     : {times.get('polish', 0)}s   {len(rw)} 段", flush=True)
    print(f"  分析     : {times.get('analysis', 0)}s", flush=True)
    if use_vision:
        print(f"  画面分析 : {times.get('vision', 0)}s   {len(vision_data)} 段", flush=True)
    print(f"  总耗时   : {total}s", flush=True)
    print(f"  报告     : {os.path.join(REPORT_DIR, vid + '.json')}", flush=True)
    emit({"type": "video_done", "video_id": vid, "title": api_title,
          "url": source_url, "total_time": total,
          "timing": {"transcribe": times.get("transcribe", 0),
                     "analysis": times.get("analysis", 0),
                     "stats": times.get("stats", 0), "total": total}})
    write_status(video_id=vid, status="done", progress="完成", percent=100, stage="video_done")

    # 异步：互动数据回填 + 自动导出飞书
    def _fill():
        meta_file = os.path.join(DATA_DIR, "meta", f"{vid}.json")
        real = None
        # 优先用下载时抓到的 detail 数据（稳定，避免风控）
        try:
            m = json.load(open(meta_file, encoding="utf-8"))
            st = m.get("stats") or {}
            au = m.get("author") or {}
            if st.get("like") not in (None, "-"):
                real = {"like": st.get("like", "-"), "comment": st.get("comment", "-"),
                        "collect": st.get("collect", "-"), "share": st.get("share", "-"),
                        "play": st.get("play", "-"),
                        "channel_name": au.get("name", ""), "channel_id": au.get("id", ""),
                        "nickname": au.get("name", ""), "fans": au.get("fans", "-")}
        except Exception:
            pass
        # 兜底：实时抓取（可能被风控）
        if real is None:
            try:
                v = stats.one(vid) or {}
                if v and not v.get("error"):
                    real = {"like": v.get("like", "-"), "comment": v.get("comment", "-"),
                            "collect": v.get("collect", "-"), "share": v.get("share", "-"),
                            "play": v.get("play", "-"), "channel_name": v.get("author_name", ""),
                            "channel_id": v.get("author_id", "")}
                    if v.get("author_id"):
                        a = stats.fetch([], v["author_id"]).get("author") or {}
                        real.update({"fans": a.get("fans", "-"), "following": a.get("following", "-"),
                                     "total_likes": a.get("total_likes", "-"), "works": a.get("works", "-"),
                                     "nickname": a.get("nickname", "")})
                else:
                    print(f"[task] 互动数据被风控拦截: {str(v)[:80]}", flush=True)
            except Exception as e:
                print(f"[task] 互动数据抓取失败: {str(e)[:80]}", flush=True)
        if real:
            _live["stats"] = real
            _write_live()
            emit({"type": "stats_done", "video_id": vid, "stats": real, "pending": False})
            rf = os.path.join(REPORT_DIR, f"{vid}.json")
            if os.path.exists(rf):
                rep = json.load(open(rf, encoding="utf-8"))
                rep["stats"] = real
                json.dump(rep, open(rf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        if conf.get("feishu", {}).get("auto_export"):
            try:
                feishu.export_report(json.load(
                    open(os.path.join(REPORT_DIR, f"{vid}.json"), encoding="utf-8")),
                    conf.get("feishu", {}))
                print("[task] 飞书自动导出成功", flush=True)
            except Exception as e:
                print(f"[task] 飞书自动导出失败: {str(e)[:120]}", flush=True)
                emit({"type": "export_failed", "video_id": vid, "reason": str(e)[:120]})

    # 互动数据回填 + 自动导出飞书（在 worker 退出前完成，避免 daemon 线程被杀）
    _th = threading.Thread(target=_fill, daemon=False)
    _th.start()
    _th.join(timeout=120)
    return True


def _emit_batch_list(videos, emit, phase="list"):
    """批量清单一次性下发（避免重复推送导致界面出现两遍）"""
    total = len(videos)
    for i, v in enumerate(videos):
        emit({"type": "batch_progress", "video_id": v["video_id"], "index": i + 1,
              "total": total, "title": v["title"], "phase": phase})


def _emit_batch_item(vid, index, total, title, emit, status):
    """单个视频状态：processing / done / failed / cached"""
    ev = {"type": "batch_progress", "video_id": vid, "index": index, "total": total,
          "title": title, "status": status}
    if status == "failed":
        ev["failed"] = True
    if status == "cached":
        ev["cached"] = True
    emit(ev)


def worker_main(argv):
    """worker 子进程入口（日志由 applog.setup() 统一处理，此处不再包装 stdout）"""
    mode = argv[0]

    def emit(ev):
        _write_event(ev)

    def wstatus(state=None, message=None, video_id=None, **fields):
        _write_status(state, message, video_id, **fields)

    def lupdate(**kw):
        _live.update(kw)
        _write_live()

    if mode == "single":
        url = argv[1]
        modules = json.loads(argv[2]) if len(argv) > 2 else {}
        vid = douyin.extract_video_id(url)
        if not vid:
            emit({"type": "job_error", "message": f"链接中未找到视频ID: {url[:60]}"})
            _write_status(state="error", message=f"链接无效: {url[:50]}")
            return
        ok = process_single(vid, url, modules, emit, wstatus, lupdate)
        _write_status(state="done" if ok else "error",
                      message=f"视频处理{'完成' if ok else '失败'}")
    elif mode == "batch":
        url = argv[1]
        count = int(argv[2]) if len(argv) > 2 else 3
        modules = json.loads(argv[3]) if len(argv) > 3 else {}
        conf = config.load()
        _write_status(state="running", message="抓取主页视频清单...")
        try:
            videos = douyin.fetch_author_videos(url, count * 3)
        except Exception as e:
            _write_status(state="error", message=f"主页抓取失败: {str(e)[:80]}")
            return
        if not videos:
            _write_status(state="error", message="主页未抓取到视频")
            return
        # 用互动数据验证作者，过滤推荐流的他人视频
        import re
        m = re.search(r"/user/([^/?]+)", url)
        target = m.group(1) if m else None
        if target:
            st = stats.fetch([v["video_id"] for v in videos])
            keep = [v for v in videos if st.get("videos", {}).get(v["video_id"], {}).get("author_id") == target]
            if keep:
                videos = keep
        videos = videos[:count]
        total = len(videos)
        if not videos:
            _write_status(state="error", message="未验证到该博主的作品")
            return
        _write_status(state="running", message=f"确认 {total} 个作品，开始批量处理...")
        _emit_batch_list(videos, emit, "list")
        # 批量预下载：单浏览器串行抓流（避免 profile 争抢）+ 并行下载媒体
        want_v = bool(modules.get("vision"))
        download_ok = {}

        def _on_dl_progress(phase, i, n, vid):
            if phase == "capture":
                _write_status(message=f"抓取流地址 {i}/{n}...")

        prep_map = douyin.prepare_media_batch([v["video_id"] for v in videos],
                                              want_video=want_v, on_progress=_on_dl_progress)
        for v in videos:
            r = prep_map.get(v["video_id"]) or {}
            download_ok[v["video_id"]] = bool(r.get("ok"))
        n_dl = sum(1 for x in download_ok.values() if x)
        print(f"[batch] 媒体准备完成：成功 {n_dl}/{len(videos)}", flush=True)

        # ===== 并行预转写：所有下载好的音频一次性转完，避免逐个等待 =====
        todo = [v for v in videos
                if download_ok.get(v["video_id"])
                and not os.path.exists(os.path.join(REPORT_DIR, f"{v['video_id']}.json"))]
        if todo:
            _write_status(message=f"并行转写 {len(todo)} 条音频...")
            print(f"[batch] 开始并行预转写 {len(todo)} 条（并发 3）", flush=True)
            audio_map = {v["video_id"]: os.path.join(AUDIO_DIR, f"{v['video_id']}.m4a") for v in todo}

            def _on_asr(done, n, vid, ok):
                _write_status(message=f"并行转写 {done}/{n} {'✓' if ok else '✗'}")

            asr.pre_transcribe_batch(audio_map, conf.get("asr", {}), workers=3,
                                     on_progress=_on_asr)
            print("[batch] 预转写完成，开始逐个润色分析", flush=True)

        for i, v in enumerate(videos):
            vid = v["video_id"]
            if os.path.exists(os.path.join(REPORT_DIR, f"{vid}.json")):
                _write_status(video_id=vid, status="done", title=v["title"], progress="已缓存(跳过)")
                _emit_batch_item(vid, i + 1, total, v["title"], emit, "cached")
                continue
            if not download_ok.get(vid):
                _write_status(video_id=vid, status="failed", title=v["title"],
                              progress="下载失败(跳过)")
                _emit_batch_item(vid, i + 1, total, v["title"], emit, "failed")
                continue
            _emit_batch_item(vid, i + 1, total, v["title"], emit, "processing")
            try:
                ok = process_single(vid, v["url"], modules, emit, wstatus, lupdate)
            except Exception as e:
                print(f"[task] 单视频异常: {str(e)[:120]}", flush=True)
                _write_status(video_id=vid, status="failed", title=v["title"],
                              progress="处理异常", percent=0)
                _emit_batch_item(vid, i + 1, total, v["title"], emit, "failed")
                ok = False
            _emit_batch_item(vid, i + 1, total, v["title"], emit, "done" if ok else "failed")
            _write_status(message=f"[{i+1}/{total}] {v['title'][:20]}... {'完成' if ok else '失败'}")
        _write_status(state="done", message=f"批量完成：{total} 个视频")
        print("DONE batch", flush=True)
    elif mode == "local":
        path, title = argv[1], argv[2]
        conf = config.load()
        vid = f"local_{int(time.time())}"
        emit({"type": "video_start", "video_id": vid, "title": title, "url": "(本地文件)"})
        try:
            data = asr.transcribe(path, vid, title, "(本地文件)", conf.get("asr", {}))
        except Exception as e:
            _write_status(state="error", message=f"转写失败: {str(e)[:80]}")
            emit({"type": "video_failed", "video_id": vid, "reason": str(e)[:80]})
            return
        emit({"type": "transcribe_done", "video_id": vid, "title": title,
              "segments": data["segments"], "full_text": data["full_text"],
              "duration": data["duration"], "segment_count": len(data["segments"])})
        rewrite_conf = conf.get("rewrite", {})
        rw = llm.polish_segments(data["segments"], rewrite_conf)
        analysis = llm.analyze(data["full_text"], rewrite_conf)
        report = {"video_id": vid, "title": title, "url": "(本地文件)",
                  "duration": data["duration"], "language": data.get("language", ""),
                  "original_segments": data["segments"], "full_text": data["full_text"],
                  "rewritten_segments": rw, "analysis": analysis}
        json.dump(report, open(os.path.join(REPORT_DIR, f"{vid}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        _log_history(report)
        _write_status(state="done", message="本地音频处理完成")
        emit({"type": "video_done", "video_id": vid, "title": title})
        print("DONE local", flush=True)

