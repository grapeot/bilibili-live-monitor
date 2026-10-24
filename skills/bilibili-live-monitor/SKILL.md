---
name: bilibili-live-monitor
description: 在Apple Silicon Mac部署、运行与验证B站多路直播监控，将本地语音转写与网页弹幕合并为持久化时间线，支持静音采集、SQLite、JSONL导出和全宽实时看板。
---

# B站多路直播监控

## 定位与注册

监控主播语音与观众弹幕，将每条弹幕和每段转写各作为一个item，按事件时间混合在每房间时间线。无视频展示、视觉理解或音频回放，无付费识别API；运行消耗本机算力、内存与网络。

从目标workspace的AGENTS.md或CLAUDE.md开始，沿路由找到skill索引，只注册本根skill。主机路径、运行目录与真实房间列表放本地私有overlay。公开仓库：<https://github.com/grapeot/bilibili-live-monitor>。

## 环境与安装

需要Apple Silicon Mac、Python3.12、uv、Node.js与Chrome。主采集无需ffmpeg，历史WebM解码测试需要它。在仓库根目录执行：

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
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

默认服务8765、CDP9222、房间上限10、队列20，均可配置。30路实测先设MAX_ROOMS=30、QUEUE_SIZE=60。看板打开http://127.0.0.1:8765，添加虚构示例123456或实际授权的房间链接。重启恢复历史但不会自动重开直播。

## 采集、存储与接口

每房间独立Page，保持muted且持续播放。video.captureStream音轨经AudioContext(16000Hz)与AudioWorklet收PCM，Worklet输出置0不外放。复用该页面已有WebSocket，后端解码普通/zlib/Brotli弹幕，不另建连接。

SQLite保存完整segments、timeline和SSE事件。UI最近80条item、20段识别数据不代表存储上限。支持全宽、全屏、自动向下跟随，上翻时暂停跟随。

- GET /api/state：房间、最近记录与运行指标。
- POST /api/rooms：`{"url":"123456","chunk_seconds":30}`。
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

原始音频临时处理后删除。真实房间、用户名、转写、截图、数据库、录音与本机路径不得进入公开仓库；证据放.local/。公开示例统一使用虚构123456。

## 语音活动检测（VAD）与队列调度

系统在音频窗口进入ASR队列前执行窗口级Silero VAD（v5.1.2 ONNX）门控，不改变原有固定窗口的切分边界。

通过环境变量控制VAD行为：
- `VAD_ENABLED=1`：启用门控，设为`0`时仅保留原有静音过滤。
- `VAD_MODEL_PATH=.local/models/silero_vad.onnx`：本地模型路径。
- `VAD_THRESHOLD=0.5`：语音帧置信度阈值。
- `VAD_MIN_SPEECH_SECONDS=0.3`：窗口内最小累计语音时长。

在已激活的Python环境中执行`python scripts/prepare_vad.py`，显式下载并校验固定v5.1.2模型的SHA256。服务启动不联网；缺模型或运行期VAD故障时，状态中显示降级，原音频窗口仍送入ASR。模型校验属于准备脚本，不在每次识别时重复执行。

VAD只决定整段音频是否识别，不裁剪或拼接，窗口UTC起止保持不变，不提供词级精确时间戳。高于阈值的累计语音达到配置时长，或任一帧置信度达到0.9，或窗口边缘帧达到配置阈值，就保留整个窗口，优先避免漏掉边界短句。余数保留至下一窗；补零尾帧仅预览、不写回循环状态。VAD不是说话与演唱分类器，不额外排除歌声；演唱仍由VAD判断，召回率尚待真实样本验收，不承诺过滤所有音乐。

ASR仍由单常驻worker消费内存有界队列。队列满时丢弃最旧待处理窗口、接纳新窗口并记录`dropped`；重启不恢复任务。排队不计入推理超时；单次推理计时从向模型发送请求开始。ASR忙碌率包含worker处理、重试与模型加载等待，不代表GPU利用率。短测不代表长期稳定或单机极限容量。
