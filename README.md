# bilibili-live-monitor

面向 Apple Silicon Mac 的多路 B 站直播录制与弹幕监控工具，语音转写可选。

系统将每路直播的主播语音转写与观众弹幕按事件时间合并为可追溯的房间时间线，供巡检与归档使用。每条弹幕、每段转写各为一个 item，区分来源与状态。

为什么要在这条时间线上做面向主播的观众记忆、调研改变了哪些判断、哪些事情我们不做，见 [Motivation](docs/motivation.md)。

## 核心机制与边界

- 默认只保存原始音频与弹幕，不启动ASR、不抓缩略图。启用ASR时，`AudioWorklet` 每5秒提取16kHz PCM，交由常驻本地 `Qwen3-ASR-1.7B`（MLX）流式识别。每房间上下文独立，音频窗口最长30秒；同窗口持续修订同一行，不再独立转写30秒切片，也不按音量跳过输入。
- 监听同一网页已有的 WebSocket，解码普通、zlib 与 Brotli 弹幕包，不另建弹幕连接。
- 全宽布局，支持全屏、新消息向下跟随、上翻暂停跟随。每房间保留最近接收或更新的80条记录，再按事件时间显示；完整历史保存SQLite，可导出JSONL。
- 不做视频图像理解；可选按需缩略图。ASR临时音频处理后删除，原始录制文件独立保留。
- 无需付费识别API，但消耗本机算力、内存与网络带宽。当前不提供告警、语义摘要或多平台支持，不承诺全量弹幕零丢失。

旧独立切片实现曾验证30路；该容量结论不适用于新的流式实现。当前流式容量与长跑稳定性须重新测量。定位与边界见 [Formulation](docs/formulation.md)。

## 环境与安装

需要 Apple Silicon Mac、Python 3.12、uv、Node.js、Google Chrome 和 ffmpeg。默认录制不需要安装ASR模型；仅启用ASR时安装对应解释器环境。

```bash
git clone https://github.com/grapeot/bilibili-live-monitor.git
cd bilibili-live-monitor
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
# 以下两行仅在需要ASR时执行
uv venv --python 3.12 .venvs/asr
uv pip install --python .venvs/asr/bin/python mlx-qwen3-asr==0.4.4
npm install --prefix frontend
bash scripts/build_frontend.sh
```

参考 `.env.example` 创建本地 `.env`。已有兼容ASR环境可通过 `ASR_PYTHON` 复用；模型缓存后可设置 `HF_HUB_OFFLINE=1` 离线加载。

## 运行

自管进程组定时启动，必须将示例替换为实际未来带时区时间：

```bash
python -m bilibili_live_monitor run --until 2030-01-01T22:00:00-08:00
```

该命令直接自管Chrome、后端与ASR进程组，不依赖外部进程管理器；运行状态写入 `DATA_DIR/runtime.json`，到期清理自有组。提前结束向manifest中的owner发送SIGTERM。也可分步执行 `bash scripts/start_chrome.sh` 与 `bash scripts/start_backend.sh`。

打开 `http://127.0.0.1:8765`，输入房间号或完整链接开始监控。需要登录时在专用Chrome窗口手动完成。默认服务端口8765、CDP端口9222、房间上限10、队列容量20，均可配置。30路实测使用 `MAX_ROOMS=30`、`QUEUE_SIZE=60`，不是公开默认值。

Tailscale内访问可使用 `tailscale serve --bg --https=8447 http://127.0.0.1:8765`，以该命令返回的tailnet URL为准。

## 存储与接口

直播中或结束后可运行 `bili-live trends build --db <monitor.sqlite3> --room 123456 --since '2030-01-01T10:00:00+08:00' --output <新的私有目录>`，生成入场流量、送礼频次、关注总数与粉丝团人数四图。数据只读、输出冻结、不影响录制；`bili-live trends verify --output <目录>` 可脱离源库复核。完整参数、隐私边界和统计口径见 [趋势分析子skill](skills/bilibili-live-monitor/trends.md)。

### 独立的录制、ASR和缩略图选项

新增房间表单与 `POST /api/rooms` 支持三个独立选项：
- `record_mode`：`none` / `audio` / `video`，其中 `video` 包含音频，默认 `audio`。
- `asr_enabled`：默认 `false`；关闭时不启动模型、不创建识别PCM窗口或队列，前端显示禁用。
- `preview_enabled`：默认 `false`；仅在启用、卡片可见且页面前台时请求缩略图。

环境默认是 `RECORD_MODE=audio`、`ASR_ENABLED=0`、`PREVIEW_ENABLED=0`、`RECORDING_SEGMENT_SECONDS=300`。
默认保存并渲染弹幕。录制位于 `DATA_DIR/recordings/<room_id>/`：`audio` 将网页实际播放的16kHz、16bit单声道 PCM 分段保存为 `.wav`，与实时 ASR 共享采集，不额外请求媒体下载地址；关闭 ASR 仍会播放并采集音频。`video` 由 FFmpeg 复制原始音视频压缩包到 `.mkv`；仅此模式在 ASR 和缩略图关闭时暂停网页视频。每次监控会话独立统计，正常停止会保留在途音频、排空缓冲并收尾文件。

当ASR与缩略图均关闭时，原始录制不再等待HTML `video`就绪或调用 `play`。初次导航和重连均以网页成功响应捕获的媒体地址为录制输入，媒体发现阶段名义等待上限30秒，保留弹幕监听。启用ASR或缩略图时仍要求网页播放器就绪；导航或重载收到403/412/429则立即停止该源。

```json
{"url":"123456","record_mode":"audio","asr_enabled":false,"preview_enabled":false}
```

SQLite完整存储 `segments`、`timeline` 和SSE事件，不因页面显示窗口截断。服务重启恢复历史，但不自动重开房间。数据库默认在 `DATA_DIR/monitor.sqlite3`。

- `GET /api/state`：房间、最近20段识别数据、80条时间线及运行指标。
- `POST /api/rooms`：如 `{"url":"123456","asr_enabled":true}`。旧`chunk_seconds`参数兼容接收，但实时流式输入固定5秒。

断流恢复默认等待2、4、8秒逐步翻倍，上限300秒；达到上限后每5分钟重试，连续20次失败后明确停止自动重试。连续有效采集60秒才清零，短暂恢复不重置预算。`MAX_RECONNECTS`、`RETRY_DELAY`、`RETRY_MAX_DELAY`可配置；明确接口拒绝仍需人工处理。浏览器保留一张空白保活页，避免关闭最后一个直播页后连查播上下文也消失。
- `POST /api/rooms/123456/stop`：停止并取消本房间排队与识别任务。
- `GET /api/events`：SSE，支持Last-Event-ID重连。
- `GET /api/export`：完整混合时间线JSONL，可加 `?room_id=123456`。
- `/docs`、`/openapi.json`：接口定义。

## 验证

```bash
pytest
ruff check src tests scripts
npm run build --prefix frontend
python scripts/verify_isolation.py
python scripts/live_smoke.py --rooms 30 --segments 2 --wait-seconds 180 --output .local/result.json
```

真实直播测试须先启动Chrome与后端并配置足够的房间上限，会消耗本机算力与网络。离线测试、构建和实测结论分别记录于 [测试说明](docs/test.md) 与 [工作记录](docs/working.md)。

公开仓库不包含真实房间列表、用户名、转写、录音、截图或本机路径。运行数据只放本地 `.local/`。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
