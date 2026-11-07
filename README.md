# bilibili-live-monitor

面向 Apple Silicon Mac 的多路 B 站直播记录、单场跟进与跨场归档工具。

系统将主播语音转写、观众弹幕、礼物和点赞通知按事件时间组织为可追溯的房间时间线。基础采集之外，可按需启用单场分析、历史场次与歌曲归档、观众备忘及授权互动。代码具备某项能力不代表每个房间或部署都已启用。

为什么要在这条时间线上构建面向主播的观众记忆与实时转化副驾、漏斗各层如何推进引流与营收转化，见 [Motivation](docs/motivation.md)。

## 按任务找到入口

| 要完成的任务 | 现有能力与操作入口 |
|---|---|
| 开始盯房间并保留记录 | 添加/停止房间，默认音频录制、弹幕与礼物时间线、JSONL导出；视频、ASR、缩略图可独立开启，见下方安装与运行 |
| 长期守候和查看多个实例 | 候选轮转或固定名单、账号/匿名实例隔离、统一看板，见[平台运维](skills/bilibili-live-monitor/platform-operations.md)与[网关示例](docs/dashboard.example.json) |
| 在播跟进，下播继续看结果 | 可选四图、活动分类、歌名登记与点歌跟进，见[单场看板](skills/bilibili-live-monitor/live-share.md)；自动建场、离线转写与终审默认关闭，见[逐场工作流](skills/bilibili-live-monitor/session-workflow.md) |
| 跨场回看场次、歌曲和观众 | 整场汇总、自然日入口、曲库与歌曲礼物/关注分析、[观众备忘](skills/bilibili-live-monitor/session-workflow.md#观众当前备忘与下播更新)；这些产物按配置生成和发布 |
| 授权互动与助手操作 | 欢迎、点赞、账号切换见[持久自动化](docs/persistent_automation.md)；已有助手卡片修改、撤销、注音与AI控制见[专项设计](docs/live_assistant_design.md)，不代表主播已实际使用 |
| 处理已有素材 | [离线ASR](skills/bilibili-live-monitor/offline-asr.md)、[趋势](skills/bilibili-live-monitor/trends.md)、[活动时间轴](skills/bilibili-live-monitor/activity.md)、[预告片](skills/bilibili-live-monitor/trailer.md)、[录像归档](skills/bilibili-live-monitor/archive.md) |

[PRD](docs/prd.md)说明用户任务与能力边界，[RFC](docs/rfc.md)说明当前实现与数据契约，[测试说明](docs/test.md)说明验证和数字差异的判定。AI操作者从[根skill](skills/bilibili-live-monitor/SKILL.md)按任务加载操作手册；具体运行配置与证据留在本地。

## 核心机制与边界

- 默认只保存原始音频与弹幕，不启动ASR、不抓缩略图。启用ASR时，`AudioWorklet` 每5秒提取16kHz PCM，交由常驻本地 `Qwen3-ASR-1.7B`（MLX）流式识别。每房间上下文独立，音频窗口最长30秒；同窗口持续修订同一行，不再独立转写30秒切片，也不按音量跳过输入。
- 监听同一网页已有的 WebSocket，解码普通、zlib 与 Brotli 弹幕包，不另建弹幕连接。
- 全宽布局，支持全屏、新消息向下跟随、上翻暂停跟随。每房间保留最近接收或更新的80条记录，再按事件时间显示；完整历史保存SQLite，可导出JSONL。
- 不做视频图像理解；可选按需缩略图。ASR临时音频处理后删除，原始录制文件独立保留。
- 本地ASR无需付费识别API，但消耗本机算力、内存与网络带宽。可选AI复核与观众备忘使用各自配置的模型，可能产生外部调用费用。现有通知与专项分析不等于通用关键词告警或任意语义问答；不承诺多平台支持或全量弹幕零丢失。

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

多实例看板与按日归档可参考 [示例配置](docs/dashboard.example.json)，把真实配置另存到本地 `.local/`，再运行 `python -m bilibili_live_monitor.dashboard_gateway --config .local/dashboard.json --port 8767`。房间卡提供直播间与按日汇总两个入口；公开仓库只包含代码和占位示例，不包含运行中的看板页面、真实配置或统计数据。

## 存储与接口

直播中或结束后可运行 `bili-live trends build --db <monitor.sqlite3> --room 123456 --since '2030-01-01T10:00:00+08:00' --output <新的私有目录>`，生成入场流量、送礼频次、关注总数与粉丝团人数四图。数据只读、输出冻结、不影响录制；`bili-live trends verify --output <目录>` 可脱离源库复核。完整参数、隐私边界和统计口径见 [趋势分析子skill](skills/bilibili-live-monitor/trends.md)。

### 独立的录制、ASR和缩略图选项

新增房间表单与 `POST /api/rooms` 支持三个独立选项：
- `record_mode`：`none` / `audio` / `video`，其中 `video` 包含音频，默认 `audio`。
- `asr_enabled`：默认 `false`；关闭时不启动模型、不创建识别PCM窗口或队列，前端显示禁用。
- `preview_enabled`：默认 `false`；仅在启用、卡片可见且页面前台时请求缩略图。

环境默认是 `RECORD_MODE=audio`、`ASR_ENABLED=0`、`PREVIEW_ENABLED=0`、`RECORDING_SEGMENT_SECONDS=300`。
默认保存并渲染弹幕。录制位于 `DATA_DIR/recordings/<room_id>/`：`audio` 将网页实际播放的16kHz、16bit单声道 PCM 分段保存为 `.wav`，与实时 ASR 共享采集，不额外请求媒体下载地址；关闭 ASR 仍会播放并采集音频。`video` 由 FFmpeg 复制原始音视频压缩包到 `.mkv`；仅此模式在 ASR 和缩略图关闭时暂停网页视频。每次监控会话独立统计，正常停止会保留在途音频、排空缓冲并收尾文件。

`audio`模式依赖网页PCM，即使ASR与缩略图关闭也要等待播放器并保持播放。`video`模式且ASR与缩略图均关闭时，不等待HTML `video`播放，使用网页成功响应捕获的媒体地址交给FFmpeg录制，并暂停网页视频以降低渲染；`none`模式不生成媒体文件。启用ASR或缩略图时仍要求播放器就绪。导航或重载收到403/412/429则停止该源。

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
npm test --prefix frontend
npm run build --prefix frontend
python scripts/verify_isolation.py
python scripts/live_smoke.py --rooms 30 --segments 2 --wait-seconds 180 --output .local/result.json
```

真实直播测试须先启动Chrome与后端并配置足够的房间上限，会消耗本机算力与网络。离线测试、构建和实测结论分别记录于 [测试说明](docs/test.md) 与 [工作记录](docs/working.md)。

公开仓库不包含真实房间列表、用户名、转写、录音、截图或本机路径。运行数据只放本地 `.local/`。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
