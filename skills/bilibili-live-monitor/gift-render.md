# 离线礼物特效切片渲染

将已录制的 B 站直播视频片段与礼物特效素材（MP4 加配套 JSON）离线合成为保留底层声音的礼物特效切片，并完成解码与双联预览图验收。

## 目标与边界

本文件作为根 skill 的子 skill 按需加载，不新建全局 skill。工具链仅提供离线 CLI，不涉及采集端或看板部署、不重启常驻服务、不接入直播自动收尾、不自动发布、不新增通知。系统不设独立礼物 WebSocket，礼物事件由现有网页连接接收并写入数据库。

当任务目标是把一场直播所有带特效的礼物切片切出来时，由 Agent 调用 `events` 读取本场候选并准备素材缓存，再优先使用下述批次流程合并重叠录像窗口、离线合成或登记未覆盖原因。合成使用的是观测时的特效素材版本，不声称复原历史网页的排队、位置、连击或动态文字效果。生产配置与真实路径从本机私有 overlay 读取；真实媒体、素材缓存、计划与渲染产物均留在私有目录，不进入公开仓库。

## 验收标准

- **候选与成片状态区分**：`events` 条目仅为候选事件，不能把候选或仅产出 `plan.json` 表述为渲染成功。无 `gift_id`、无对应特效或遇到未实现的格式时，必须记录未覆盖原因；整场任务逐事件列出输出路径或原因，不以成功文件数代替总候选数。
- **规格与完整解码验收**：`render` 自动校验画幅、音轨、目标时长与完整解码；`verification.json` 必须记录通过，完整覆盖 `[at-before, at+动画时长+after]`。可用 `verify` 独立重验。
- **双联预览图视觉验收**：完整解码不等于比例正确。Agent 必须查看或交由人工核查 `preview.jpg`（左底层、右合成），确认底层比例、透明边缘与摆放位置。
- **隔离输出与失败留证**：每次渲染写入全新的私有目录，拒绝复用已有目录；失败保留现场，不覆盖或改记为成功。

## 可用资源与命令约束

### 礼物素材获取与缓存

```bash
bili-live gift-assets fetch --room 123456 --gift-id 123 --count 1 \
  --cache /PRIVATE/cache --cdp-url http://127.0.0.1:9222
```

默认缓存为 `XDG_CACHE_HOME/bilibili-live-monitor/gift-assets`（未设置时为 `~/.cache/bilibili-live-monitor/gift-assets`）。缓存命中不联网；缺失或显式 `--refresh` 时，提供 `--catalog /PRIVATE/catalog.json` 快照，或 `--cdp-url` 只读读取已打开且唯一的该房间网页。这两个来源参数互斥。CDP 不创建、重载或关闭页面，不送礼；网页 Vue 结构变更直接报错。

catalog 支持 `{"gifts":[...],"effects":[...]}` 或 `{"assets":{"giftAssets":...,"effectAssetsList":...}}`。数量按 `countMap` 精确匹配，非零覆盖普通 `effectId`，零值回退。仅下载目标礼物所需 MP4 与 JSON，按内容 SHA256 去重；目录快照、固定版本与最新索引分开保存，刷新不改旧版本。stdout 返回 `cache_hit`、`asset`（版本记录路径）、`version`。支持 MP4 加配套 JSON，以及下述已知舰长昵称文字槽；SVGA、Spine、图片替换及未知动态元素明确报错，不假称已支持。

### 场次候选盘点

```bash
bili-live gift-render events --session /PRIVATE/live_sessions/SESSION_DIR
```

`--session` 是目录路径而非单独 ID。命令读取 `config.json` 与只读 SQLite，按房间、场次开始和截止时间筛选礼物：有 `offline/manifest.json` 时使用其 cutoff，否则使用当前时刻。输出 `events` 列表，条目含 `event_id`、`at`、`gift_id`、`count`、`gift_name`、`status`；无礼物 ID 的条目状态为 `no_gift_id`，其他为 `candidate`，不导出 UID 或昵称。

### 计划、合成与重验

```bash
# 单文件时间为相对秒数，也可用--room/--gift-id/--cache替代--asset。
bili-live gift-render plan --base /PRIVATE/base.mp4 --at 6 \
  --asset /PRIVATE/cache/versions/VERSION.json --before 6 --after 6

# 场次时间必须带时区；自动从配置获得房间号。
bili-live gift-render plan --session /PRIVATE/live_sessions/SESSION_DIR \
  --at 2030-01-01T12:00:00+08:00 --gift-id 123 --count 1 --cache /PRIVATE/cache

# 保存stdout为plan.json后执行；render也支持与plan相同的直接输入参数。
bili-live gift-render render --plan /PRIVATE/plan.json --output /PRIVATE/result
bili-live gift-render verify --output /PRIVATE/result
```

`plan` 只输出 JSON，不下载、不编码、不写来源目录。计划绑定素材哈希、版本与来源 stat，刷新缓存不会改变已生成计划。渲染始终离线，缓存缺失先显式 fetch。

时间区间为 `at-before` 至 `at+动画时长+after`，总长不超过 600 秒，不裁短缺失区间。特效从成片第 `before` 秒开始，保留底层声音，不混入特效独立音轨。

`--placement` 为 `center-fit`（默认）、`left-fit`、`right-fit`；`--region x,y,w,h` 是输出像素区域；`--canvas 1600x1280` 指定不超过 4096 的偶数画布，底层保持正确显示比例并等比补边。`--base-display-aspect 5:4` 用于人工确认后的旧归档比例校正，不按 PK 标签硬编码 5:4。

场次模式需要既有 `offline/manifest.json` 做墙钟到文件映射，校验来源或归档回执，不重跑 ASR、不改原片。直播中尚无清单时，使用已封口的明确 `--base` 文件和文件内秒数。输入须有单视频轨和单音频轨，检查目标区间包覆盖，拒绝缺口、重叠或素材变化。支持跨稳定分片和不同分片的画幅归一；片内画幅切换报错，须先拆稳定输入。管理原片需要参数恢复时，先显式准备独立可读副本，工具不会自动向生产 `.codec-media` 写入。

### 原始录制分片的解码准备

```bash
bili-live gift-render prepare --base /PRIVATE/recordings/segment.mkv --output /PRIVATE/prepared
# 使用prepare返回的prepared路径及原文件相对秒数，继续plan/render。
bili-live gift-render render --base /PRIVATE/prepared/prepared.mkv --at 30 \
  --asset /PRIVATE/cache/versions/VERSION.json --output /PRIVATE/result
```

`prepare` 的输出目录必须全新且在原始录像目录之外。它只复制原片并按可信历史参数准备解码头；较长参数头需要重建 Matroska Tracks 长度并移除失效索引，不重新编码媒体。准备后逐包核对内容及时间戳，生产原片与 `.codec-media` 不变。返回 `prepared` 路径与 `preparation.json`，不把解码头准备等同于整片画幅稳定或完整解码通过；后续计划仍检查目标时间段的画幅和覆盖。片内仍有参数切换时，应选择稳定区间或另行拆分。

跨分片合成的中间音频使用 PCM，拼接后只编码一次 AAC，避免逐片 AAC 的延迟／尾部填充在接缝产生新增静音。成片仍保留底层声音，不混入礼物音轨。

## 舰长事件绑定与动态昵称

### 事件素材抓取与绑定校验

- 事件 ID 来自 `gift-render events`，须属于所选场次和房间。
- 只支持有平台时间的 `GUARD_BUY`，按房间、UID、平台时间、`gift_id`、数量、`guard_level` 匹配唯一 `USER_TOAST_MSG_V2`，从 `ship_effect_id` 获取动画号，不从礼物号或昵称猜测。绑定缺失、多义、未支持素材均拒绝。
- 抓取事件素材（亦可用 `--cdp-url` 代替 `--catalog`，只读已经打开且唯一的该房页面）：

```bash
bili-live gift-assets fetch-event --session /PRIVATE/live_sessions/SESSION_DIR \
  --event-id 42 --cache /PRIVATE/cache --catalog /PRIVATE/catalog.json
```

### 事件渲染、计划固化与独立底片模式

按事件渲染，从事件绑定昵称、时刻和数量，不混用 `--at`、`--gift-id`、`--room`、`--count` 覆盖或 `--username`：

```bash
bili-live gift-render render --session /PRIVATE/live_sessions/SESSION_DIR \
  --event-id 42 --cache /PRIVATE/cache --font /PRIVATE/font.ttf --output /PRIVATE/result
```

- 默认保留动画前后各6秒，可照普通模式选 `--region` 和 `--canvas`，不自动判 PK 左右；PK 合流本身是一条录像。
- `--event-id` 同样支持 `plan`；固定计划后使用 `render --plan ...`。事件素材按动画 ID 单独索引，不覆盖普通礼物索引。
- 字体 SHA256 与昵称进入计划，字体变更拒绝；输出 `assets` 保留字体副本与 `bindings.json`。
- 独立 `--base` 文件模式可用 `--asset` 加 `--at 6 --username TestUser --font /PRIVATE/font.ttf` 明确昵称，不能把这当历史事件核验。

### 动态合成规范与边界

- 舰长文字限 `srcTag=live.guard.open.animation.user.name` 的 `txt/fitXY`、单色无额外 `style`；字体须显式指定。另支持下述原始批次绑定的普通盲盒结果图片槽。
- 按 JSON 的 `frame`/`mFrame` 逐帧位置与遮罩合成，未知语义、其他图片槽及样式仍拒绝；源素材 MP4/JSON 不修改。
- 合成按时间戳归零对齐，动画保留 JSON 帧率，输出30fps；25/30fps底片配30fps动画通过离线测试，不改播放速度。不混入特效独立音轨。
- 输出包含真实昵称及本机路径，必须留在私有目录。字体与排队延迟不保证与历史网页相同，不自动送礼、发布或接入录制收尾。

## 整场批次去重与连续场景

### 普通盲盒结果卡

```bash
# event-id是本场某条盲盒结果的timeline ID；同批任一结果可用于抓取。
bili-live gift-assets fetch-event --session /PRIVATE/session --event-id 42 \
  --cache /PRIVATE/cache --catalog /PRIVATE/catalog.json \
  --card-background /PRIVATE/verified-background.png --card-digits /PRIVATE/digits
# digits目录包含已核对的平台0.png至9.png；缓存命中不联网。
bili-live gift-render render --session /PRIVATE/session --event-id 42 \
  --cache /PRIVATE/cache --output /PRIVATE/result
```

- 从原始`SEND_GIFT_V2`中的订单身份匹配本场完整结果，不按昵称或同秒合并；同订单多结果按原始下标区分，缺项、冲突、无身份和缺素材快照均拒绝。结果图标来自报文的`gift_info`快照。卡底及数字须显式提供已核对的PNG，固定内容哈希；不使用配置中失效的本地路径，不自动猜替代图。
- 仅支持`live.gift.blindboxresult.img`的`img/fitXY`槽；1至10条结果按图标及数量组成一张卡，嵌进原开盒动画的逐帧遮罩。单次五十盒开出三种结果时仅播放一次开盒动画、显示三格；覆盖清单仍逐条登记全部结果。独立结果特效非零或目录存在兼容动画时批次拒绝完整覆盖并报告原因，不能静默略过。
- 这是依据真实结果重建的开盒展示。网页送礼者主态与普通观众广播路径不同，不宣称所有人历史上都看到了开盒动画，不复原网页队列。普通盲盒缓存与正式`batch-plan/render/verify`共用，渲染完全离线。

处理整场直播或同场多条礼物时，优先使用批次流水线；单事件 `render` 用于单条调试，不要逐事件独立截片后直接拼接，以免重复播放相同实录。

### 批次命令工作流

```bash
# 缓存缺失须先fetch；catalog可选，只用于解释未覆盖原因，不下载素材。
bili-live gift-render batch-plan --session /PRIVATE/session --cache /PRIVATE/cache \
  --font /PRIVATE/font.ttf --before 2 --after 4 --canvas 1920x1080 \
  --catalog /PRIVATE/catalog.json \
  --event-region 42=120,80,960,720 --event-aspect 42=5:4 > /PRIVATE/batch.json

bili-live gift-render batch-render --plan /PRIVATE/batch.json --output /PRIVATE/result
bili-live gift-render batch-verify --output /PRIVATE/result
```

### 去重、连续场景与验收规则

- **尾段盘点与显式纳入**：默认`events`及`batch-plan`仍以冻结cutoff划定场次，但在`recorded_tail.events`单独报告同一连续录制内的后续礼物。需要纳入时用`batch-plan --include-recorded-tail`；尾段舰长/盲盒的`fetch-event`及单事件`plan/render`也须带该选项。此选项不修改生产场次或清单。范围从cutoff左侧录制建立，只延伸同run_group/run_start、无缺口或重叠的连续分片；新run在cutoff开始时也不能视作旧场尾段。完整动画及前后文越过录制边界时拒绝，不截短或跨场取片；计划和验证结果保留尾段选择及候选清单。
- **身份去重**：消息按房间与已有 `source_key` 去重，同一身份的关键事件字段冲突时拒绝；无 `source_key` 时只按 timeline 行 ID 区分，不按同礼物、昵称、相同时刻或数量猜重复。多条真实赠送全部保留，重复项以 `duplicate_of` 标明来源。
- **区间并集与单底片多特效**：对前后文录像区间取并集，重叠或相接片段只播一次；`--gap` 默认0秒，可指定最多60秒，将相距不超过该值的窗口合并。一个场景包含多个按原始事件时刻触发的特效，只准备一份底片，音轨不随特效重复；不模拟网页历史排队或连击合并。
- **人工覆盖**：`--event-region ID=x,y,w,h` 及 `--event-aspect ID=5:4` 覆盖已人工核对的具体事件，不自动判 PK，不按标签硬编码比例。重叠场景中的人工比例覆盖不一致时拒绝；混合画幅归档中的补边不能当作原片比例错误，应先保持已验证归档几何，或另做有来源证据的私有裁边副本。
- **画布与音画拼接**：所有场景统一指定画布及30fps；按时间戳重置并统一帧率，解码后的场景音轨拼接后仅编码一次AAC，避免逐片AAC直拼新增静音。
- **产物与覆盖率**：输出 `batch-plan.json`、各 `scene-0000/` 的底片/成片/预览/验收、总 `video.mp4`、`coverage.json`、`verification.json` 和章节。无特效/缓存缺失和不可计划事件在 `inventory` 列明；盲盒一个overlay对应原批次全部`event_ids`，逐条结果均须覆盖一次。最终覆盖清单仅把成功渲染的 `planned` 改为 `rendered`，不能把候选或计划成功称为渲染成功。
- **计划验收**：验证每个可渲染事件恰好覆盖一次、场景源区间不重叠、素材/字体/来源指纹；固定场次manifest哈希，从墙钟重新核对实际取片路径、偏移、时长与来源区间无复用，拒绝只改声明时间而重播另一段底片。清单改变须重新计划。最终30fps帧数按计划时长严格验收。没有可渲染项时，`batch-plan` 仍输出 `inventory`，`batch-render` 拒绝生成空片；不自动抓素材或发布。

## 输出规格

新输出目录包含 `video.mp4`（合成）、`base.mp4`（底层）、`plan.json`、`assets/effect.mp4`、`assets/effect.json`、`assets/version.json`、`preview.jpg`（左底右合）和 `verification.json`。失败时可能保留本次中间文件，用于诊断；它们不代表验收通过。

## 真实陷阱

- **旧归档画幅冲突**：历史回执出现容器尺寸与 coded 尺寸不同。缺少新版证据时，须显式给出已确认的 `--base-display-aspect`，不能靠可解码判断比例正确。校正归档显示比例不等于重新解码原始 H.264 或恢复丢失细节。
- **候选不是成片**：有送礼事件不保证有动画资源或完整录像。整场任务保留每条事件的成功或未覆盖状态；多个事件引用相同素材时复用缓存，分别记录时间和数量。
