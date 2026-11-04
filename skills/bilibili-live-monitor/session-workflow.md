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

## 通知适配器

### 单房内容模式与录制策略

`rooms.<id>.content_mode` 支持 `music`（默认）与 `talk`。`music` 维持原唱歌流程；`talk` 不创建或调度内容 AI、不查歌名、不做歌曲 AI 终审，保留实时 ASR 与程序每分钟统计发布。下播后封口已保存录音，执行离线 ASR，再转最终静态页。未分析的 ASR 不强行标成已复核杂谈，页面隐藏点歌跟进和唱歌杂谈分段。

`rooms.<id>.record_mode` 支持 `none`、`audio`、`video`。未指定时继承平台 policy；显式配置只覆盖当前房间。`audio` 将网页播放的16kHz、16bit单声道音频分段保存为 WAV，与实时 ASR 共享采集，不另请求媒体下载地址；关闭 ASR 仍保存音频。`video` 沿用独立 FFmpeg 压缩流录制，`none` 不保存媒体。`talk` 可以选择任一录制模式，欢迎开关保持独立。

所有活动场次固定每300秒运行一次 `session_health` 程序巡检，没有可关闭的房间开关。巡检检查录音/录像增长、已启用的 ASR、页面更新及已启用的内容复核，结果保存在私有 `health.json` 与 `health_history.jsonl`。仅对状态新鲜、同一场、未受平台拒绝的错误或停止采集执行有限恢复，30分钟最多两次；不重启共享服务。调度失败保留本轮结果，runner 后续补链；completed/attention 停止本场巡检。程序巡检不调用内容 AI。

需要完成离线收尾的`talk`房间应至少保存`audio`；`none`只能实时采音，没有素材时离线收尾将保留错误而非伪装完成。

新场次从房间配置读取 `content_mode`。运行中将房间改成 `talk` 并 reload 后，场次管理器同步本场配置，runner 停止旧内容 AI 后继续发布；恢复 `music` 从下一新场生效。已完成场次不回写模式。

### 自动化配置热加载与动态加房

新增房间的欢迎弹幕默认关闭，`rooms.<id>.welcome_enabled` 为严格布尔值，默认 `false`；已有持久化界面开关在重启时保留。

`POST /api/automation/reload` 无请求体，从启动时指定的 `AUTOMATION_CONFIG` 文件读取配置，校验全部成功后执行热加载。支持追加或更新 `rooms` 与 `session_workflow.publish`；拒绝删除已有房间，拒绝其他启动参数变化。显式配置 `welcome_enabled` 会在 reload 时更新运行开关，设为关闭会撤销待发送欢迎；未指定时不改变已有运行开关。

热加载保留已有场次目录、URL、通知、已见观众和冷却状态；更新 `publish` 仅用于之后新建的场次。空房间启动后，首次热加载可以启动自动化循环。

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
