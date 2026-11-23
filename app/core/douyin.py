# -*- coding: utf-8 -*-
"""抖音抓取：无头浏览器抓音频/视频流、作者视频清单。"""
import os
import re
import json
import time
import threading

import browser
import accounts

from paths import BASE, DATA_DIR
import ffmpeg_tool
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
VIDEO_DIR = os.path.join(DATA_DIR, "video")

_LOCK = threading.Lock()
# 视频抓取用独立锁：与音频下载互不阻塞（两者并行时各用各的浏览器会话）
_VIDEO_LOCK = threading.Lock()


def extract_video_id(url):
    """支持 /video/、modal_id、纯数字、v.douyin.com 短链"""
    url = (url or "").strip()
    m = re.search(r"/video/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"modal_id=(\d+)", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d+", url):
        return url
    if "v.douyin.com" in url:
        try:
            import urllib.request
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": browser.UA})
            final = urllib.request.urlopen(req, timeout=10).geturl()
            m = re.search(r"/video/(\d+)", final) or re.search(r"modal_id=(\d+)", final)
            if m:
                return m.group(1)
        except Exception:
            pass
    return None


def _is_stream(u, kind):
    if "douyinvod" not in u:
        return False
    has_audio = "media-audio" in u
    if kind == "video":
        return "mime_type=video_mp4" in u and not has_audio
    return has_audio or "mime_type=audio" in u


def capture_media_url(video_id, kind="audio"):
    """无头打开视频页，捕获带签名的媒体流地址。kind: audio | video"""
    profile = accounts.profile_dir()
    if not os.path.isdir(profile):
        raise RuntimeError("登录态不存在，请先登录账号")
    lock = _LOCK if kind == "audio" else _VIDEO_LOCK
    with lock:
        page = None
        try:
            import browser as bu
            sb = bu.shared()
            ctx, page = sb.new_page(profile, headless=True, args=browser.LAUNCH_ARGS,
                                    viewport=browser.VIEWPORT, user_agent=browser.UA, locale="zh-CN")
            found = {"url": "", "detail": None, "mismatch": ""}

            def on_resp(resp):
                if "aweme/v1/web/aweme/detail" in resp.url and not found["detail"]:
                    try:
                        a = (resp.json() or {}).get("aweme_detail")
                        if a:
                            got = str(a.get("aweme_id") or "")
                            if got and got != str(video_id):
                                found["mismatch"] = got
                            else:
                                found["detail"] = a
                    except Exception:
                        pass
                if _is_stream(resp.url, kind) and resp.status in (200, 206) and not found["url"]:
                    found["url"] = resp.url

            page.on("response", on_resp)

            def _play_urls():
                v = ((found["detail"] or {}).get("video") or {})
                urls = []
                for holder in (v.get("play_addr") or {}, v.get("download_addr") or {}):
                    for u in holder.get("url_list") or []:
                        if u and u not in urls:
                            urls.append(u)
                return urls

            for attempt in range(2):
                try:
                    # commit：拿到响应头即返回；超时设短，流事件已并行监听
                    page.goto(f"https://www.douyin.com/video/{video_id}",
                              wait_until="commit", timeout=8000)
                except Exception as e:
                    print(f"[douyin] 打开视频页超时(第{attempt + 1}次): {str(e)[:60]}", flush=True)
                for _ in range(100):
                    if found["url"]:
                        break
                    page.wait_for_timeout(200)
                if found["url"]:
                    return found["url"]
                if found["detail"] and _play_urls():
                    return _play_urls()[0]
                if attempt == 0:
                    page.wait_for_timeout(1000)
            if found.get("mismatch"):
                raise RuntimeError(f"抖音返回的视频与链接不一致：请求 {video_id}，实际 {found['mismatch']}")
            print(f"[douyin] 未捕获到 {kind} 流", flush=True)
            return None
        except Exception as e:
            print(f"[douyin] 捕获 {kind} 流失败: {str(e)[:150]}", flush=True)
            return None
        finally:
            if page is not None:
                sb.close_page(page)


def _http_get(url, timeout=300):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "Referer": "https://www.douyin.com/", "User-Agent": browser.UA})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _fetch_media(url, retries=3, timeout=120):
    """下载单个媒体流，失败重试（间隔递增，避免一次 TLS 抖动就降级）"""
    last = None
    for i in range(retries + 1):
        try:
            data = _http_get(url, timeout=timeout)
            if len(data) > 5000:
                return data
            last = f"内容过小({len(data)}字节)"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
        if i < retries:
            time.sleep(1.0 * (i + 1))
    print(f"[douyin] 流下载失败(已试{retries + 1}次): {last}", flush=True)
    return None


def capture_streams(video_id):
    """一次打开视频页，同时捕获【音频流 + 视频流】地址与详情。
    返回 dict: {audio_url, video_url, detail, title, duration}"""
    profile = accounts.profile_dir()
    if not os.path.isdir(profile):
        raise RuntimeError("登录态不存在，请先登录账号")
    with _LOCK:
        page = None
        try:
            import browser as bu
            sb = bu.shared()
            ctx, page = sb.new_page(profile, headless=True, args=browser.LAUNCH_ARGS,
                                    viewport=browser.VIEWPORT, user_agent=browser.UA,
                                    locale="zh-CN")
            found = {"audio": "", "video": "", "detail": None, "title": "",
                     "video_list": []}

            def on_resp(resp):
                u = resp.url
                if "douyinvod" not in u or resp.status not in (200, 206):
                    return
                is_audio = "media-audio" in u
                if is_audio and not found["audio"]:
                    found["audio"] = u
                elif not is_audio and "mime_type=video_mp4" in u:
                    if u not in found["video_list"]:
                        found["video_list"].append(u)
                    if not found["video"]:
                        found["video"] = u
                if "aweme/v1/web/aweme/detail" in u and not found["detail"]:
                    try:
                        a = (resp.json() or {}).get("aweme_detail")
                        if a:
                            got = str(a.get("aweme_id") or "")
                            if not got or got == str(video_id):
                                found["detail"] = a
                    except Exception:
                        pass

            page.on("response", on_resp)
            try:
                # commit 只需拿到响应头；超时设短，避免页面慢时白等（流事件已在并行监听）
                page.goto(f"https://www.douyin.com/video/{video_id}",
                          wait_until="commit", timeout=8000)
            except Exception as e:
                print(f"[douyin] 页面提交超时(已继续监听流): {str(e)[:50]}", flush=True)
            # 等两条流（音频优先；最多 30 秒），视频一到也立即记录
            for _ in range(150):
                if found["audio"] and found["video_list"]:
                    break
                page.wait_for_timeout(200)

            detail = found["detail"] or {}
            # 详情被风控拦截：在同一个页面内补一次请求（不重开页面，省约 10 秒）
            if not (detail.get("statistics") or {}).get("digg_count"):
                try:
                    d2 = page.evaluate(
                        """async (vid) => {
                            try {
                                const r = await fetch('/aweme/v1/web/aweme/detail/?aweme_id=' + vid +
                                    '&device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1',
                                    {headers: {'Referer': 'https://www.douyin.com/'}});
                                const j = await r.json();
                                return j.aweme_detail || null;
                            } catch(e) { return null; }
                        }""", str(video_id))
                    if d2 and (d2.get("statistics") or {}).get("digg_count"):
                        detail = d2
                        print("[douyin] 详情已通过页面内请求补齐", flush=True)
                except Exception:
                    pass
            video = detail.get("video") or {}
            api_title = (detail.get("desc") or "").strip()
            if not api_title:
                try:
                    api_title = page.title().replace(" - 抖音", "").strip()
                except Exception:
                    api_title = ""
            ms = int(video.get("duration") or 0)
            duration = f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}" if ms else ""
            # 优先低码率视频流：画面分析会压到 480p，低码率足够且下载快数倍
            video_url = found["video"]
            if found["video_list"]:
                def _br(u):
                    m = re.search(r"[?&]br=(\d+)", u)
                    return int(m.group(1)) if m else 999999
                video_url = min(found["video_list"], key=_br)
            print(f"[douyin] 捕获流: 音频={'有' if found['audio'] else '无'} "
                  f"视频={'有' if video_url else '无'}（候选{len(found['video_list'])}）", flush=True)
            return {"audio_url": found["audio"], "video_url": video_url,
                    "detail": detail, "title": api_title, "duration": duration,
                    "_video_obj": video}
        finally:
            if page is not None:
                sb.close_page(page)


def _materialize(video_id, info, want_video=False):
    """根据已抓到的流地址下载媒体并落盘（线程安全，可并行调用）"""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    m4a = os.path.join(AUDIO_DIR, f"{video_id}.m4a")
    meta_file = os.path.join(DATA_DIR, "meta", f"{video_id}.json")
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(VIDEO_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(meta_file), exist_ok=True)
    audio_cached = os.path.exists(m4a) and os.path.getsize(m4a) > 5000
    meta = {}
    if os.path.exists(meta_file):
        try:
            meta = json.load(open(meta_file, encoding="utf-8"))
        except Exception:
            meta = {}

    info = info or {}
    detail = info.get("detail") or {}
    video_obj = info.get("_video_obj") or {}
    api_title = info.get("title") or meta.get("title") or f"视频{video_id}"
    duration = info.get("duration") or meta.get("duration") or ""

    fallback_urls = []
    for holder in (video_obj.get("play_addr") or {}, video_obj.get("download_addr") or {}):
        for u in holder.get("url_list") or []:
            if u and u not in fallback_urls:
                fallback_urls.append(u)

    # 音频、视频并行下载
    audio_fut = video_fut = None
    pool = ThreadPoolExecutor(max_workers=2)
    if not audio_cached:
        audio_fut = pool.submit(lambda: _fetch_media(info["audio_url"]) if info.get("audio_url") else None)
    if want_video:
        if info.get("video_url"):
            video_fut = pool.submit(_fetch_media, info["video_url"], 2, 300)
        elif fallback_urls:
            video_fut = pool.submit(_fetch_media, fallback_urls[0], 2, 300)

    audio_bytes = None
    video_bytes = None
    if audio_fut:
        audio_bytes = audio_fut.result()
    if video_fut:
        try:
            video_bytes = video_fut.result()
        except Exception as e:
            print(f"[douyin] 视频下载异常: {str(e)[:80]}", flush=True)
    pool.shutdown(wait=False)

    if not audio_cached:
        if audio_bytes:
            with open(m4a, "wb") as f:
                f.write(audio_bytes)
            print(f"[douyin] 音频就绪: {len(audio_bytes)} bytes（纯音频流）", flush=True)
        else:
            src_data = video_bytes
            if not src_data:
                for u in fallback_urls[:2]:
                    src_data = _fetch_media(u, retries=1, timeout=300)
                    if src_data:
                        break
            if not src_data:
                return {"ok": False, "error": "音频与视频流都下载失败"}
            src = m4a + ".src.mp4"
            with open(src, "wb") as f:
                f.write(src_data)
            try:
                ff = ffmpeg_tool.find("ffmpeg")
                subprocess.run([ff, "-y", "-v", "error", "-i", src, "-vn",
                                "-c:a", "aac", "-b:a", "96k", m4a],
                               check=True, timeout=600)
                print(f"[douyin] 音频就绪: 由视频抽音（{len(src_data)} bytes 源）", flush=True)
            finally:
                try:
                    os.remove(src)
                except OSError:
                    pass
        if not os.path.exists(m4a) or os.path.getsize(m4a) <= 5000:
            return {"ok": False, "error": "音频结果为空"}

    st = detail.get("statistics") or {}
    author = detail.get("author") or {}
    stats_pack = {
        "like": st.get("digg_count", "-"), "comment": st.get("comment_count", "-"),
        "collect": st.get("collect_count", "-"), "share": st.get("share_count", "-"),
        "play": st.get("play_count", "-"),
        "author": {"name": author.get("nickname", ""), "id": author.get("sec_uid", ""),
                   "fans": author.get("follower_count", "-")},
    }
    try:
        json.dump({"title": api_title, "duration": duration,
                   "stats": {k: stats_pack[k] for k in ("like", "comment", "collect", "share", "play")},
                   "author": stats_pack["author"]},
                  open(meta_file, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return {"ok": True, "title": api_title, "duration": duration, "audio_path": m4a,
            "video_bytes": video_bytes, "stats": stats_pack, "error": ""}


def prepare_media_batch(video_ids, want_video=False, on_progress=None):
    """批量：单浏览器串行抓流（避免同一 profile 争抢），然后并行下载媒体。
    返回 {video_id: prep_dict}"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    infos = {}
    total = len(video_ids)
    for i, vid in enumerate(video_ids):
        if on_progress:
            on_progress("capture", i + 1, total, vid)
        try:
            infos[vid] = capture_streams(vid)
        except Exception as e:
            print(f"[douyin] 抓流失败 {vid}: {str(e)[:80]}", flush=True)
            infos[vid] = None
    results = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_materialize, vid, infos.get(vid), want_video): vid
                for vid in video_ids}
        for fut in as_completed(futs):
            vid = futs[fut]
            if on_progress:
                on_progress("download", 0, total, vid)
            try:
                results[vid] = fut.result()
            except Exception as e:
                print(f"[douyin] 下载失败 {vid}: {str(e)[:80]}", flush=True)
                results[vid] = {"ok": False, "error": str(e)[:100]}
    return results


def prepare_media(video_id, want_video=False, on_progress=None):
    """一次抓取 + 音频/视频并行下载（核心提速入口）。
    返回 dict: {ok, title, duration, audio_path, video_bytes, stats, error}
    - 音频就绪即可转写
    - video_bytes 不为空表示画面分析可直接用，无需再开浏览器
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    m4a = os.path.join(AUDIO_DIR, f"{video_id}.m4a")
    meta_file = os.path.join(DATA_DIR, "meta", f"{video_id}.json")
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(VIDEO_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(meta_file), exist_ok=True)

    audio_cached = os.path.exists(m4a) and os.path.getsize(m4a) > 5000
    meta = {}
    if os.path.exists(meta_file):
        try:
            meta = json.load(open(meta_file, encoding="utf-8"))
        except Exception:
            meta = {}

    # 音频已缓存且不需要视频 → 直接返回（不启动浏览器）
    # 但若 meta 缺互动数据，仍要走一次抓取补数据
    meta_has_stats = (meta.get("stats") or {}).get("like") not in (None, "-", "")
    if audio_cached and not want_video and meta_has_stats:
        return {"ok": True, "title": meta.get("title", ""), "duration": meta.get("duration", ""),
                "audio_path": m4a, "video_bytes": None, "stats": meta.get("stats"), "error": ""}

    try:
        info = capture_streams(video_id)
    except Exception as e:
        if audio_cached:
            return {"ok": True, "title": meta.get("title", ""), "duration": meta.get("duration", ""),
                    "audio_path": m4a, "video_bytes": None, "stats": meta.get("stats"),
                    "error": f"抓流失败但音频已缓存: {str(e)[:80]}"}
        return {"ok": False, "error": f"抓取失败: {str(e)[:120]}"}

    detail = info.get("detail") or {}
    # 详情缺失时：只做一次轻量重试（页面内已尝试过，这里不再重开页面）
    if not (detail.get("statistics") or {}).get("digg_count"):
        print("[douyin] 详情仍缺失，互动数据将为空（抖音风控）", flush=True)

    return _materialize(video_id, info, want_video)


def download_audio(video_id, title_hint=""):
    """下载音频 → data/audio/<id>.m4a。返回 (ok, title, duration_str)。
    优先抓纯音频流（直接存 m4a），兜底视频流后 ffmpeg 抽音。"""
    import subprocess
    m4a = os.path.join(AUDIO_DIR, f"{video_id}.m4a")
    meta_file = os.path.join(DATA_DIR, "meta", f"{video_id}.json")
    if os.path.exists(m4a) and os.path.getsize(m4a) > 5000:
        # 缓存命中：从 meta 读取标题/时长，避免标题丢失
        try:
            m = json.load(open(meta_file, encoding="utf-8"))
            return True, m.get("title") or title_hint, m.get("duration") or ""
        except Exception:
            return True, title_hint, ""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(meta_file), exist_ok=True)
    with _LOCK:
        page = None
        try:
            import browser as bu
            sb = bu.shared()
            ctx, page = sb.new_page(accounts.profile_dir(), headless=True,
                                    args=browser.LAUNCH_ARGS, viewport=browser.VIEWPORT,
                                    user_agent=browser.UA, locale="zh-CN")
            found = {"detail": None, "audio_url": ""}

            def on_resp(resp):
                u = resp.url
                try:
                    if "aweme/v1/web/aweme/detail" in u and not found["detail"]:
                        a = (resp.json() or {}).get("aweme_detail")
                        if a:
                            got = str(a.get("aweme_id") or "")
                            if got and got != str(video_id):
                                print(f"[douyin] 视频ID不一致: 请求 {video_id} 实际 {got}", flush=True)
                            else:
                                found["detail"] = a
                    if "douyinvod" in u and resp.status in (200, 206) and not found["audio_url"]:
                        if "media-audio" in u or "mime_type=audio" in u:
                            found["audio_url"] = u
                except Exception:
                    pass

            page.on("response", on_resp)
            try:
                page.goto(f"https://www.douyin.com/video/{video_id}",
                          wait_until="commit", timeout=8000)
            except Exception as e:
                print(f"[douyin] 页面提交超时(已继续监听流): {str(e)[:60]}", flush=True)
            # 优先等纯音频流（约 4MB，比整段视频快得多），最多 30 秒
            for _ in range(150):
                if found["audio_url"]:
                    break
                page.wait_for_timeout(200)

            detail = found["detail"] or {}
            video = detail.get("video") or {}
            candidates = []
            audio_only = False
            if found["audio_url"]:
                candidates.append(found["audio_url"])
                audio_only = True
            for holder in (video.get("play_addr") or {}, video.get("download_addr") or {}):
                for u in holder.get("url_list") or []:
                    if u and u not in candidates:
                        candidates.append(u)
            if not candidates:
                raise RuntimeError("页面未捕获到音频/播放地址，请确认登录态有效")

            data = b""
            used = ""
            for media_url in candidates:
                try:
                    resp = ctx.request.get(media_url, headers={"Referer": "https://www.douyin.com/"},
                                           timeout=120000)
                    if resp.ok:
                        data = resp.body()
                        if len(data) > 5000:
                            used = media_url
                            break
                except Exception as e:
                    print(f"[douyin] 媒体地址下载失败，尝试下一地址: {str(e)[:80]}", flush=True)
            if len(data) <= 5000:
                raise RuntimeError(f"音频下载失败或文件过小({len(data)} bytes)")

            if "media-audio" in used or "mime_type=audio" in used:
                with open(m4a, "wb") as f:
                    f.write(data)
            else:
                # 兜底拿到的是整段视频 → ffmpeg 抽音频（满足 ASR 50MB 限制）
                src = m4a + ".src.mp4"
                with open(src, "wb") as f:
                    f.write(data)
                try:
                    import shutil
                    import ffmpeg_tool
                    ff = ffmpeg_tool.find("ffmpeg")
                    subprocess.run([ff, "-y", "-v", "error", "-i", src, "-vn",
                                    "-c:a", "aac", "-b:a", "96k", m4a],
                                   check=True, timeout=600)
                finally:
                    try:
                        os.remove(src)
                    except OSError:
                        pass
            if not os.path.exists(m4a) or os.path.getsize(m4a) <= 5000:
                raise RuntimeError("音频结果为空")

            api_title = (detail.get("desc") or "").strip()
            if not api_title:
                try:
                    api_title = page.title().replace(" - 抖音", "").strip()
                except Exception:
                    api_title = ""
            if not api_title:
                api_title = title_hint or video_id
            ms = int(video.get("duration") or 0)
            duration = f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}" if ms else ""
            # 保存元信息（标题/时长/互动数据），供缓存命中与统计复用
            try:
                st = detail.get("statistics") or {}
                author = detail.get("author") or {}
                json.dump({
                    "title": api_title, "duration": duration,
                    "stats": {
                        "like": st.get("digg_count", "-"),
                        "comment": st.get("comment_count", "-"),
                        "collect": st.get("collect_count", "-"),
                        "share": st.get("share_count", "-"),
                        "play": st.get("play_count", "-"),
                    },
                    "author": {"name": author.get("nickname", ""), "id": author.get("sec_uid", ""),
                               "fans": author.get("follower_count", "-")},
                }, open(os.path.join(DATA_DIR, "meta", f"{video_id}.json"), "w", encoding="utf-8"),
                    ensure_ascii=False)
            except Exception:
                pass
            kind_str = "纯音频" if audio_only else "视频抽音"
            print(f"[douyin] 下载成功: {api_title[:30]} ({len(data)} bytes, {kind_str})", flush=True)
            return True, api_title, duration
        except Exception as e:
            print(f"[douyin] 下载失败: {str(e)[:150]}", flush=True)
            return False, title_hint, ""
        finally:
            if page is not None:
                sb.close_page(page)


def download_video(video_id):
    """下载完整视频（画面分析用）→ data/video/<id>.mp4，返回 bytes。"""
    os.makedirs(VIDEO_DIR, exist_ok=True)
    url = capture_media_url(video_id, kind="video")
    if not url:
        return None
    try:
        return _http_get(url, timeout=300)
    except Exception as e:
        print(f"[douyin] 视频流下载失败: {str(e)[:150]}", flush=True)
        return None


def clean_title(raw):
    if not raw:
        return "(无标题)"
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    keep = []
    for l in lines:
        if l == "置顶" or re.fullmatch(r"[\d.,]+[万kK]?", l):
            continue
        if l not in keep:
            keep.append(l)
    with_tag = [l for l in keep if "#" in l]
    return (with_tag or keep or ["(无标题)"])[0]


def fetch_author_videos(url, max_count=5):
    """无头扫博主主页 → [{video_id, title, url}]"""
    import browser as bu
    videos = {}
    sb = bu.shared()
    ctx, pg = sb.new_page(accounts.profile_dir(), headless=True,
                          args=browser.LAUNCH_ARGS)
    try:
        pg.route("**/*", lambda route: route.abort()
                 if route.request.resource_type in ("image", "media", "font", "stylesheet")
                 else route.continue_())
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(8)
        try:
            pg.click("text=作品", timeout=3000)
            time.sleep(2)
        except Exception:
            pass
        last_count, stable = 0, 0
        for _ in range(80):
            pg.mouse.wheel(0, 1500)
            time.sleep(1.2)
            links = pg.eval_on_selector_all(
                "a[href*='/video/']",
                "els => els.map(e => ({href: e.href, title: (e.getAttribute('title') || e.innerText || '').trim()}))")
            for l in links:
                m = re.search(r"/video/(\d+)", l["href"])
                if m and m.group(1) not in videos:
                    videos[m.group(1)] = {
                        "video_id": m.group(1), "title": clean_title(l["title"]),
                        "url": f"https://www.douyin.com/video/{m.group(1)}"}
            if len(videos) == last_count:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            last_count = len(videos)
            if len(videos) >= max_count:
                break
    finally:
        sb.close_page(pg)
    return list(videos.values())[:max_count]
