# -*- coding: utf-8 -*-
"""模型列表：直接读取服务商官方 /models 接口，保证模型名与官方一致。"""
import os
import json
import urllib.request
import urllib.error


def _models_url(base_url):
    """把 chat/completions 地址推导为 models 地址"""
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/chat/completions"):
        return u[: -len("/chat/completions")] + "/models"
    if u.endswith("/completions"):
        return u[: -len("/completions")] + "/models"
    return u + "/models"


# 非对话类模型后缀（ASR/TTS/向量等，不能用于改写或画面理解）
NON_CHAT_HINTS = ("-asr", "-tts", "whisper", "sensevoice", "embedding", "rerank",
                  "voiceclone", "voicedesign", "-ocr")


def list_models(base_url, key, timeout=20, chat_only=False):
    """GET {base}/models → [模型名]；chat_only=True 时过滤掉 ASR/TTS/向量等非对话模型"""
    url = _models_url(base_url)
    if not url:
        raise RuntimeError("未配置 Base URL")
    if not (key or "").strip():
        raise RuntimeError("未配置 API Key")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key.strip()}",
        "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"请求失败: {type(e).__name__}: {str(e)[:120]}") from e

    items = d.get("data") or d.get("models") or []
    out = []
    for it in items:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
        else:
            mid = str(it)
        if mid:
            out.append(mid)
    if not out:
        raise RuntimeError("接口返回中未找到模型列表")
    if chat_only:
        filtered = [m for m in out if not any(h in m.lower() for h in NON_CHAT_HINTS)]
        if filtered:
            out = filtered
    return sorted(set(out))