# 🎬 抖音视频拆解分析系统 Pro

全新重构版本：粘贴抖音链接 → 无头下载音频 → 云端转写 → AI 润色/分析 → 可选画面理解 → 飞书/Excel/Markdown 导出。

## 特性

- **纯云端转写**：硅基流动 SenseVoiceSmall，免费、秒级、无本地模型
- **整段视频理解**：MiMo 视觉按 fps 抽帧理解完整视频（URL 方式），支持流式输出
- **极速下载**：无头浏览器直抓音频流（约 4MB），不下载整段视频，浏览器跨任务复用
- **全链路流式**：转写逐句、润色按批、分析/画面逐 token，实时推送
- **多账号**：扫码登录长期保存，可切换
- **批量**：主页并行预下载 + 顺序处理，单视频失败不拖垮整批
- **导出**：飞书多维表格（自动建字段）、Excel、Markdown

## 模块

| 引擎 | 用途 |
|------|------|
| DeepSeek（主）/ MiMo（兜底） | 润色、结构化分析 |
| 硅基流动 SenseVoiceSmall | 语音转写 |
| MiMo mimo-v2.6-flash | 画面理解（URL 方式，fps 可调） |
| 飞书多维表格 | 结果导出 |

## 快速开始

1. `pip install -r requirements.txt && python -m playwright install chromium`
2. 双击 `login.bat` 扫码登录抖音（登录态长期保存）
3. 双击 `start.bat`，浏览器打开 http://127.0.0.1:8321
4. 设置里填入各引擎 API Key（DeepSeek / MiMo / 硅基流动 / 飞书）

## 结构

```
app/
├── main.py            # FastAPI 入口 + API
├── config.py          # 配置（data/config.json）
├── accounts.py        # 多账号
├── tasks.py           # 任务运行器（worker 子进程）
├── worker_main.py     # worker 入口
├── login_worker.py    # 扫码登录子进程
├── browser.py         # 无头浏览器管理
├── core/
│   ├── douyin.py      # 抓音频/视频流、作者清单
│   ├── asr.py         # 硅基流动转写
│   ├── llm.py         # DeepSeek/MiMo 改写分析
│   ├── vision.py      # MiMo 画面分析（飞书托管 URL）
│   ├── feishu.py      # 飞书客户端 + 导出
│   ├── stats.py       # 互动数据
│   └── exporters.py   # Excel / Markdown
└── web/index.html     # 前端（深色主题）
```

## 数据目录

`data/`（config.json、reports/、history.json、audio/ 等运行时产物，已 gitignore）

仅供学习交流使用。
