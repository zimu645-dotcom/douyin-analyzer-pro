# -*- coding: utf-8 -*-
"""LLM 调用：DeepSeek 主 + MiMo 兜底，支持流式；失败打印具体原因。"""
import os
import json
import time
import urllib.request
import urllib.error

BATCH_REWRITE_PROMPT = """你是顶级短视频口播润色专家。输入是 JSON 数组，每项 {id, text} 为一段语音转写文本（可能有同音错字、句子碎片化、口语冗余）。
请对每段做「高质量优化」——让这段内容比原文更精彩、更有感染力、更适合短视频口播。

要求：
1. 修正同音错字，补全碎句，理顺逻辑
2. 优化措辞：更精准、生动、有画面感，增强口语节奏和情绪张力
3. 删掉重复、口头禅、冗余填充
4. 保留原文核心信息与语气风格，不夸大不编造
5. 输出 JSON 数组：[{"id": 原id, "text": "优化后的文本"}]，只输出 JSON，不要其他内容

输入：
{payload}"""

ANALYZE_PROMPT = """你是短视频内容分析专家。给定视频转写全文，输出结构化分析（JSON格式，不要markdown代码块）：
{{
  "summary": "一句话总结（30-50字，点明核心卖点和钩子）",
  "scenes": "适合参考的场景",
  "structure": "核心结构分析（钩子-信任-卖点-收尾，各多少秒）",
  "highlights": [{{"point": "值得学的点", "how": "原视频怎么做的", "use": "仿写时怎么用"}}],
  "improvements": [{{"point": "需要调整的点", "problem": "原片的问题", "fix": "仿写时怎么改"}}]
}}
要求：highlights 和 improvements 各 3-4 条，具体、可操作。
转写全文：
{full_text}"""


def _engines(rewrite_conf):
    engines = []
    for k in ("primary", "fallback"):
        e = rewrite_conf.get(k) or {}
        if e.get("base_url") and e.get("model"):
            engines.append({"url": e["base_url"], "model": e["model"],
                            "key": (e.get("key") or "").strip(),
                            "reasoning": e.get("reasoning", "off")})
    return engines


def _tune_payload(payload, eng):
    """按官方文档调优：
    - DeepSeek 默认开启思考模式(reasoning_effort=high)，正文迟迟不来会显得卡顿；
      润色/分析任务关闭思考，流式输出立刻可见
    - stream_options.include_usage 让最后一块带 usage
    """
    if payload.get("stream"):
        payload["stream_options"] = {"include_usage": True}
    mode = (eng.get("reasoning") or "off").lower()
    if mode == "off":
        # DeepSeek / MiMo 通用关闭思考的写法（不支持的厂商会忽略未知字段）
        payload["thinking"] = {"type": "disabled"}
        payload["reasoning_effort"] = "none"
    elif mode in ("low", "high", "max"):
        payload["reasoning_effort"] = mode
    return payload


def _call_once(eng, payload, timeout=120):
    """单次调用，返回 content 字符串；流式则返回 (full_text, chunks_iterator_flags)"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        eng["url"], data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {eng['key']}"})
    with opener.open(req, timeout=timeout) as r:
        if payload.get("stream"):
            full = []
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if isinstance(delta, list):
                        delta = "".join(x.get("text", "") for x in delta if isinstance(x, dict))
                    if delta:
                        full.append(delta)
                except Exception:
                    continue
            # 流式生成器模式在 chat() 里处理，这里直接返回全文
            return "".join(full).strip()
        d = json.loads(r.read().decode("utf-8"))
        content = d["choices"][0]["message"].get("content")
        if isinstance(content, list):
            content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
        return (content or "").strip()


def chat(user, system, rewrite_conf, max_tokens=1500, temperature=0.7,
         stream=False, on_token=None, max_retries=2):
    """主+兜底自动切换；失败打印具体原因。"""
    engines = _engines(rewrite_conf)
    if not engines:
        raise RuntimeError("未配置改写引擎")
    last_err = None
    for eng in engines:
        payload = {"model": eng["model"],
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "max_tokens": max_tokens, "temperature": temperature,
                   "stream": bool(stream)}
        payload = _tune_payload(payload, eng)
        for attempt in range(max_retries):
            try:
                if stream:
                    # 手动逐 token 处理 SSE
                    result = _stream_call(eng, payload, on_token, timeout=120)
                else:
                    result = _call_once(eng, payload)
                if result and result.strip():
                    return result
                last_err = RuntimeError("返回内容为空")
            except urllib.error.HTTPError as e:
                last_err = e
                detail = ""
                try:
                    detail = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
                except Exception:
                    detail = str(e)
                print(f"[llm] {eng['model']} HTTP {e.code}: {detail}", flush=True)
                if e.code in (429, 500, 502, 503) and attempt < max_retries - 1:
                    time.sleep(3 * (attempt + 1))
                else:
                    break
            except Exception as e:
                last_err = e
                print(f"[llm] {eng['model']} 异常: {type(e).__name__}: {str(e)[:120]}", flush=True)
                if attempt < max_retries - 1:
                    time.sleep(2)
                else:
                    break
        print(f"[llm] 引擎 {eng['model']} 失败，尝试下一个", flush=True)
    raise RuntimeError(f"LLM 调用失败（所有引擎）: {last_err}")


def _stream_call(eng, payload, on_token, timeout=120):
    """SSE 流式调用，逐 token 回调；空结果时回退非流式。"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        eng["url"], data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {eng['key']}",
                 "Accept": "text/event-stream"})
    full = []
    with opener.open(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk["choices"][0]["delta"].get("content", "")
                if isinstance(delta, list):
                    delta = "".join(x.get("text", "") for x in delta if isinstance(x, dict))
                if delta:
                    full.append(delta)
                    if on_token:
                        on_token(delta)
            except Exception:
                continue
    text = "".join(full).strip()
    if not text:
        # 空结果回退非流式（推理模型偶发只输出 reasoning）
        return _call_once(eng, dict(payload, stream=False))
    return text


def polish_segments(segments, rewrite_conf, on_batch=None, batch_size=10, max_workers=4):
    """分段润色（并发批处理），返回 [{"start","end","original","rewritten"}]"""
    import json as _json
    from concurrent.futures import ThreadPoolExecutor
    items = [{"id": i, "text": s["text"]} for i, s in enumerate(segments)]
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]

    def process(batch):
        # 用 replace 而非 format：提示词里含 {id, text} 等字面量，format 会误解析
        payload = BATCH_REWRITE_PROMPT.replace("{payload}", _json.dumps(batch, ensure_ascii=False))
        out = []
        try:
            raw = chat(payload, "你是口播润色助手，只输出 JSON。", rewrite_conf,
                       max_tokens=2500, temperature=0.6)
            import re
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            arr = _json.loads(m.group(0)) if m else []
            by_id = {x.get("id"): x.get("text", "") for x in arr if isinstance(x, dict)}
            for it in batch:
                out.append({"start": segments[it["id"]]["start"],
                            "end": segments[it["id"]]["end"],
                            "original": it["text"],
                            "rewritten": by_id.get(it["id"], it["text"])})
        except Exception as e:
            # 单批失败不拖垮整体：该批回退原文，其余批次继续
            print(f"[llm] 润色批失败，回退原文: {str(e)[:100]}", flush=True)
            for it in batch:
                out.append({"start": segments[it["id"]]["start"],
                            "end": segments[it["id"]]["end"],
                            "original": it["text"], "rewritten": it["text"]})
        if on_batch and out:
            on_batch(out)
        return out

    all_out = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for batch_out in ex.map(process, batches):
            all_out.extend(batch_out)
    return all_out


def analyze(full_text, rewrite_conf, stream=False, on_token=None):
    """文本层结构化分析，JSON 容错重试"""
    import re
    last_err = ""
    for attempt in range(2):
        try:
            # 推理模型会先消耗 reasoning token，预算需留足
            raw = chat(ANALYZE_PROMPT.format(full_text=full_text), "请分析", rewrite_conf,
                       max_tokens=4096, temperature=0.5, stream=stream, on_token=on_token)
            if not (raw or "").strip():
                last_err = "返回空内容"
                print(f"[llm] 分析返回空（第{attempt + 1}次）", flush=True)
            else:
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    d = json.loads(m.group(0))
                    if d.get("highlights") or d.get("summary"):
                        return d
                last_err = f"JSON 解析失败: {raw[:120]}"
                print(f"[llm] 分析 {last_err}", flush=True)
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:150]}"
            print(f"[llm] 分析异常（第{attempt + 1}次）: {last_err}", flush=True)
        time.sleep(1)
    print(f"[llm] 分析最终失败: {last_err}", flush=True)
    return {"summary": "", "scenes": "", "structure": "", "highlights": [], "improvements": [],
            "error": last_err}
