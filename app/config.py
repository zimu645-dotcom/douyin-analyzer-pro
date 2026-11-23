# -*- coding: utf-8 -*-
"""统一配置管理：app_config.json 持久化，前端设置面板可改。"""
import os
import json

from paths import BASE
OUTPUT_DIR = os.path.join(BASE, "data")
CONFIG_FILE = os.path.join(OUTPUT_DIR, "config.json")

DEFAULT_CONFIG = {
    "rewrite": {
        "primary": {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-flash",
            "key": "",
            "reasoning": "off",     # off=关闭思考模式，流式输出立刻可见
        },
        "fallback": {
            "provider": "mimo",
            "base_url": "https://api.xiaomimimo.com/v1/chat/completions",
            "model": "mimo-v2.6-flash",
            "key": "",
        },
    },
    "vision": {
        "provider": "mimo",
        "base_url": "https://api.xiaomimimo.com/v1/chat/completions",
        "model": "mimo-v2.6-flash",
        "key": "",
        "fps": 1,
        "chunk_sec": 120,     # 分段长度（秒）：越长段数越少越快；实测 120 最优
        "parallel": 3,        # 分段并发：MiMo 并发上限约 3，再高会被排队反而变慢
        "enabled": True,
    },
    "asr": {
        "base_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
        "model": "FunAudioLLM/SenseVoiceSmall",
        "key": "",
    },
    "feishu": {
        "app_id": "",
        "app_secret": "",
        "feishu_url": "",
        "app_token": "",
        "table_id": "",
        "auto_export": False,
    },
    "remember_key": True,
}

os.makedirs(OUTPUT_DIR, exist_ok=True)


def load():
    try:
        if os.path.exists(CONFIG_FILE):
            cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
            merged = json.loads(json.dumps(DEFAULT_CONFIG))
            for k, v in cfg.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
            return merged
    except Exception:
        pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def public():
    """返回完整配置（含密钥明文，方便用户查看和管理）"""
    import copy
    return copy.deepcopy(load())
