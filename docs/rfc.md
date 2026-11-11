# 技术设计

本文件描述当前实现、数据契约与责任边界；用户任务见[PRD](prd.md)，操作步骤见[根skill](../skills/bilibili-live-monitor/SKILL.md)，验证与差异裁决见[test.md](test.md)。配置默认值以 `src/bilibili_live_monitor/config.py:Settings` 为来源，不将部署中的私有配置当作公开默认值。

## 1. 模块责任与实际依赖

Python 3.12/FastAPI提供本机API与静态前端，React/Vite/TypeScript负责看板，SQLite保存事实与状态，CDP连接专用Chrome。启用ASR时使用独立解释器内的本地模型进程。

| 责任 | 当前模块（相对 `src/bilibili_live_monitor/`） |
|---|---|
| 采集、媒体与存储 | `monitor.py`、`capture.py`、`recording.py`、`pcm_recording.py`、`store.py`、`asr.py`、`streaming_worker.py` |
| 平台、账号与统一入口 | `platform.py`、`accounts.py`、`app.py`、`dashboard_gateway.py` |
| 单场产物与生命周期 | `live_share.py`、`live_share_control.py`、`live_share_watch.py`、`session_workflow.py`、`session_runner.py`、`session_finalize.py` |
| 活动、整场与跨场分析 | `activity_summary.py`、`archive_summary.py`、`room_archive.py`、`song_catalog.py`、`song_gifts.py`、`song_followers.py`、`audience_crm.py` |
| 授权操作与可选产物 | `assistant_api.py`、`automation.py`、`audience_publish.py`、`offline_asr.py`、`trailer.py`、`recording_archive.py`及对应脚本 |

`time_utils.py`提供时间转换，`session_source.py`提供只读取数与范围裁剪；活动、归档和歌曲分析直接依赖这些基础模块。`live_share.py`继续负责基础快照和页面渲染，并保留 `epoch/iso/read_source/scope_source` 兼容导出，外部消费者无需同时改入口。部分写入、编排模块仍依赖它的其他工具函数，不将当前结构描述为已经完全分层。

`session_snapshot.py`共用配置到快照的组装、内容模式处理和HTML/JSON写出步骤，不自行取数、推断阶段或发布。调用者传入已确定的source、cutoff、phase、health和标注；正常写出保持先HTML后JSON，异常收尾只复用适用的HTML写出步骤。不同入口的临时文件名也保留。

`export_snapshot`、`watch.build`、`watch.cycle`截止异常分支各有产物写出路径。手动export在stop_intent时标记completed，watch带workflow_key时标记finalizing；正常路径先HTML后JSON，截止异常路径从已有snapshot文件读取，先JSON后HTML。单文件临时替换不等于HTML/JSON联合事务。阶段、失败后状态和调用顺序是各入口契约，不能只根据函数名字推断行为相同。

## 2. 采集、录制与流式识别

每房间独立Chrome页面，播放器静音。audio模式或启用ASR/预览时需要播放器就绪；audio从网页16kHz单声道PCM保存WAV，即使关闭ASR仍保持播放。video由FFmpeg复制媒体流到MKV；仅在不需要ASR/预览且不是audio模式时可暂停网页视频并使用低渲染守卫。none不生成媒体文件。页面脚本和WebSocket仍需运行，低渲染不能冻结导致验证码不可见的动画。

实时输入固定5秒，每房间保留最长30秒上下文，同窗持续修订一条时间线记录。`transcript_version`、`stable_text`、`is_final`表达实时稿状态；上下文在音频缺口或worker重启后重建。原始录制、ASR临时素材和独立离线转写是不同产物，处理临时素材不删除持久录音。

来源有效性检查包含音轨状态、PCM/墙钟覆盖、媒体推进与缺失/丢弃样本；失败不能当作正常静音。低RMS不是语音/音乐分类，实时稿完成也不代表语义已核验。停止后取消排队与在途任务，迟到结果按generation检查；过载、重试与错误保持可见。

主要默认值来自Settings：audio录制，ASR/预览关闭，录制分段300秒，房间上限10、队列20；重连从2秒指数退避、300秒封顶、20次连续失败后停止，连续有效采集60秒才重置。明确HTTP拒绝不走无期限重试。容量配置不是容量实测承诺。

## 3. 事实、时间与存储

SQLite保存房间、segments、timeline和递增SSE事件；timeline按 `(room_id,kind,source_key)` 去重，同一转写窗口更新原item。网页已有WebSocket提供弹幕、礼物、点赞及平台状态，普通/zlib/Brotli报文在后端解析。协议事件时间、接收时间、写入次序不可混同；缺事件时间时保留来源标记，不把接收时间当精确因果证据。

看板只显示最近记录，不截断历史。采集API的SSE支持Last-Event-ID恢复；网关的周期快照汇总不是同一个事件重放协议。JSONL导出固定起始最大ID分批读取，不承诺数据库事务级一致快照。只有排查问题确实需要冻结输入时才制作事务一致数据库副本，普通历史抽查不以此为前提。

## 4. 统计口径与消费者差异

| 指标 | 当前语义与需要区分的边界 |
|---|---|
| 礼物金额 | 协议平台标价，不是主播实收；SC人民币、大航海金瓜子、金币/银币保留原单位。`gift_amount`转换到毫元累计；银币记0，未知记None，total不再乘num |
| 件数、事件数、付费身份数 | 三者不同；事件曲线与去重金额可能经过不同处理，不以表格行数代替人数；身份缺失不推成确定个人 |
| 在线均值和峰值 | archive按相邻观测持续时长加权，首观测前不回填；峰值取有效原始观测，不能拿分钟采样峰值或样本算术平均替代 |
| 粉丝变化 | 按窗口端点计算净变化，允许负数；不是最大减最小，也不是新增关注事件数 |
| 粉丝团人数 | 平台推送的当前点亮人数（存量，点亮约72小时后不续即熄灭），只在开播时推送。房间主页按北京时间自然日取最后一次推送为收盘值；日变化与上一个有推送的日期比较，跨过下播期间；滚动窗口给收盘值日均、最新值及较窗口前最后收盘值的变化，缺基线时用窗口内首个收盘值，单个收盘值不给变化。无推送为null，不沿用前值 |
| 累计看过 | 该场截止前的有效平台累计观测，不是跨场去重观众，不逐次累计状态消息 |
| 场次、自然日与活动段 | 整场按北京时间开播日归属；自然日按日窗口；活动段含截止限制下的30秒尾窗，可能相互重叠，不能直接相加作整场 |

这些定义还需结合具体消费者。`archive_summary.summaries` 在缺测时可返回null，非自送礼字段当前直接为null；外部归档聚合可能等待完整观测、拒绝未知金额并按私有排除集合计算非自送礼。单粉丝观测时，`activity_summary.summarize_session`可给0变化，archive至少需要两个观测。独立实现对乱序、同刻观测和缺去重键的处理也可能不同，不能声称只有一套完全统一的统计。

`shared_summaries`投影既有汇总JSON，保持原指标；这与从原始事件重新计算不同。共享JSON的多页面一致只证明传递/展示一致。历史缺start_utc时不猜造本地详情；回退只接受显式HTTPS origin及受限 `/s/<safe>/`、`/share/<safe>/` 路径，本地有效详情优先。

匿名分析身份限定于房间及原样昵称，同名可能合并、改名视作不同人，不与UID档案自动合并。昵称不写入协议UID字段。观众备忘与普通人数统计不同，遮蔽UID仍可能识别人；发布目标不能依赖CSS隐藏私有数据。

普通对照先确认同一房间和场次；出现明显异常时，再按线索核对取数版本、事件/接收时钟、截止、分析阶段、排除策略或舍入，不要求每次固定全部条件。未知、零、部分已知和等待不能互换。尚未定案的政策差异保留说明，不为凑数改黄金文件。实用方法见[test.md](test.md)。

## 5. 进程、构建与外部操作

同一DATA_DIR由服务锁保护；服务恢复历史，临时房间不自动重开，平台持久目标按自身策略恢复。`run --until`持有Chrome、后端及ASR进程组，所有权写入runtime.json，到期或收到退出信号只清理自有进程组。`ASR_PYTHON`隔离模型环境，`APP_ROOT_PATH`与前端base配合。

多个采集实例可各自提供API和看板；gateway汇总配置的本机实例、读取归档，不是第三个采集器，不接管上游生命周期。远端HTML、JSON和浏览器定时刷新可以在静态托管上运行，不要求远端拥有采集进程或完整控制API。

构建、运行编排、上传、通知有不同副作用。现有某些同步入口串联这些步骤，不能把build或dry-run名称当作离线保证。`audience_publish`包含目标文件白名单、版本化产物、hash读回和原子切换；不能将其保证泛化到所有发布器。验证时对各实际入口分别检查写入、网络、进程和通知，不递归发布私有工作目录。

一次性欢迎脚本默认预演，持久自动化按明确配置和开关执行，点赞须明确目标与次数；各类账本及失败状态按对应操作手册处理，不把某一种发送状态机套到全部入口。采集、AI、发布与互动的启用状态分别保留，普通浏览不应开启其他能力。
