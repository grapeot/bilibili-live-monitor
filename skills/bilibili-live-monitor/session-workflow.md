# 固定监控的自动逐场工作流

## 目标与一次配置

在固定房间持久监控基础上，通过 `session_workflow` 自动完成每场看板的建立、周期更新和离线收尾。规范房间 ID 与北京时间 `live_start` 标识同一场直播；每场拥有独立私有目录和随机 URL，同场重启沿用原 URL，新场生成新 URL。正常运行无需逐场执行 `init`。代码交付本身不表示目标机器已经配置或启用。

在现有 Automation 配置的 `rooms`、`notification_command` 旁增加 `session_workflow`。未配置或 `enabled:false` 时不启用。以下均为虚构示例；实际主播、站点映射、邮箱与机器路径仅留私有配置。

```json
{
  "rooms": {
    "123456": {
      "label": "示例主播",
      "intro": "欢迎听歌",
      "local_only": false,
      "notification_command": ["/PRIVATE/notify-adapter"],
      "followup_rules": {"blind_gift_names": ["示例盲盒"]}
    }
  },
  "notification_command": [
    "/PRIVATE/monitor/.venv/bin/python", "-m",
    "bilibili_live_monitor.broadcast_email", "--env-file", "/PRIVATE/notify.env"
  ],
  "session_workflow": {
    "enabled": true,
    "workspace_root": "/PRIVATE/sessions",
    "opencode_python": "/PRIVATE/opencode/.venv/bin/python",
    "opencode_cwd": "/PRIVATE/opencode",
    "opencode_project": "/PRIVATE/workspace",
    "model": "provider/model",
    "final_model": "provider/final-model",
    "api": "http://127.0.0.1:8765",
    "launcher": "http://127.0.0.1:7997",
    "max_hours": 12,
    "seal_seconds": 300,
    "finish_timeout": 21600,
    "final_review_batches": 40,
    "finalize_parallel": true,
    "final_concurrency": 10,
    "final_subagent": "general",
    "publish": {
      "default": {"remote": "HOST:/share/", "url": "https://example.org/share/"},
      "rooms": {
        "123456": {"remote": "HOST:/artist/", "url": "https://artist.example/s/"}
      }
    }
  }
}
```

`publish.default` 是其他房间的发布前缀，`publish.rooms` 覆盖指定房间；各场随机路径追加在这些前缀下。`workspace_root`、两个 OpenCode 目录和解释器均为绝对路径；虚拟环境解释器不要解析符号链接。`max_hours` 默认及上限为12；封口等待默认300秒，收尾预算默认21600秒，终审最多40批。`followup_rules` 沿用[实时看板](live-share.md)中的字段与含义。

监控与 Launcher 使用已安装本版本包的解释器。Automation 自动启动以下单场子进程，并按 label 检查、恢复；不为每场另建 cron，也不在已有单场工作区额外运行旧发布器：

```bash
python -m bilibili_live_monitor.session_runner --config /PRIVATE/sessions/SESSION/config.json
```

## 观众当前备忘与下播更新

观众当前备忘（Audience CRM）按房间与观众 UID 聚合已完成场次记录，程序负责计数与金额汇总，指定查证 agent 负责生成带逐字引用的当前备忘；私有原始证据保留在本地，公开构建只输出最小投影。

### 配置文件与隐私边界

每位主播使用独立配置文件、独立输出目录和独立随机 URL。随机 URL 仅用于路径隔离，不充当鉴权机制。配置文件放在 `.local/` 下（如 `.local/example-crm.json`），主要字段包括：

- 基础与输出配置：`room_id`、`scope`、`secret`、`label`、`workspace`、`output_dir`、`home_url`。其中 `secret` 用于生成公开 HMAC ID，使用私有随机密钥，不提交入库。
- 数据源配置：`database` 与 `sessions_root`，可选接入 `legacy_sessions` 与 `analysis_files`。

公开仓库只保留通用代码与合成测试，真实资料、私有配置与原始逐字证据全部留在 `.local/`。

### 分步生成与语义核验

首次建档或按场次补跑时，按冻结输入、模型生成与核查、接纳、构建四步执行：

```bash
# 1. 只读扫描已完成场次，冻结按人拆分的 input/result/plan/instructions
python -m bilibili_live_monitor.audience_crm prepare --config .local/example-crm.json

# 2. 校验单个观众的生成结果结构与引用锚点
python -m bilibili_live_monitor.audience_crm validate \
  --config .local/example-crm.json \
  --run .local/example-run \
  --result .local/example-run/example.result.json

# 3. 全量任务通过后接纳并切换 current，拒绝旧完成结果回退新版本
python -m bilibili_live_monitor.audience_crm finish \
  --config .local/example-crm.json \
  --run .local/example-run

# 4. 根据当前有效状态执行本地静态构建，不触发远端发布
python -m bilibili_live_monitor.audience_crm build --config .local/example-crm.json
```

在 `prepare` 与 `finish` 之间，针对 `plan.tasks` 中每个任务的 `input` 与 `result` 路径，调用指定 agent 生成备忘（运行前须实核底层模型映射），并发上限为 10。自动收尾读取 CRM 配置的 `author_agent`；未指定时，整场查证模式默认 `grok`，旧局部模式保留 `ollama_glm_5_3_flash`。可选 `coordinator_model`（`provider/model`）仅覆盖 CRM 总协调模型，不改变直播采集或原场终审配置。指定 agent 不可用时报告阻塞，不静默切换。

设置 `author_agent: "cursor_grok_cli"` 时，OpenCode保留指定总协调模型，按人查证改由本机Launcher托管的Cursor CLI执行，固定`grok-4.7-high`、最多十并发、单片45分钟；此值是执行后端标识，不是OpenCode task名称。总协调按`plan.tasks`列表位置以`000`起编号，调用`python -m bilibili_live_monitor.cursor_finalize start --config SESSION_CONFIG --kind audience_finalize --shard 000`。单片只写候选，总协调语义审核后将`start`改为`accept`，程序按原CRM规则核对身份、版本与真实引文后转正；明确失败且确认无旧写入者才用`retry`，最多两次、优先同CLI session且模型不变。`reused=true`保留原结果；旧结果无CLI回执时记录`legacy_result`，不伪造Cursor成功。全部实际退出并验收后仍由总协调执行原`audience_crm finish`，不把JSON合法当作查证完成。

`validate` 通过仅代表 JSON 结构与引用字段合法，不代表语义通过。接纳前核对本人归属、ASR 近音、跨场履约、原唱与主播演唱的区别，以及公开引用的必要性。局部召回没有后续记录，不等于承诺未兑现；同一入团送歌与具体点歌按同一履约链处理。

### Cursor CLI 终审分片

音乐与杂谈终审可设置 `final_subagent: "cursor_cli_grok"`，通过本机 Launcher 托管 Cursor CLI 分片。分片固定使用 `grok-4.7-high`，总协调由显式 `final_model: "provider/model"` 独立选择，不继承实时 `model`。必须启用 `finalize_parallel`，并发上限为十；总协调审核候选并执行 `accept` 后，继续各自的合并、来源登记与完成验收。`final_model` 只控制 OpenCode 总协调，不改变 Cursor 分片模型。热加载默认值只影响后续初始化的场次，已有场次保留其冻结配置。

### 整场文件式查证（可选）

观众 CRM 配置支持可选字段 `agentic_sources: true`（默认关闭）。开启后，程序继续负责房间与观众 UID 隔离、互动与礼物计数、整场来源冻结、引用逐字校验和版本原子接纳；每位观众的 `.input.json` 保留本人互动记录、既有备忘与 `source_manifest` 路径。本房间各场次的整场转写（包含原稿、有效校正版与在线对照稿）、同期互动事件、歌曲时间线及已存歌词作为共享私有文件写入运行目录 `sources/`，由清单统一索引。具备文件读取工具的查证 agent 自主按需查阅整场文件，不再局限于观众发言前 3 分钟、后 2 分钟的候选窗口。观众输入、共享来源与生成 prompt 摘要共同纳入版本计算，prompt 或来源变化后不会复用旧结果。加载与冻结阶段核对房间及场次身份；`validate` 与 `finish` 对冻结来源做只读校验，遇到文件缺失或字节变化直接报错，不补写缺失文件。

在现有私有 CRM 配置中增加：

```json
{
  "agentic_sources": true,
  "legacy_sessions": [
    {
      "id": "session-example-1",
      "room_id": "123456",
      "label": "示例历史场次",
      "url": "https://artist.example/s/live-example-1/",
      "start": "2026-11-01T12:00:00Z",
      "end": "2026-11-01T15:00:00Z",
      "evidence_workspace": "/PRIVATE/sessions/session-example-1",
      "offline_transcript": "/PRIVATE/sessions/session-example-1/offline.jsonl"
    }
  ]
}
```

历史场次可按实际情况提供 `evidence_workspace` 与 `offline_transcript`；不要覆盖原有 `legacy_sessions` 列表。命令行仍沿用 `prepare`、`validate`、`finish`、`build` 四个入口。已有 `.result.json` 通过结构与引用校验只代表格式及锚点合法，不代表语义已审阅；更新查证规则后须实际重跑核验。启用该模式时，先选取真实小样本生成并完成独立 review，确认口径后再推全量；试跑期间可暂停外部周期调度，暂停操作不修改原直播录制与通知账本。

普通点歌查证优先对照既有歌曲时间线、已存歌词与原始 ASR，先确认目标曲目在后续是否实际出现。主播慢读弹幕或点歌队列等待属于正常延迟，不能据此认作拒绝，也不要求每次都有口头接单或点名；同曲多人点歌或主播明显自行选歌等冲突线索并存时，保留归属不确定。普通点歌后续出现对应曲目且归属合理即可记为已响应，现场演唱与播放未区分不等于未响应；只有观众明确提出亲唱、学会后补唱或完整演唱时，才按对应承诺条件核对。单纯提到歌名不算歌曲出现，歌曲表未收录也不证明未播；歌词文件与依据该歌词修过的 ASR 同源，不能当作相互独立的双证据。两首证据强度或归属确定度不同的曲目不合并为同一个 `fulfilled` 条目；剩余未知如实记录，不转化为主播必做补唱。公开页面只输出简短结论和必要引用，原始转写与共享来源文件不公开。

### 下播自动更新与独立观察器

存在两种下播更新接入方式，命令行不设独立的 `publish` 子命令：

1. **逐场 Finalize 联动**：在逐场配置中设置 `audience_crm_config` 路径，或在 `session_workflow` 设置的 `audience_crm` 下按房间映射配置。CRM 配置的 `publish_on_finalize` 为显式发布开关，开启时需确保 `remote` 与 `url` 匹配部署范围。
2. **独立 `audience_watch` 观察器**：在不重启常驻采集进程时，可单独运行观察器扫描新完成的场次：

```bash
python -m bilibili_live_monitor.audience_watch --config .local/example-watch.json
```

观察器配置包含 `workspace`、`targets`（每项含 `config` 绝对 CRM 配置路径与 `not_before` 带时区起始时刻）及可选 `timeout_seconds`（默认 `21600`）。

观察器对原直播 workflow 保持只读，使用独立状态与回执，不修改原 workflow 状态与通知账本，每次运行至多推进一个活跃的 completed 场次。上一轮子会话树尚未确认停止时不推进新工作；attention 回执需核查，不能对结果不确定的提交盲目重发。外部调度器的周期调用单独启用，适配器代码落地不代表自动调度已在运行。

## 通知适配器

### 单房内容模式与录制策略

`rooms.<id>.content_mode` 支持 `music`（默认）与 `talk`。`music` 维持原唱歌流程；`talk` 不创建或调度内容 AI、不查歌名、不做歌曲 AI 终审，保留实时 ASR 与程序每分钟统计发布。下播后封口已保存录音，执行离线 ASR，再转最终静态页。未分析的 ASR 不强行标成已复核杂谈，页面隐藏点歌跟进和唱歌杂谈分段。

`rooms.<id>.record_mode` 支持 `none`、`audio`、`video`。未指定时继承平台 policy；显式配置只覆盖当前房间。`audio` 将网页播放的16kHz、16bit单声道音频分段保存为 WAV，与实时 ASR 共享采集，不另请求媒体下载地址；关闭 ASR 仍保存音频。`video` 沿用独立 FFmpeg 压缩流录制，`none` 不保存媒体。`talk` 可以选择任一录制模式，欢迎开关保持独立。

所有活动场次固定每300秒运行一次 `session_health` 程序巡检，没有可关闭的房间开关。巡检检查录音/录像增长、已启用的 ASR、页面更新及已启用的内容复核，结果保存在私有 `health.json` 与 `health_history.jsonl`。仅对状态新鲜、同一场、未受平台拒绝的错误或停止采集执行有限恢复，30分钟最多两次；不重启共享服务。调度失败保留本轮结果，runner 后续补链；completed/attention 停止本场巡检。程序巡检不调用内容 AI。

需要完成离线收尾的`talk`房间应至少保存`audio`；`none`只能实时采音，没有素材时离线收尾将保留错误而非伪装完成。

新场次从房间配置读取 `content_mode`。运行中将房间改成 `talk` 并 reload 后，场次管理器同步本场配置，runner 停止旧内容 AI 后继续发布；恢复 `music` 从下一新场生效。已完成场次不回写模式。

### 自动化配置热加载与动态加房

新增房间的欢迎弹幕默认关闭，`rooms.<id>.welcome_enabled` 为严格布尔值，默认 `false`；已有持久化界面开关在重启时保留。

`POST /api/automation/reload` 无请求体，从启动时指定的 `AUTOMATION_CONFIG` 文件读取配置，校验全部成功后执行热加载。支持追加或更新 `rooms`，以及 `session_workflow` 的 `publish`、`model`、`final_model`、`final_subagent`、`final_concurrency`；拒绝删除已有房间，拒绝其他启动参数变化。显式配置 `welcome_enabled` 会在 reload 时更新运行开关，设为关闭会撤销待发送欢迎；未指定时不改变已有运行开关。

热加载保留已有场次目录、URL、通知、已见观众和冷却状态；更新 `publish` 及四个模型路由字段仅用于之后初始化的场次。已有 `config.json` 绑定场次身份后，实时会话、后续终审及冻结分片计划继续使用原路由，不重建或中断已提交任务；回切默认模型也只影响后续场次。空房间启动后，首次热加载可以启动自动化循环。

`model` 与可选 `final_model` 须为非空 `provider/model`，`final_subagent` 须为非空 agent 名，`final_concurrency` 须为1到10的整数（不能用布尔值）；即使工作流关闭也检查已配置的路由。热加载仅做配置校验，不发起模型请求；目标模型的可用性仍由新场预检核实。模型不可用时沿用既有失败处理，不自动切换 provider 或退回旧模型。

动态加房顺序：更新并原子保存配置文件后，调用 `POST /api/automation/reload`；再导入 candidate；最后 `PUT` 完整 fixed policy 加入新房间并提高 `target_rooms`。单独导入 candidate 不加入固定名单。`target_rooms` 不能超过 `MAX_ROOMS`；容量硬上限是启动参数，超过该上限才需要调整并重启。真实容量和房间名单只放本机私有配置。

私有环境文件提供 `RESEND_API_KEY`、`BILI_NOTIFY_FROM`、`BILI_NOTIFY_TO`、`BILI_NOTIFY_CC`。本人通知将 To 设为本人邮箱，CC 设为本人 Pushover 地址；收件人固定从该环境读取，不能由 stdin 或模型添加。

```bash
python -m bilibili_live_monitor.broadcast_email --env-file /PRIVATE/notify.env --dry-run
```

输入为原通知的 JSON；dry-run 验证输入与配置，不发信。实际通知去掉 `--dry-run`。适配器需要返回 `id` 和 RFC `message_id`；邮件服务商的 UUID 不能作为 `In-Reply-To`。旧版只返回 `id` 的适配器须升级，不能冒称已支持同一线程。

先发送原有开播通知。看板双文件首次部署且远端 hash 校验通过后，再以 `Re:` 原主题及 `In-Reply-To` / `References` 回复，附上本场 URL。未知发送回执不自动重发。邮件 accepted 表示适配器确认提交，不代表已经送达或阅读。

## 执行顺序

新开播恢复该房间实时 ASR。本场显式更新 policy 可以降级，跨重启保留该选择，下一场再恢复；不启用自由聊天或点赞。独立 `update_periodic` 会话使用完整模型名，经过 OpenCode 项目校验和 dry-run preflight，不启动或重启 OpenCode 服务。程序每分钟更新看板，每五分钟向同一会话追加复核；busy 时跳过当轮。歌曲查证、灯牌/盲盒和隐私规则沿用实时看板契约。

五分钟复核在核实歌词、歌名或前后文后，可回溯纠正本场旧ASR。校正版写入私有`transcript_corrections.json`，绑定原稿指纹、原因和确切来源引文；不改原始转写。后续AI输入同时提供原文与有效校正版，新校正影响的旧窗口重新复核。原稿被回填替换时，不盲目迁移旧校正，重新查证后再采用；校正与来源均不公开。这里只做文字纠错，不补写缺失歌词或把推断改成原话。

两次新鲜离线确认且录制停止、明确换场或达到时长上限时，冻结本场上界并停止实时派发，确认本场专属 AI 已停止。固定监控主服务继续运行。页面显示本场结束、正在整理最终版，并继续等待最终产物。

本场文件停止变化至少 `seal_seconds` 后，读取本场命名时间范围内的录制源，运行无 VAD 的离线 ASR。源文件只读，转写独立保存在工作区，不覆盖采集数据库。墙钟映射采用文件 UTC 加前序媒体时长的估计，不宣称精确字幕对齐；探测失败项会重新探测，全部选中源文件完成后才能进入终审。不可恢复的媒体进入 `attention`，不把部分稿当作全场完成。

另建 `finalize` 总会话，项目归属使用 `opencode_project`，只向它提交一次完整收尾任务。默认按30分钟分片，前后各60秒上下文，最多4个子任务并行；`final_subagent`须为目标服务实际可用的agent，默认general继承总会话模型。子任务只写独立结果文件，总会话统一合并并处理跨片点歌、未登记歌名。总会话的任务清单和子会话关系用于观察进度。

`session_finalize merge --config ...`校验分片身份、指纹和完整覆盖后统一合并；`session_finalize finish --config ...`复核所有窗口及歌名登记后生成完成标记。会话暂时idle不等于完成，程序不会因此再追加一轮。AI复核完成后，程序在同一URL发布并验证最终页；总会话不能提前宣称页面已发布。`finalize_parallel:false`保留原逐批模式，`final_review_batches`仅用于该兼容模式。

终审也执行ASR回溯纠错：分片提出有来源的局部候选，总会话统一审核并合并私有`transcript_corrections.json`。以当前离线窗口指纹重新定位，不将实时校正按序号套用；原稿与冻结输入不修改。运行`python -m bilibili_live_monitor.session_finalize corrections --config ...`查看有效校正、失效原因与待重审窗口。校正影响的窗口须重新判断歌名、唱聊及声源，并记录当前`correction_id`；未完成时`finish`拒绝收尾。完成标记也绑定校正版本，校正新增、变化或撤销后不可沿用旧完成状态。

## 恢复与验收

| 操作 | 命令/文件 | 约束 |
|---|---|---|
| 直播中回填预检 | `python -m bilibili_live_monitor.session_backfill --config /PRIVATE/sessions/SESSION/config.json --dry-run` | 只选已封口素材，不推进场次状态 |
| 执行或续跑该回填批次 | 同上去掉 `--dry-run` | `live_backfill/manifest.json`固定批次上界；重复命令续跑同一批，不扩展上界 |
| 直播回填输出 | `live_backfill_segments.json` | 私有；自动优先于完整覆盖的实时窗口，缺口和跨界实时证据保留 |
| 健康巡检预检 | `python -m bilibili_live_monitor.session_health --config /PRIVATE/sessions/SESSION/config.json --dry-run` | 不发起恢复或调度 |
| 检查并建立五分钟巡检链 | 同上改为 `--schedule` | 单场锁、单pending；completed/attention即停；默认由runner自动建立 |
| 健康记录 | `health.json`、`health_history.jsonl` | 与五分钟内容AI复核独立，不创建AI会话；失败录制仅有界resume，不覆盖操作者停用 |

`health_enabled:false`可在本场私有config禁用巡检。调度故障独立记入`health_schedule_error`，publisher继续运行并在后续周期补链。音频采集隔离修改属于常驻采集代码，需采集服务加载新版本；仅重载publisher不能使其生效。

单场文件锁排除重复实例。backend 恢复后按 label 找已有进程，子进程恢复原工作区；离线 ASR 保留已完成文件，终审继续使用该场的会话。收尾超时或超过复核预算时保留 `attention`、错误及现有页面，不报告最终成功。

私有 `workflow.json` 记录阶段、URL就绪、错误和收尾进度，`state.json` 记录看板截止点；`/api/automation` 提供逐场工作流状态。验收应覆盖断线重启不重复建场、同线程通知、下播先停止AI、源文件封口、离线转写进入页面、原URL不变和最终停止轮询。合成测试不代替真实部署、邮件送达与模型质量验收。

只上传公开白名单中的两个文件，不上传整个工作区、录制媒体、ASR原文、来源全文或UID。页面随机地址和noindex仅减少发现，不等于身份认证。
