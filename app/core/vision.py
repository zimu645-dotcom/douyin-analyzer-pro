# -*- coding: utf-8 -*-
"""画面分析：整段视频 → 飞书托管 URL → MiMo 分段流式分析。只走 URL（v2.6-flash 不支持本地上传）。"""
import os
import sys
import json
import time
import base64
import uuid
import threading
import subprocess
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from paths import BASE, DATA_DIR
import ffmpeg_tool

VIDEO_PROMPT = """你是短视频拆解专家。下面是一条完整的抖音视频（画面 + 声音）。
请从以下四个角度分析，直接输出结果，不要任何前缀，不要使用 **、#、- 这类 Markdown 符号：

一、整体结构
开头钩子、中段推进、结尾收束，标注大致时间点。

二、画面语言
镜头运动、景别、转场、字幕/花字的使用。

三、视觉风格
色调、光线、场景、人物或产品呈现方式。

四、可复用要点
这条视频最值得借鉴的 3 个具体做法，每条一行。"""

CHUNK_SEC = 120
URL_MAX = 300 * 1024 * 1024
# 分段并行分析的并发上限（实测并发 3 最优，再高会被 MiMo 排队反而更慢）
MAX_PARALLEL = 3
# 飞书上传并发上限：飞书对上传有频率限制（99991400），上传走低频串行更稳
UPLOAD_PARALLEL = 2

# 超过此体积先压到 480p：飞书单次上传大文件易被掐断，压缩后更稳且分析更快
COMPRESS_THRESHOLD = 8 * 1024 * 1024

# 模型内容审核拦截的特征词（返回的不是分析内容）
REJECT_HINTS = ("rejected because", "considered high risk", "content policy",
                "content_policy", "content filter", "violates")


def _feishu_host(data, filename, feishu_conf, retries=3):
    """视频上传飞书云空间 → 临时直链。遇飞书频率限制(99991400)自动退避重试。"""
    app_id = (feishu_conf.get("app_id") or "").strip()
    secret = (feishu_conf.get("app_secret") or "").strip()
    app_token = (feishu_conf.get("app_token") or "").strip()
    if not (app_id and secret and app_token):
        print("[vision] 未配置飞书凭证，无法托管视频", flush=True)
        return None
    from core.feishu import FeishuClient
    last_msg = ""
    for attempt in range(retries + 1):
        try:
            c = FeishuClient(app_id, secret)
            token = c.get_token()
            boundary = uuid.uuid4().hex
            body = []
            for k, v in [("file_name", filename), ("parent_type", "bitable_file"),
                         ("parent_node", app_token), ("size", str(len(data))), ("extra", "")]:
                body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                        f'filename="{filename}"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
            body.append(data)
            body.append(f'\r\n--{boundary}--\r\n'.encode())
            r = c._raw("POST", f"{c.BASE_URL}/drive/v1/medias/upload_all", b"".join(body),
                       {"Authorization": "Bearer " + token,
                        "Content-Type": f"multipart/form-data; boundary={boundary}"})
            if r.get("code") != 0:
                last_msg = f"{r.get('code')} {r.get('msg')}"
                # 99991400 = 频率限制，退避重试；其他错误直接返回
                if "99991400" in str(r.get("code")) or "frequency" in str(r.get("msg")).lower():
                    wait = 2 * (attempt + 1)
                    print(f"[vision] 飞书限流，{wait}s 后重试({attempt + 1}/{retries})", flush=True)
                    time.sleep(wait)
                    continue
                print(f"[vision] 飞书上传失败: {r.get('msg')}", flush=True)
                return None
            ft = (r.get("data") or {}).get("file_token")
            g = c._req("GET", f"{c.BASE_URL}/drive/v1/medias/batch_get_tmp_download_url"
                       f"?file_tokens={urllib.parse.quote(ft)}",
                       headers={"Authorization": "Bearer " + token})
            urls = (g.get("data") or {}).get("tmp_download_urls") or []
            u = urls[0].get("tmp_download_url") if urls else None
            if u:
                return u
        except Exception as e:
            last_msg = f"{type(e).__name__}: {str(e)[:100]}"
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
    print(f"[vision] 飞书托管失败({last_msg[:80]})", flush=True)
    return None


def _compress(data, video_id):
    """压到 480p CRF32，减小上传体积与 MiMo 处理时长"""
    tmpdir = os.path.join(DATA_DIR, "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    src = os.path.join(tmpdir, f"{video_id}_src.mp4")
    dst = os.path.join(tmpdir, f"{video_id}_min.mp4")
    with open(src, "wb") as f:
        f.write(data)
    try:
        ff = ffmpeg_tool.find("ffmpeg")
        if not os.path.isfile(ff):
            print(f"[vision] 压缩失败: 未找到 ffmpeg（请安装 ffmpeg 或把 ffmpeg.exe 放到程序目录）", flush=True)
            return None
        rr = subprocess.run([ff, "-y", "-v", "error", "-i", src,
                             "-vf", "scale='min(480,iw)':-2", "-c:v", "libx264",
                             "-preset", "veryfast", "-crf", "32",
                             "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", dst],
                            timeout=600, capture_output=True)
        if rr.returncode != 0:
            err = (rr.stderr or b"").decode("utf-8", "replace")[:200]
            print(f"[vision] 压缩失败(ffmpeg 返回 {rr.returncode}): {err}", flush=True)
            return None
        if not os.path.exists(dst) or os.path.getsize(dst) < 5000:
            print("[vision] 压缩失败: 输出文件为空", flush=True)
            return None
        with open(dst, "rb") as f:
            out = f.read()
        print(f"[vision] 压缩: {len(data)//1024//1024}MB → {len(out)//1024//1024}MB", flush=True)
        return out
    except Exception as e:
        print(f"[vision] 压缩失败: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None
    finally:
        for p in (src, dst):
            try:
                os.remove(p)
            except OSError:
                pass


def _split_chunks(data, video_id, chunk_sec=CHUNK_SEC):
    """按视频真实时长分段（ffprobe 读实际时长，stream copy 不重编码）。
    末尾碎片自动均分：若最后一段过短（< 1/3 段长），减少段数把总时长均分，
    避免出现十几秒的零碎段。"""
    import shutil
    tmpdir = os.path.join(DATA_DIR, "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    src = os.path.join(tmpdir, f"{video_id}_full.mp4")
    with open(src, "wb") as f:
        f.write(data)
    chunks = []
    starts = []
    try:
        # 时长读取：优先 ffprobe，缺失时用 ffmpeg 解析（打包只带 ffmpeg）
        from core.asr import probe_duration
        dur = probe_duration(src)
        if dur <= chunk_sec + 1:
            print(f"[vision] 视频 {dur:.0f}s ≤ {chunk_sec}s，不分段直接分析", flush=True)
            return [data], [0.0]

        # 按真实时长计算段数
        count = int(dur // chunk_sec)
        tail = dur - count * chunk_sec
        if tail > 0:
            if tail < chunk_sec / 3 and count >= 1:
                # 末尾太短 → 不单独成段，把总时长均分到 count 段
                seg_len = dur / count
                print(f"[vision] 视频 {dur:.0f}s → {count} 段，每段 {seg_len:.0f}s"
                      f"（末尾 {tail:.0f}s 并入均分）", flush=True)
            else:
                count += 1
                seg_len = chunk_sec
                print(f"[vision] 视频 {dur:.0f}s → {count} 段，每段约 {seg_len:.0f}s"
                      f"（末段 {tail:.0f}s）", flush=True)
        else:
            seg_len = chunk_sec
            print(f"[vision] 视频 {dur:.0f}s → {count} 段，每段 {seg_len:.0f}s", flush=True)

        for idx in range(count):
            start = idx * seg_len
            length = min(seg_len, max(1.0, dur - start))
            dst = os.path.join(tmpdir, f"{video_id}_p{idx}.mp4")
            rr = subprocess.run([ffmpeg_tool.find("ffmpeg"), "-y", "-v", "error",
                                 "-ss", f"{start:.2f}", "-t", f"{length:.2f}",
                                 "-i", src, "-c", "copy", "-reset_timestamps", "1", dst],
                                timeout=300, capture_output=True)
            if rr.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 5000:
                with open(dst, "rb") as f:
                    chunks.append(f.read())
                starts.append(round(start, 1))
            try:
                os.remove(dst)
            except OSError:
                pass
    except Exception as e:
        print(f"[vision] 分段失败: {str(e)[:100]}", flush=True)
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
    if len(chunks) <= 1:
        return [data], [0.0]
    return chunks, starts


def _stream_call(conf, payload, on_token=None, timeout=300):
    """MiMo SSE 流式；空结果回退非流式。"""
    p = dict(payload)
    p["stream"] = True
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(conf["base_url"], data=json.dumps(p).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + conf["key"],
                                          "Accept": "text/event-stream"})
    parts = []
    try:
        with opener.open(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    obj = json.loads(data_str)
                except Exception:
                    continue
                if obj.get("error"):
                    raise RuntimeError((obj["error"] or {}).get("message") or "流式返回错误")
                ch = (obj.get("choices") or [{}])[0]
                tok = (ch.get("delta") or {}).get("content")
                if tok:
                    parts.append(tok)
                    if on_token:
                        on_token(tok)
        text = "".join(parts).strip()
        return text if text else _call_plain(conf, payload, timeout=timeout)
    except Exception:
        if parts:
            return "".join(parts).strip()
        return _call_plain(conf, payload, timeout=timeout)


def _call_plain(conf, payload, timeout=300):
    import urllib.error
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(conf["base_url"], data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + conf["key"]})
    try:
        with opener.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        # 400 常见原因：视频过短/过小、URL 失效、fps 超范围 —— 把服务端原话带出来
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e
    if not d.get("choices"):
        err = d.get("error") or {}
        raise RuntimeError(err.get("message") or "接口返回无 choices")
    msg = d["choices"][0]["message"]
    c = msg.get("content")
    if isinstance(c, list):
        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return (c or "").strip()


def analyze_video(video_id, duration, vision_conf, feishu_conf, emit=None, video_bytes=None):
    """整段视频 → 分段 URL → MiMo 流式分析。
    video_bytes: 若上游已下载好视频（与音频并行下载），直接复用，不再重复抓取。
    emit(event_dict): 实时事件回调。返回 [{time, image, analysis}]"""
    from core import douyin
    conf = {k: vision_conf.get(k) for k in ("base_url", "model", "key")}
    try:
        fps = max(0.1, min(10.0, float(vision_conf.get("fps") or 1)))
    except (TypeError, ValueError):
        fps = 1.0
    # 段长与并发可配置（长视频建议 15~45 秒一段 + 并发 6）
    try:
        chunk_sec = max(5, min(600, int(vision_conf.get("chunk_sec") or CHUNK_SEC)))
    except (TypeError, ValueError):
        chunk_sec = CHUNK_SEC
    try:
        parallel = max(1, min(10, int(vision_conf.get("parallel") or MAX_PARALLEL)))
    except (TypeError, ValueError):
        parallel = MAX_PARALLEL

    data = video_bytes
    if not data:
        data = douyin.download_video(video_id)
    if not data:
        return [{"time": 0, "image": "", "analysis": "(视频下载失败，无法画面分析)"}]
    if len(data) > COMPRESS_THRESHOLD:
        data = _compress(data, video_id)
        if not data:
            return [{"time": 0, "image": "", "analysis": "(视频压缩失败)"}]

    chunks, starts = _split_chunks(data, video_id, chunk_sec)
    total = len(chunks)
    # 阶段一：托管上传（飞书有上传频率限制，低并发串行 + 退避，避免撞 99991400）
    print(f"[vision] 上传 {total} 段到飞书（低并发，避免限流）…", flush=True)
    seg_urls = [None] * total
    _up_lock = threading.Semaphore(UPLOAD_PARALLEL)

    def _upload(i):
        with _up_lock:
            return i, _feishu_host(chunks[i], f"{video_id}_p{i}.mp4", feishu_conf)

    with ThreadPoolExecutor(max_workers=UPLOAD_PARALLEL) as ex:
        for i, u in ex.map(_upload, range(total)):
            seg_urls[i] = u
    ok_urls = sum(1 for u in seg_urls if u)
    print(f"[vision] 托管完成 {ok_urls}/{total}", flush=True)

    # 阶段二：MiMo 分析（RPM 100 足够，高并发）
    if total == 1:
        return [_analyze_chunk(conf, fps, video_id, chunks[0], 0, 1, feishu_conf,
                               emit, starts[0], seg_urls[0])]
    results = [None] * total
    workers = min(parallel, total)
    print(f"[vision] 分段分析：{total} 段并行（并发 {workers}）", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_analyze_chunk, conf, fps, video_id, chunks[i], i, total,
                          feishu_conf, emit, starts[i], seg_urls[i]): i
                for i in range(total)}
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                print(f"[vision] 第{idx + 1}段异常: {str(e)[:100]}", flush=True)
                results[idx] = {"time": starts[idx] if idx < len(starts) else 0, "image": "",
                                "analysis": f"(第{idx + 1}段失败: {str(e)[:60]})"}
    return [r for r in results if r]


def _analyze_chunk(conf, fps, video_id, chunk, i, total, feishu_conf, emit,
                   start=0.0, url=None):
    """单段：使用已托管的 URL 做 MiMo 流式分析 → 返回 {time, image, analysis}"""
    label = f"{int(start) // 60:02d}:{int(start) % 60:02d}"
    if emit:
        emit({"type": "vision_segment_start", "video_id": video_id,
              "index": i + 1, "total": total, "time": start, "label": label})

    url = url or _feishu_host(chunk, f"{video_id}_p{i}.mp4", feishu_conf)
    if not url:
        msg = f"第 {i + 1}/{total} 段托管失败，本段跳过（仅支持 URL 方式）"
        item = {"time": start, "image": "", "analysis": msg}
        if emit:
            emit({"type": "vision_frame", "video_id": video_id, "frame": item})
        return item

    prompt = VIDEO_PROMPT if total == 1 else (
        f"这是同一条完整视频的第 {i + 1}/{total} 段，起始时间约 {label}。"
        f"请只分析本段，并按整条视频的绝对时间标注。\n{VIDEO_PROMPT}")
    payload = {"model": conf["model"],
               "messages": [{"role": "user", "content": [
                   {"type": "video_url", "video_url": {"url": url},
                    "fps": fps, "media_resolution": "default"},
                   {"type": "text", "text": prompt}]}],
               "max_completion_tokens": 2048}

    def on_tok(tok):
        if emit:
            emit({"type": "vision_token", "video_id": video_id,
                  "index": i + 1, "total": total, "time": start, "tok": tok})

    content, last_err = "", ""
    for attempt in range(2):
        try:
            content = _stream_call(conf, payload, on_token=on_tok, timeout=300)
        except Exception as e:
            content, last_err = "", f"(本段分析失败: {str(e)[:80]})"
        # 内容审核拦截不算有效结果，重试一次
        if content and any(h in content.lower() for h in REJECT_HINTS):
            print(f"[vision] 第{i + 1}段被内容审核拦截，重试", flush=True)
            content = ""
            last_err = f"(第{i + 1}段被模型内容审核拦截，无法分析)"
            if attempt == 0:
                time.sleep(2)
                continue
        if content:
            break
        if attempt == 0:
            payload["max_completion_tokens"] = 4096
            time.sleep(2)
    if not content:
        content = last_err or "(本段内容无法识别)"
    item = {"time": start, "image": "", "analysis": content}
    if emit:
        emit({"type": "vision_frame", "video_id": video_id, "frame": item})
    return item
