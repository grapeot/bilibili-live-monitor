# 本地复核歌词库查证与登记指南

本技能为 B 站直播监控的歌词查证扩展，定义本地复核歌词库的访问规范、数据契约与登记流程。

## 1. 原则与配置路径

- **优先本地查证**：AI 复核歌名与歌词时优先使用本地复核歌词库。已有匹配项直接复用，不再例行联网搜索，避免网络异字覆盖本地已整理文本；仅在本地未命中、信息残缺或存在冲突时联网补查。
- **库路径与覆盖**：全局默认路径为 `~/.local/share/bilibili-live-monitor/lyrics/current`，可通过单场配置中的 `config.lyric_library` 指定绝对路径覆盖。若目录或 `index.jsonl` 不存在，行为与原有流程完全一致。
- **自主检索**：Agent 自主决定检索与阅读范围，程序不预选候选亦不拼装歌词上下文。可通过 `rg --fixed-strings -- '歌名' /path/to/library/index.jsonl` 查询索引，或用短歌词在 `songs/` 目录检索候选文件后自主阅读前后文。

## 2. 状态分级与使用约束

库内条目按三种状态严格分级处理：
- `ready`：已按实用标准整理复核，作为优先参考，允许少量用字差异。
- `partial`：歌词仅为部分收录，仅支持已包含唱词的比对，不能假定全曲完整。
- `missing`：仅收录歌名线索，无有效歌词，严禁据此声称已有歌词比对依据。

歌词匹配仅作文本辅助，不能单独作为现场真唱、本人原声或点歌已兑现的证明，严禁全量替换 ASR 原始文本。

## 3. 导出与登记复用

- **不可变导出**：通过 CLI 导出不可变只读版本，路径使用绝对路径占位符：
  `python -m bilibili_live_monitor.local_lyrics --catalog /path/to/catalog.json --output /path/to/export_dir`
  输出包含不可变清单、`index.jsonl`、`songs/*.txt` 及 `sources/*.json`。
- **来源登记复用**：`sources/<work_id>.json` 封装了标准来源信封，可直接复用现有命令：
  `bili-live live-share register-song --config /path/to/config.json --title '准确歌名' --source /path/to/sources/WORK_ID.json --result-index 0`
- **终审收敛分工**：终审拆分子任务只在各自结果中返回作品 ID 与来源路径，不直接执行登记，由总协调任务统一执行登记与合并。

## 4. 边界说明

歌词库及快照属于私有数据，严禁将歌词正文提交至公开仓库；本扩展仅补充文件线索与 Prompt 接线，不重建 Agent 结构或外部调度体系。
