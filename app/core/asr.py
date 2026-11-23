# -*- coding: utf-8 -*-
"""云端语音转写（硅基流动 SenseVoice）：60s 超时 + 指数退避重试 5 次。"""
import os
import re
import json
import time
import uuid
import subprocess
import urllib.request
import urllib.error

from paths import BASE, DATA_DIR
TRANSCRIPT_DIR = os.path.join(DATA_DIR, "transcripts")

RETRYABLE = (429, 500, 502, 503, 504)


def probe_duration(path):
    """读取媒体时长（秒）。优先 ffprobe，缺失时用 ffmpeg 回退
    （打包只带 ffmpeg，避免 exe 体积翻倍）。"""
    import ffmpeg_tool
    # 1) ffprobe 可用则直接用
    probe = ffmpeg_tool.find("ffprobe")
    if os.path.isfile(probe):
        try:
            r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", path],
                               capture_output=True, text=True, timeout=60)
            v = float((r.stdout or "").strip() or 0)
            if v > 0:
                return v
        except Exception:
            pass
    # 2) 回退：ffmpeg -i 的 stderr 里有 Duration: HH:MM:SS.ss
    try:
        ff = ffmpeg_tool.find("ffmpeg")
        r = subprocess.run([ff, "-hide_banner", "-i", path],
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


def cache_path(video_id):
    return os.path.join(TRANSCRIPT_DIR, f"{video_id}.json")


def load_cached(video_id):
    """读取转写缓存；无效或不存在返回 None"""
    try:
        p = cache_path(video_id)
        if not os.path.exists(p):
            return None
        d = json.load(open(p, encoding="utf-8"))
        if d.get("segments") and d.get("full_text"):
            return d
    except Exception:
        pass
    return None


def pre_transcribe_batch(audio_map, conf, workers=3, on_progress=None):
    """批量并行预转写：{video_id: audio_path} → {video_id: data 或 None}
    先把所有音频一次性转完，后续处理直接读缓存，不用一个个等。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    todo = [(vid, p) for vid, p in audio_map.items() if p and os.path.exists(p)]
    if not todo:
        return results

    def _one(vid, path):
        cached = load_cached(vid)
        if cached:
            return vid, cached, True
        try:
            data = transcribe(path, vid, vid, "", conf, use_cache=True)
            return vid, data, False
        except Exception as e:
            print(f"[asr] 预转写失败 {vid}: {str(e)[:100]}", flush=True)
            return vid, None, False

    done = 0
    total = len(todo)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as ex:
        futs = {ex.submit(_one, vid, p): vid for vid, p in todo}
        for fut in as_completed(futs):
            vid, data, from_cache = fut.result()
            results[vid] = data
            done += 1
            tag = "缓存" if from_cache else ("成功" if data else "失败")
            print(f"[asr] 预转写 {done}/{total} {vid[:12]}… {tag}", flush=True)
            if on_progress:
                on_progress(done, total, vid, bool(data))
    return results


def _split_sentences(text, max_chars=60):
    parts = re.split(r"(?<=[。！？!?；;])\s*", text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    merged = []
    for p in parts:
        if len(p) > max_chars:
            for s in re.split(r"(?<=[，,])\s*", p):
                if s.strip():
                    merged.append(s.strip())
        elif merged and len(p) <= 3 and len(merged[-1]) < max_chars:
            merged[-1] += p
        else:
            merged.append(p)
    return merged


def transcribe(audio_path, video_id, title, url, conf, on_segment=None, use_cache=True):
    """云端转写 → {"segments", "full_text", "duration", ...}；失败抛异常。
    use_cache=True 时命中 data/transcripts/<vid>.json 直接返回（批量提速关键）。"""
    if use_cache:
        cached = load_cached(video_id)
        if cached:
            print(f"[asr] 命中转写缓存: {video_id}（{len(cached.get('segments') or [])} 段）", flush=True)
            if on_segment:
                for seg in cached.get("segments") or []:
                    on_segment(seg)
            return cached
    import shutil
    asr_url = (conf.get("base_url") or "").strip()
    model = (conf.get("model") or "FunAudioLLM/SenseVoiceSmall").strip()
    key = (conf.get("key") or "").strip()
    if not (asr_url and key):
        raise RuntimeError("未配置云端转写（硅基流动），请在设置中填写 ASR API Key")

    with open(audio_path, "rb") as f:
        audio_data = f.read()
    ext = os.path.splitext(audio_path)[1].lstrip(".").lower() or "mp3"
    ctype = {"m4a": "audio/mp4", "mp4": "audio/mp4", "wav": "audio/wav",
             "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg"}.get(ext, f"audio/{ext}")
    boundary = uuid.uuid4().hex
    body = []
    body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode())
    body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{os.path.basename(audio_path)}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
    body.append(audio_data)
    body.append(f'\r\n--{boundary}--\r\n'.encode())
    req = urllib.request.Request(asr_url, data=b"".join(body), method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    resp = None
    last_err = None
    for attempt in range(5):
        try:
            with opener.open(req, timeout=60) as r:
                resp = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            last_err = e
            detail = e.read().decode("utf-8", "replace")[:150]
            print(f"[asr] HTTP {e.code}: {detail}", flush=True)
            if e.code in RETRYABLE and attempt < 4:
                time.sleep(3 * (2 ** attempt))
                continue
            raise RuntimeError(f"转写接口错误 HTTP {e.code}: {detail}") from e
        except Exception as e:
            last_err = e
            print(f"[asr] 连接异常({attempt + 1}/5): {type(e).__name__} {str(e)[:100]}", flush=True)
            if attempt < 4:
                time.sleep(3 * (2 ** attempt))
                continue
            raise RuntimeError(f"转写接口连接失败: {last_err}") from e
    if resp is None:
        raise RuntimeError("转写接口未返回结果")

    text = (resp.get("text") or "").strip()
    duration = float((resp.get("usage") or {}).get("seconds") or 0)
    probe = probe_duration(audio_path)
    if probe > 1:
        duration = probe
    lang = (resp.get("language") or "zh").lower()

    if not text:
        data = {"video_id": video_id, "title": title, "url": url, "duration": duration,
                "language": lang, "segments": [], "full_text": ""}
        _save(data)
        return data

    sents = _split_sentences(text)
    span = duration if duration > 0 else max(len(text) / 4.0, 1.0)
    segs = []
    cum, total_len, cur = 0, sum(len(s) for s in sents) or 1, 0.0
    for i, s in enumerate(sents):
        cum += len(s)
        nxt = span * (cum / total_len) if i < len(sents) - 1 else span
        seg = {"start": round(cur, 2), "end": round(nxt, 2), "text": s}
        segs.append(seg)
        if on_segment:
            on_segment(seg)
        cur = nxt
    data = {"video_id": video_id, "title": title, "url": url, "duration": duration or span,
            "language": lang, "segments": segs, "full_text": "".join(s["text"] for s in segs)}
    _save(data)
    return data


def _save(data):
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    with open(os.path.join(TRANSCRIPT_DIR, f"{data['video_id']}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
