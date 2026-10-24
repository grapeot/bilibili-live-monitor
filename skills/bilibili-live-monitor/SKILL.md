---
name: bilibili-live-monitor
description: 在Apple Silicon Mac运行B站常驻录制平台，通过AI控制面管理目标与来源，将原始媒体、本地语音转写与网页弹幕持久保存；包含面向AI的平台运维子skill。
---

# B站多路直播监控

## 定位与注册

默认保存原始音频与弹幕，可独立启用ASR、音视频保存和按需缩略图。每条弹幕和每段转写各作为一个item，按事件时间混合在每房间时间线。不做视觉理解，无付费识别API；运行消耗本机算力、内存与网络。

从目标workspace的AGENTS.md或CLAUDE.md开始，沿路由找到skill索引，只注册本根skill。主机路径、运行目录与真实房间列表放本地私有overlay。公开仓库：<https://github.com/grapeot/bilibili-live-monitor>。

## AI 操作者入口

常驻目标、重启恢复、健康摘要与AI异步补位见 [平台运维子skill](platform-operations.md)。只注册本根skill，子skill按任务加载；真实路径、端口、模型和运行状态从本机私有overlay读取。

已有录制素材的盘点、可恢复本地转写与文本/能量判读边界见 [离线ASR子skill](offline-asr.md)。

## 环境与安装

需要Apple Silicon Mac、Python3.12、uv、Node.js、Chrome与ffmpeg。在仓库根目录执行：

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
# 以下两行仅在需要ASR时执行
uv venv --python 3.12 .venvs/asr
uv pip install --python .venvs/asr/bin/python mlx-qwen3-asr==0.4.4
npm install --prefix frontend
bash scripts/build_frontend.sh
```

参考.env.example创建本地.env。已有ASR环境通过ASR_PYTHON复用，模型缓存后可设HF_HUB_OFFLINE=1。

## 启动与管理

```bash
python -m bilibili_live_monitor run --until 2030-01-01T22:00:00-08:00
```

将截止时间换成实际未来带时区时间。该控制进程直接拥有Chrome、后端和ASR进程组，不使用外部Background Process Manager；DATA_DIR/runtime.json记录所有权，到期清理自有组。提前结束向manifest中的owner发送SIGTERM。也支持scripts/start_chrome.sh和scripts/start_backend.sh分步启动。

默认服务8765、CDP9222、房间上限10、队列20，均可配置。30路实测先设MAX_ROOMS=30、QUEUE_SIZE=60。看板打开http://127.0.0.1:8765，添加虚构示例123456或实际授权的房间链接。临时房间重启后不自动重开；平台候选与policy目标持久化并自动恢复。

## 采集、存储与接口

默认RECORD_MODE=audio、ASR_ENABLED=0、PREVIEW_ENABLED=0，新增房间可覆盖record_mode/asr_enabled/preview_enabled三个独立字段。ASR关闭时不加载模型、不创建识别PCM窗口或队列；看板显示禁用。ffmpeg复制原始压缩包分段写入DATA_DIR/recordings/<room_id>/，音频.mka、音视频.mkv。默认300秒分段，RECORDING_SEGMENT_SECONDS可配置。

每房间独立Page并静音。仅启用ASR时，video.captureStream音轨经AudioContext(16000Hz)与AudioWorklet收PCM，Worklet输出置0不外放。ASR与缩略图都关闭时暂停网页视频，后台录制独立进行。复用网页已有WebSocket采集弹幕，不另建连接。缩略图仅显式启用、卡片可见且页面前台时请求。

SQLite保存完整segments、timeline和SSE事件。UI最近80条item、20段识别数据不代表存储上限。支持全宽、全屏、自动向下跟随，上翻时暂停跟随。

- GET /api/state：房间、最近记录与运行指标。
- POST /api/rooms：`{"url":"123456","chunk_seconds":30,"record_mode":"audio","asr_enabled":false,"preview_enabled":false}`。
- POST /api/rooms/123456/stop：停止并清除本房间排队任务，取消inflight，迟到结果校验generation。
- GET /api/events：SSE，支持Last-Event-ID。
- GET /api/export：混合时间线JSONL，可加?room_id=123456。
- /docs与/openapi.json：接口定义。

## 验证与失败表达

```bash
pytest
ruff check src tests scripts
npm run build --prefix frontend
python scripts/verify_isolation.py
python scripts/live_smoke.py --rooms 30 --segments 2 --wait-seconds 180 --output .local/result.json
```

真实直播测试须显式启动，使用本机算力与网络；失败记录不能覆盖或改记为通过。--resume可沿用同次运行序号基线继续等待，输出另存以保留前一份证据。

有效窗口要求源音轨live/enabled/非mute、覆盖率90%～120%、媒体进度至少墙钟80%、缺输入≤1600样本、源mute≤100ms、无缓冲丢样。低RMS不等于语音检测；游戏和音乐仍可能误识别。队列过载显式记录，默认连续重连5次、有效采集后归零；ASR进程/通信失败同段重启重试一次。

30路各两个有效窗口已短测验证，包含恢复，不保证无间隙、弹幕全量或长期稳定。当前无告警、摘要、多平台或跨平台ASR。

## 数据边界

ASR临时音频处理后删除；原始录制文件按保存选项独立保留。真实房间、用户名、转写、截图、数据库、录音与本机路径不得进入公开仓库；证据放.local/。公开示例统一使用虚构123456。
