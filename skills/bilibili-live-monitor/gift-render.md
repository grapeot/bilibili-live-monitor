# 离线礼物特效切片渲染

将已录制的 B 站直播视频片段与礼物特效素材（MP4 加配套 JSON）离线合成为保留底层声音的礼物特效切片，并完成解码与双联预览图验收。

## 目标与边界

本文件作为根 skill 的子 skill 按需加载，不新建全局 skill。工具链仅提供离线 CLI，不涉及采集端或看板部署、不重启常驻服务、不接入直播自动收尾、不自动发布、不新增通知。系统不设独立礼物 WebSocket，礼物事件由现有网页连接接收并写入数据库。

当任务目标是把一场直播所有带特效的礼物切片切出来时，由 Agent 调用 `events` 读取本场礼物候选列表，再按各事件实际情况编排素材缓存、计划生成、离线合成或登记未覆盖原因。合成使用的是观测时的特效素材版本，不声称复原历史网页的排队、位置、连击或动态文字效果。生产配置与真实路径从本机私有 overlay 读取；真实媒体、素材缓存、计划与渲染产物均留在私有目录，不进入公开仓库。

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

catalog 支持 `{"gifts":[...],"effects":[...]}` 或 `{"assets":{"giftAssets":...,"effectAssetsList":...}}`。数量按 `countMap` 精确匹配，非零覆盖普通 `effectId`，零值回退。仅下载目标礼物所需 MP4 与 JSON，按内容 SHA256 去重；目录快照、固定版本与最新索引分开保存，刷新不改旧版本。stdout 返回 `cache_hit`、`asset`（版本记录路径）、`version`。当前仅支持 MP4 加配套 JSON，SVGA、Spine 和动态替换元素明确报错，不假称已支持。

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

## 输出规格

新输出目录包含 `video.mp4`（合成）、`base.mp4`（底层）、`plan.json`、`assets/effect.mp4`、`assets/effect.json`、`assets/version.json`、`preview.jpg`（左底右合）和 `verification.json`。失败时可能保留本次中间文件，用于诊断；它们不代表验收通过。

## 真实陷阱

- **旧归档画幅冲突**：历史回执出现容器尺寸与 coded 尺寸不同。缺少新版证据时，须显式给出已确认的 `--base-display-aspect`，不能靠可解码判断比例正确。校正归档显示比例不等于重新解码原始 H.264 或恢复丢失细节。
- **候选不是成片**：有送礼事件不保证有动画资源或完整录像。整场任务保留每条事件的成功或未覆盖状态；多个事件引用相同素材时复用缓存，分别记录时间和数量。
