# bilibili-live-monitor

## 项目定位与工作语言
- B站多直播间记录、单场跟进与跨场归档工具，包含可选实时转写与授权互动；公开GitHub仓库。
- 默认工作语言：中文，覆盖文档、注释、UI 和变更记录。

## 目录
- `src/bilibili_live_monitor/`：FastAPI、CDP 采集、ASR 桥接、SQLite。
- `frontend/`：React + Vite + TypeScript。
- `scripts/`：启动与构建入口，均从项目根目录执行。
- `skills/bilibili-live-monitor/SKILL.md`：唯一根 skill。
- `docs/`：`prd.md`维护用户任务与能力，`rfc.md`维护当前架构和数据契约，`test.md`维护实用验收方法，`working.md`记录变更与验证；`tests/`为离线测试。
- `.local/`：运行数据与验收证据，禁止提交。

## 环境与维护
- Python 3.12，用 `uv venv`、激活环境、`uv pip install -e '.[dev]'`。
- ASR 解释器由 `ASR_PYTHON` 指定，可复用已有 `mlx-qwen3-asr==0.4.4` 环境。
- 修改功能后更新 `docs/working.md`，不虚构测试或长跑结果。
- 公开文件不放私有路径、账号、Cookie、音频、转写或抓包。示例用虚构房间号。
- 用户显式要求后才 commit/push；只暂存指定文件，不用 `git add .` 或 `git add -A`。

## 验证
`pytest`、`ruff check src tests`、`npm run build --prefix frontend`。真实直播验收见根 skill，证据放 `.local/`。
