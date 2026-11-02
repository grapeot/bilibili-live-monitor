---
name: bilibili-live-monitor
description: 在Apple Silicon Mac运行B站常驻录制平台，通过AI控制面管理目标与来源，将原始媒体、本地语音转写与网页弹幕持久保存；包含面向AI的平台运维子skill。
---

# B站多路直播监控

## 固定房间跨场守候

平台调度支持 `Policy.mode='fixed'`（默认仍为 `rotation`），持久保留候选与目标房间，不自动发现或换房；定时查开播状态按需启停录制，未开播不占房间 tab、不耗失败预算，重启后重查状态。可关闭实时 ASR，先存原始音视频供离线处理。SQLite 保存弹幕身份、原始 WebSocket 业务报文及定期舰队榜单快照；真实数据只存私有 `DATA_DIR`。配置、状态与恢复入口见 [平台运维](platform-operations.md#固定模式配置与启停)。采集器本身不判断歌名；可通过单场看板的独立AI复核和来源登记展示歌名，离线歌名须交叉核对后导入活动页。

## 定位与注册

默认保存原始音频与弹幕，可独立启用ASR、音视频保存和按需缩略图。每条弹幕和每段转写各作为一个item，按事件时间混合在每房间时间线。不做视觉理解，无付费识别API；运行消耗本机算力、内存与网络。

从目标workspace的AGENTS.md或CLAUDE.md开始，沿路由找到skill索引，只注册本根skill。主机路径、运行目录与真实房间列表放本地私有overlay。公开仓库：<https://github.com/grapeot/bilibili-live-monitor>。

## AI 操作者入口

常驻目标、重启恢复、健康摘要、开播巡检与补位见 [平台运维子skill](platform-operations.md)。`rotate-live`默认只读预演，显式`--execute`才停止已确认停播的来源并补位；定时频率与最终一次时间由外部调度器管理。只注册本根skill，子skill按任务加载；真实路径、端口、模型和运行状态从本机私有overlay读取。

已有录制素材的盘点、可恢复本地转写与文本/能量判读边界见 [离线ASR子skill](offline-asr.md)。

直播中或结束后，使用 `bili-live trends build` 从已有数据库只读生成入场流量、送礼频次、关注总数与粉丝团人数四图及冻结数据；参数、口径与 `trends verify` 验收见 [直播趋势子skill](trends.md)。不画大航海人数，不访问控制面或中断录制。

需要对照唱歌/讲话与关注变化时，见 [活动候选时间轴](activity.md)：`activity build/verify` 分开保存活动类别与声源，仅绿色高亮主播现场演唱候选，录音/对口型/声源不明不高亮。支持Flash分片初判后主线程全场复核、本地试听及标注修订；文本语义复核不等于听音确认，不推断因果。

用户要求盯一场直播、每几分钟巡检、下播后全量分析并分享时，见[单场跟进闭环](session-followup.md)。定时回到原会话、录制优先的ASR降级、bot文案核对、封口等待和发布验收都按该契约执行，具体运行身份留在私有overlay。

用户要求为一场直播建立每分钟更新的四图、歌名/点歌跟进和分段汇总时，见[单场实时共享看板](live-share.md)。使用 `bili-live live-share init/export/verify/register-song` 配置每场规则、核验来源和页面；可复用到不同主播，默认不启用AI或远端发布。

用户要求固定监控自动为每场建看板、同线程通知URL、下播离线ASR并更新同一URL时，见[自动逐场工作流](session-workflow.md)。一次配置后由Automation与独立单场进程接力，默认关闭，不需逐场手动初始化。

需要从整场直播制作2–3分钟引流预告时，见[直播引流预告片](trailer.md)：事件选择、源时间映射、隐私裁剪、`trailer render/verify`、剪点与独立审阅。真实精剪表和媒体留在私有输出目录。

## 环境与安装

需要Apple Silicon Mac、Python3.12、uv、Node.js、Chrome与ffmpeg。在仓库根目录执行：

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
# 以下两行仅在需要ASR时执行
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

默认服务8765、CDP9222、房间上限10、队列20，均可配置。30路实测先设MAX_ROOMS=30、QUEUE_SIZE=60。看板打开http://127.0.0.1:8765，添加虚构示例123456或实际授权的房间链接。临时房间重启后不自动重开；平台候选与policy目标持久化并自动恢复。

## 采集、存储与接口

默认RECORD_MODE=audio、ASR_ENABLED=0、PREVIEW_ENABLED=0，新增房间可覆盖record_mode/asr_enabled/preview_enabled三个独立字段。ASR关闭时不加载模型、不创建识别PCM窗口或队列；看板显示禁用。ffmpeg复制原始压缩包分段写入DATA_DIR/recordings/<room_id>/，音频.mka、音视频.mkv。默认300秒分段，RECORDING_SEGMENT_SECONDS可配置。

实时ASR只使用千问流式：每5秒送入新PCM，每房间保留独立的最长30秒音频上下文。当前窗口以同一条时间线记录持续修订，`transcript_version=realtime`、`stable_text`与`is_final`区分实时稿状态；窗结束不代表识别准确性已经核验。音频缺口或worker重启必须重建上下文，不拼接成无缝录音。原始录制不依赖这些实时稿；`offline-asr`继续独立从媒体转写并写入自己的输出目录，不覆盖实时记录。旧30路独立切片验收不代表流式容量。

个人点赞通知作为`kind=like`保存，与平台累计点赞分开：`room.like_summary.reported_total`是最近收到的累计值，不能逐次相加或直接称作本次新增量；`notifications`是已保存通知条数，`identified_users`是其中可识别UID去重数，不是完整点赞人数。通知时间为接收时间，匿名身份保持隐藏。历史通知可依据`raw_event_id`核验与去重。

`scripts/welcome_once.py`是独立的一次性欢迎助手，默认仅dry-run。只有用户明确批准文案、目标房间和一次发送后才加`--execute`；必须用`--since`固定带时区的本场观察起点，不把采集重启当作新场次。`--minimum 1`用于下一位新观察UID的单次测试，默认minimum=4、window=30、cooldown=300。它按原始接收顺序读取本场入场、弹幕、礼物、点赞，排除已观察UID，复用已登录Chrome正常输入框，成功后即退出，不启用常驻循环。实际发送请求核对目标和精确文案，成功回执绑定该请求。账本固定为数据库目录下的`welcome_once_<room_id>.json`；sending/sent/ambiguous状态禁止自动重跑，不根据超时猜测未发送，不换路径绕过账本。原始UID与文案回执只保存在私有路径。

发送链路支持浏览器实际使用的UTF-8 multipart表单及URLencoded表单；重复目标/文案字段拒绝发送。仅发送期间对目标页临时绕过Service Worker，确保请求经过校验，结束恢复原生SW处理并分离CDP会话。页面上限按ASCII宽1、非ASCII每UTF-16单元宽2计数；组合超限才拆两条，间隔5秒。完成后同时检查平台HTTP/code回执与采集到的完整弹幕回显，区分已确认、未确认和仅点击按钮。

`scripts/welcome_session.py`仅在用户明确授权持续欢迎后使用，默认仍dry-run；参数另需`--api-url`，默认30秒至少2位新观察UID、300秒冷却，可用`--last-sent-at`继承前次发送时间。必须配合仅含目标房间的fixed/target=1策略和外部单场生命周期控制。未知或过期状态暂停；新鲜的两次离线确认结束本场。该CLI不会自行关闭录制后端，外部控制须持久禁用policy、回收自有进程并保留不可自动恢复的结束标记。单场账本`welcome_session_<room_id>.json`一旦启动，禁止自动重新执行；发送失败保留现场，不自动重发或重启发送器。

临时断流默认按2秒起指数退避，300秒封顶，连续第20次失败后停止自动重试并标为人工处理；连续有效采集60秒才重置。`MAX_RECONNECTS=20`、`RETRY_DELAY=2`、`RETRY_MAX_DELAY=300`为对应配置。房间状态包含`reconnect_attempts`与`next_retry_utc`。明确403/412/429不走持续重试，仍须人工确认后resume。专用Chrome保留`about:blank#live-monitor-anchor`空白页供查播与下一次开播复用，不要把它当作无用标签页关闭。

每房间独立Page并静音。仅启用ASR时，video.captureStream音轨经AudioContext(16000Hz)与AudioWorklet收PCM，Worklet输出置0不外放。ASR与缩略图都关闭时暂停网页视频，后台录制独立进行。复用网页已有WebSocket采集弹幕，不另建连接。缩略图仅显式启用、卡片可见且页面前台时请求。

仅录制模式同时对顶层与子frame启用低渲染：根元素透明，并监听新增视频/自动播放以维持暂停。Chrome直播页不再显示画面，dashboard仍正常；页面布局、动画时序、脚本和WebSocket保留，ffmpeg继续独立录制。不全局冻结CSS动画，以免淡入式验证码始终透明而漏报。ASR或预览开启时不安装低渲染守卫，选项改变由平台关闭旧页再创建。`python scripts/verify_low_render.py --cdp-url <本机CDP>`可在独立合成页验证播放防护、静态/淡入验证码判据及非pause路径，不访问真实直播。

暂停播放不保证网页停止下载；同一页面也可能有额外播放器。监测资源时分开统计录制进程、浏览器和整机，不能把录制写入速率等同于总下载带宽，也不能把GPU进程CPU时间当成GPU利用率。可选同源`assets/resource_metrics.json`由本机采样器提供资源面板；无采样器时隐藏，数据超过30秒时标记失效。该私有运行文件不进Git，整机GPU和RSS共享页口径在面板中明确标注。

SQLite保存完整segments、timeline和SSE事件。UI最近80条item、20段识别数据不代表存储上限。支持全宽、全屏、自动向下跟随，上翻时暂停跟随。

采集器复用页面原有WebSocket连接捕获礼物与付费互动事件，支持JSON `SEND_GIFT`、protobuf `SEND_GIFT_V2`（含单条消息内多个盲盒结果）、`GUARD_BUY`及`SUPER_CHAT_MESSAGE`。事件统一以`kind=gift`写入SQLite timeline，并进入SSE `item`、`GET /api/state`、JSONL导出与前端混合时间线；UI仍只显示最近80项。忽略`COMBO_SEND`累计通知，避免重复记账；去重优先采用订单标识，大航海缺订单但有UID时采用发送者、事件时间、等级与数量等事件字段，其他情况回退payload hash；不承诺识别全部语义相同的重发。V2多结果保留`batch_index`。历史未保存礼物报文，不能从旧录音回补礼物流。

礼物字段包含事件类型、协议UID（可能缺失或脱敏）、事件/接收时间、订单标识、礼物名/数量及原始`price`、`total_coin`、`coin_type`。金额缺失用null，显式0保留0。协议事件时间缺失时，`event_at_utc=null`、`time_source=received`，`at_utc`仅使用接收时钟展示，不能用于精确的事件先后因果推断。统计时将金币、银币分开，大航海原始金额用`guard_price_gold`单列（单位金瓜子，不乘数量），SC人民币金额单列；界面分别标识银币礼物、金币礼物、盲盒结果、大航海与醒目留言。盲盒分别展示结果标价与未换算的原始价格字段，不推断原始字段的币种或单价，结果标价不代表真实付费或主播净收入。五房间180秒真实测试只有单结果V2金币礼物，8条均无UID、无盲盒/多结果；UID、多结果盲盒、上舰、SC与旧式礼物仅有合成测试覆盖，短样本不代表全量捕获或长期覆盖。

- GET /api/state：房间、最近记录与运行指标。
- POST /api/rooms：`{"url":"123456","record_mode":"audio","asr_enabled":false,"preview_enabled":false}`；旧`chunk_seconds`兼容接收，实时输入固定5秒。
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

30路各两个有效窗口已短测验证，包含恢复，不保证无间隙、弹幕全量或长期稳定。可选的持久欢迎、开下播通知、账号与点赞控制见`docs/persistent_automation.md`；未配置时不启用这些外发能力。

## 持久欢迎、账号与点赞

通过私有`AUTOMATION_CONFIG`分别配置房间文案、账号profile及通知适配器。已配置房间的自动欢迎默认开启，支持看板或`python scripts/automation_control.py welcome 123456 off`动态关闭。关闭只影响自动欢迎，不影响录制与状态通知；AI推理仍需单独明确启用。

固定模式下播只结束该房间采集，服务和历史看板继续运行；下次开播自动恢复。通知对新鲜开播及两次确认下播去重，网络错误不当作下播。账号切换短暂停止采集、保留各自Cookie与欢迎冷却，不复制会话到另一profile。

点赞必须显式指定房间、次数和请求身份，不默认运行：`python scripts/automation_control.py like 123456 20 --request-id example-1`。遵守网页冷却、加入随机间隔；计数是前端触发，不宣称服务端最终计入。完整API、限制与私有配置示例见上述设计文档。

## 数据边界

ASR临时音频处理后删除；原始录制文件按保存选项独立保留。真实房间、用户名、转写、截图、数据库、录音与本机路径不得进入公开仓库；证据放.local/。公开示例统一使用虚构123456。
