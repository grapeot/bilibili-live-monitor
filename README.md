# bilibili-live-monitor

面向 Apple Silicon Mac 的多路 B 站直播文本监控工具。

系统将每路直播的主播语音转写与观众弹幕按事件时间合并为可追溯的房间时间线，供巡检与归档使用。每条弹幕、每段转写各为一个 item，区分来源与状态。

## 核心机制与边界

- 每个直播间在独立 Chrome 页面中静音播放，通过 Web Audio `AudioWorklet` 提取独立 16kHz PCM（默认30秒窗口），交由常驻本地的 `Qwen3-ASR-1.7B`（MLX）共享串行识别。
- 监听同一网页已有的 WebSocket，解码普通、zlib 与 Brotli 弹幕包，不另建弹幕连接。
- 全宽布局，支持全屏、新消息向下跟随、上翻暂停跟随。每房间保留最近接收或更新的80条记录，再按事件时间显示；完整历史保存SQLite，可导出JSONL。
- 无视频图像理解、视频画面展示或音频回放；网页视频元素作为内部采音载体，临时音频在处理后删除。
- 无需付费识别API，但消耗本机算力、内存与网络带宽。当前不提供告警、语义摘要或多平台支持，不承诺全量弹幕零丢失。

已验证30路各至少两个30秒有效窗口，包含停顿后恢复；短测不代表无间隙或长期稳定运行。定位与边界见 [Formulation](docs/formulation.md)。

## 环境与安装

需要 Apple Silicon Mac、Python 3.12、uv、Node.js、Google Chrome。主采集不需要ffmpeg，仅历史WebM解码测试需要它。

```bash
git clone https://github.com/grapeot/bilibili-live-monitor.git
cd bilibili-live-monitor
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
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

SQLite完整存储 `segments`、`timeline` 和SSE事件，不因页面显示窗口截断。服务重启恢复历史，但不自动重开房间。数据库默认在 `DATA_DIR/monitor.sqlite3`。

- `GET /api/state`：房间、最近20段识别数据、80条时间线及运行指标。
- `POST /api/rooms`：如 `{"url":"123456","chunk_seconds":30}`。
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
