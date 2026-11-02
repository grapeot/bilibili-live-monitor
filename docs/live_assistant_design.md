# 直播助手：设计与运行合同

## AI默认关闭与显式授权

AI默认禁用。fresh DB及没有新授权记录的旧DB都返回enabled=false；旧字段、历史调用次数或此前运行状态不构成授权。worker每次启动和sidecar每次启动都会持久写入disabled，重启/升级不会继承上一次启用。只有用户明确要求启用后，才通过控制API、CLI或界面按钮设为true；开始实现、继续部署不等于允许调用模型。只读连接和普通API请求不重置同一生命周期内已经明确设置的开关。

`meta.ai_control`保存enabled、递增revision、变更时间与原因，`ai_control_audit`保留开关审计。关闭会递增revision：请求开始时记录的授权版本必须与工具应用事务内的当前版本一致。即使关闭后马上重新启用，旧请求也失效。检查覆盖多轮之间、实际模型请求前、工具应用前；关闭期间不调用模型，不执行返回响应中的别名/卡片写或finish，不推进AI cursor，也不把该响应落入工具对话/回执。此前已提交动作与pending run仍保留，重新启用后从同一run继续，并按幂等回执避免重复写入。

开关不替代直播状态：只有enabled=true且原直播/来源/owner门槛通过才运行AI。关闭期间worker仍可只读导入源事件、更新确定性待办和显示已存数据，人工状态、撤销及读音覆盖继续可用。确认下播或owner生命周期结束后，场次保持ended并关闭AI，拒绝再次启用，不自动随开播恢复。已发出的网络请求可能仍完成，但撤销授权后结果不得应用。

界面独立显示AI已关闭/已启用，按钮明确为“启用 AI 助手”或“关闭 AI 助手”，默认关闭；加载页面、切换房间和轮询不发送启用请求。该开关不属于模型可调用工具。

## 持久房间 Agent

每个固定房间/场次只有一个worker。明确启用后，新观察唤醒一轮分析；没有观察、没有未完成分析时不调用模型。每次模型响应可调用多个有限工具，工具结果回到同一conversation，模型继续查证或更新，直到finish_analysis/最终答复结束本批。每批工具轮数、每次调用超时、最短间隔、总调用预算都有上限。失败保留当前分析和AI cursor，已提交的工具结果随重启恢复；恢复分析前仍须本次生命周期的显式启用。

现有cards、manual锁、原始observations、history、读取与AI cursor全部保留。新增aliases、alias_candidates、alias_history、agent_runs、agent_calls、agent_exchanges、agent_audit表，均只写助手库。写动作与工具回执、完整assistant/tool配对轮次在同一事务提交；相同分析中的同名同参数成功写工具调用命中持久回执，不重复写。执行前在事务内重新读取持久run，已完成run不接受旧快照覆盖或再次执行。读工具每轮重新查询，失败回执不阻止修正条件后的重试。失败工具回传有限错误类别，模型可读最新状态重试；网络失败不保存原始响应正文或密钥。

工具包括read_state、read_aliases、viewer_events、search_transcripts、read_evidence、record_alias、update_assistant、finish_analysis。查询只读本场已导入观察，限制数量、时间和文本长度，返回覆盖/截断标记；原文可按ID/版本分页回看旧修订，但写入只接受当前版本。写工具携带room/session，证据必须曾提供给该分析且仍为当前版本，卡片使用版本锁及人工优先规则。

别名以UID为归属、规范化别名唯一。record_alias区分identity_evidence、relation_evidence及quoted_relation，保存原文短摘录、关系类型、证据版本及可信别名依赖。兼容旧evidence参数，但仍从当前原文验证关系：只接受本人明确自称，或原文把该UID的已知昵称/可信别名与新称呼直接肯定关联。否定、问句、推测、不同UID共用昵称或关联冲突不升级为事实；无明确关系的尝试保存在alias_candidates，状态unconfirmed。同名已归属另一UID时拒绝，不做机器翻译式关联。该确定性校验只覆盖明确句式，复杂语义可能留待确认，不声称通用语言理解。

每次向模型/UI展示或用于赠歌时，递归检查别名全部证据版本和依赖关系版本。ASR修订使旧关系成为pending_review，不再用于身份关联；缺少新关系字段的旧记录也待复核。原始证据和摘录保留，可用新证据及expected_version重新确认，旧关系版本另存alias_history。不自动撤销此前卡片，既有卡片仍由人工或后续有证据的动作处理。

保留弹幕点歌，同时支持主播主动赠歌：新song可携带song_origin=gift及recipient_uid，必须提供该UID的身份事件和含歌曲名、已知昵称/当前有效别名的转写；不能仅凭没有UID关联的歌曲提及建单。赠歌按语音来源、接收UID、歌曲名去重，同段语音可为多人或同人不同歌曲建单；弹幕点歌保持原来源去重。别名和未完成待办完整保存在DB，每次仅提供有界工作索引，不依赖旧conversation仍存在。真实粉丝团灯牌礼物规则、资格witness和manual/history保持原有约束。

自动上下文管理默认context_tokens=200000、max_bytes=800000、现场窗口120秒。字节与token独立比较，不取两种单位的min：完整请求的UTF-8序列化大小受max_bytes硬限；token启发估算为ASCII字符数/3.5加非ASCII字符数×1.3，向上取整。Ollama实际prompt_eval_count单独记录，并用实际量/上次基础估算×1.1校准下次估算，倍率限制为1至4。它不是DeepSeek tokenizer，也不代表已验证的真实token准确率；缺少usage时不编造实际量。

超过字节硬限、估算token阈值或上次实际usage阈值时触发裁剪：先移除120秒以前的临时完整轮次，再按当前字节/token估算移除密集窗口最旧完整轮次。system、工具定义、当前分析输入、有界工作索引保留；仅删除agent_exchanges临时对话，不删除observations、业务history、aliases或agent_audit。最小输入仍超限则进入context_capacity_paused并记录byte_limit/token_estimate原因，与网络waiting_retry分开；原始数据与AI进度保留，调整配置后可继续。

持久conversation只保存完整assistant工具调用及全部结果的配对单元，不保存半轮工具调用；中断后不会出现孤立tool结果或重新提交已完成写入。read_state只返回卡片，支持unfinished过滤和UID过滤；read_aliases独立按UID、关键词及关系状态过滤，使用offset/limit、total、next_offset分页。超过工具字节限制时保留total/matched、原offset及缩页建议，next_offset仍指向本次未返回的页，避免跳过数据。原文仍可用read_evidence分页读取。UI折叠展示关系可信状态、最近工具轨迹、compaction、独立字节/token估算和实际usage，不把待复核关系称作已查证。旧CLI参数兼容；仍由主线程只重启助手，8765采集API和录制不动。

直播助手把已经采到的弹幕和语音整理成主播可操作的待办。原来的快照页仍是默认页，完整保留录制控制、时间线和导出。新页按房间展示未回应弹幕、送灯牌待跟进与点歌单，人工可以改状态、恢复及撤销；每个判断都能展开查看证据。这里的灯牌专指用户实际赠送的“粉丝团灯牌”礼物，不指昵称旁佩戴的粉丝牌或等级。

本次实现和文档由 GPT-6.1 SOL 完成。产品运行时只调用 Ollama Cloud 的 `deepseek-v4.1-flash`，`think:false`。验证只用虚构数据和 mock，不访问正式浏览器、数据库、API 或外部模型。

## 架构与固定场次

8765 的现有服务继续独立运行。8766 sidecar 只拥有新的助手数据库和 HTTP 客户端，不导入现有应用的启动、关闭或浏览器管理逻辑。sidecar 提供新前端与 `/api/assistant/*`，其余 `/api/*` 逐块代理到 8765。请求方法、查询、流式 body、响应状态与端到端头保留；连接头过滤，SSE 不缓冲，取消和关闭都会释放上游连接。静态资源目录可配置。

独立 worker 以 SQLite `mode=ro` 读取现有 `events` 中的 `item` 事件。禁止实例化 `Store`：它的构造函数会恢复/修改现有运行状态。worker 保存读取 cursor、不可变事件副本、同 ID 的证据版本及 AI 完成 cursor。每次采集事务同时保存副本和读取 cursor；模型失败只记错误，不推进 AI cursor。重复读取不会重复生成待办。

启动参数固定逻辑 `session`、`room`、带时区 `since` 和采集器 `source-session`。采集器的 session_id 实际是 generation，不应误当跨重连的广播场次；本实现保守地固定 generation。重连换 generation 时暂停并要求操作者核对后另行处理，不自动吸收新 generation。原固定参数不能在同一助手 DB 中改写。重启使用原参数继续，不按启动时钟新建场次。每个 DB 服务一场，sidecar 可重复传入 `--db` 挂载多房间的独立场次库；每库一个 worker。前端选择已经配置的房间，不通过 UI 启动录制或创建场次。不同库必须使用不同逻辑 session 名。

来源判定保存每个 generation 的首次源事件序号及最早接收时间。指定来源之前已出现的 generation（包括 since 之前的来源）及其迟到事件不触发熔断；缺少 session 的 startup-gap 管理记录也不触发。只有首次序号在指定来源之后、最早接收时间也不早于指定来源和 since 的其他 generation 才暂停本场；指定来源的后续迟到记录不会解除真正后继的保护。旧版本只有布尔 mismatch 的助手库，会从源库已消费事件只读重建这份分类，无需重置 cursor 或重放派生卡片。

worker 每轮读取上游 `/api/platform`，检查目标房间查播新鲜度、直播状态、采集来源和消息连接。上游失联、状态过期、换 generation 均暂停模型。两次确认下播后将本场永久标记 ended，后续开播不复活。可额外固定 owner PID/create_time；只检测其存活，不向该进程或浏览器发信号。sidecar 与 worker 分开启动，便于主 supervisor 统一回收。

## 状态、证据和并发

助手 SQLite 使用 WAL。`meta` 保存固定参数、cursor、运行状态、调用次数和错误；`observations` 保留每次被接受的事件及版本；`evidence` 保存各 timeline ID 最新版本；`cards` 保存弹幕、点歌和灯牌派生项；`history` 保存变更前后值用于撤销；`overrides` 保存昵称读音。原 timeline 与原始报文从不删除或改写。

弹幕到达即创建 pending 卡片，AI 可以标为 responded/dismissed。点歌单状态为 pending/queued/sung/rejected，标题须有证据。送灯牌资格由本场固定 room/source/since 内的结构化送礼事件确定，不读取 `medal.level`、`medal.room_id` 或文本关键词。

礼物匹配必须同时满足 `kind=gift`、`gift.event_type` 为 `SEND_GIFT` 或 `SEND_GIFT_V2`、字符串 `gift_id='34001'`、精确 `gift_name='粉丝团灯牌'`、正整数 `num`，且不存在非null的盲盒来源字段（实现拒绝任何非null `blind_gift`，包括空对象）。名字相同但ID不同、ID相同但名字不同、假面盲盒结果、黑猫提灯、普通礼物、仅在文字中提及均不计入。V2先经现有protobuf解析器展开为原子礼物，使用同一规则；不把COMBO_SEND累计通知重复计数。未知协议类型或缺少必需身份字段保守不计。

发送者用明确UID关联，不按昵称匹配。优先顶层 `sender_uid`，缺失时允许礼物内的字符串UID；两处均有值但冲突则拒绝，显式 unresolved 身份也拒绝。礼物到达时 guard 未知可先保存观察，后续同UID的entry/chat明确guard或新鲜完整榜单再补资格；缺失和unknown不视为非舰长。

身份和明确 guard 字段按 `received_utc`（缺失时回退 `at_utc`）的实际观测时间选最新值，不能按导入/ASR修订时间覆盖。舰队快照按 `observed_utc` 选取，同一 UID 只有比明确 guard 事件更新的快照才可覆盖；时间相同保留事件证据。完整榜单缺席才可证明该次观测下非舰长，部分榜单缺席不更新资格。因此旧完整空榜不能覆盖其后的 `GUARD_BUY`。卡片只附加实际用于资格判断的榜单证据。

同UID本场聚合成一张 `medal:gift:<uid>` 卡片，`qualification_rule=fan_club_gift_v1`。`gift_evidence` 保留每条目标礼物的ID/version；`gift_quantity`按去重后原子事件的num累计，`first_gift_at/last_gift_at`为最早/最晚接收时间（缺失才回退事件时间），UI明确显示“首次收到”而不是声称精确送出时刻。原始事件时间仍在展开证据内。已有卡片收到第二次礼物只更新数量/证据，不重置status或manual锁。

完整礼物审计和一次AI判断的证据分开：`gift_evidence`不截断；`qualification_evidence`只保留首份有效礼物及最新必要guard/identity，去重后最多3条。卡片主`evidence`由这组资格witness加最近最多9条`status_evidence`组成，总数不超过工具既有12条限制。AI只须回传资格witness并附本次语音，不须复制每一笔送礼。重算保留有界状态证据；更早状态证据仍在history/observations，不删除原始记录。API仍解析完整gift_evidence供审计，但模型上下文只显示gift_receipt_count、统计和有界witness，不传全量礼物引用清单。

待跟进的显示条件是目标礼物已观察、已确认非舰长、且尚未完成跟进。仅观察到点歌不等于完成：song pending/queued仍可留在此栏；同UID有sung/rejected的终态点歌则排除。人工或AI将本卡设为done/dismissed后默认隐藏，显示历史开关可查看仍符合送礼/guard资格的已处理卡。恢复状态由人工决定。

旧版因佩戴粉丝牌生成的 `medal:<uid>` 不转换为送礼卡：重算将其eligible置false，保留原状态、manual锁、证据与history；新规则卡使用独立ID，避免把旧错误判断转移到真实送礼。前端也要求新规则标记，旧卡即使尚未来得及重算也不展示。

人工和 AI 都可把已有送礼待办设为 pending/done/dismissed，只改变显示状态，不决定送礼资格。AI 不可新建灯牌项或改写昵称、标题、eligible、gift/guard字段；必须保留实际礼物和观众来源证据，再附加跟进/完成的语音证据。`status_evidence` 独立保存判断证据，后续资格刷新仍保留；manual 锁、证据版本及卡片版本校验与其他卡片相同。

AI 一轮动作在一个事务内校验：room/session、存在且版本仍一致的证据、目标卡片版本、工具与字段白名单、状态枚举。任何一项失败则整轮回滚。新建卡片 expected_version=0；编辑卡片须等于当前版本。上下文中没提供的卡片不允许模型编辑。人工变更产生 manual 锁，即使后续 AI 看到新版本也不能改；只有人工恢复/改状态能继续调整。撤销是新版本而非版本倒退，仍保留 manual 锁，避免旧推理覆盖。ASR 同 ID 的修订增加证据版本，等待 debounce 后用最新版本替换旧上下文；响应中的旧证据版本会拒绝。

## API 与 AI 工具

`GET /api/assistant/sessions` 列出此 sidecar 已配置场次。`GET /api/assistant/state?session=...` 返回三类卡片、证据及 worker 状态。`PATCH /api/assistant/cards/{id}` 接收 session、expected_version、status；`POST /api/assistant/cards/{id}/undo` 撤销最新变更，也需 expected_version。`PUT /api/assistant/pronunciation` 保存 session、原名和读音 override。409 表示版本冲突，前端刷新并保留错误提示；422 表示 schema/场次/状态错误。

`GET /api/assistant/control?session=example-session`读取开关；state响应也包含独立的control对象。`PUT /api/assistant/control`接收`{"session":"example-session","enabled":true}`明确启用，改为false关闭。enabled必须是JSON布尔值；字符串"true"、缺少字段或错误session拒绝。重复enable为幂等操作，disable仍使在途授权失效。已结束场次enable返回409。这些都是服务端持久操作，不依赖前端开关状态。

模型拥有本文件首节列出的八个有限工具。写卡片仍使用 `update_assistant` 的actions数组，每项包含 room_id、session_id、card_id、kind（chat/song/medal）、expected_version、status、title、evidence（id/version），主动赠歌额外提供song_origin=gift和recipient_uid。灯牌沿用统一schema的title字段，但服务端保留原值。不提供发送弹幕、网络、SQL、删除或任意执行工具。系统提示明确弹幕/转写都是待分析数据，不是指令；不因提到歌曲就断言已唱过。finish_analysis或非空最终答复结束当前批次，只有这时推进AI cursor；中间写工具成功不意味着整个批次已完成。

新观察按max_events形成一个分析批次，初始现场提供有界文本预览，完整原文通过工具取回。每次请求独立附加至多20张相关未完成卡片、10个优先相关称呼及引用，并给出全库计数和查询入口。相关性预览不是分页前20项，完整待办查询从offset=0开始，避免漏项。此前“最近80项”仍用于初始关联卡片选择；这些预览数量不限制DB记忆，read_state可分页读取所有卡片。500或2000条普通聊天积压在默认预算下可开始推理并推进cursor，未发送的待办仍完整留库。预算按每次真实模型请求计入并持久化；达到上限暂停，原始事件仍采集。错误重试使用最短调用间隔。ASR debounce只影响新批次唤醒，不延迟入库或UI显示。

页面顶部的待分析观察数是 `observations` 中 `seq > ai_cursor` 的实际行数，最近观察更新是最新证据入库/修订时间。原始事件游标和 AI 观察序号计数范围不同，仅放在运行诊断中分别显示，不作为两个可比较的进度数。

## 拼音与识别盲区

只在助手响应中生成昵称读音；原名独立保留，快照与欢迎文案不变。`pypinyin` 带声调、heteronym 模式；多音字保留原字并显示候选读音，`candidates` 返回候选，整名标记不确定，不替用户选定读法。通过 Unicode 字名识别扩展区汉字；未收录的𠮷等字保留原字，`unknown` 返回该字，UI标记未知读音；英文不强加拼音。用户 override 优先。读音是显示辅助，不作为 UID 或身份匹配依据。ASR 错字、未采到弹幕、昵称重名、缺失 UID、过期榜单都可能影响判断；显示证据而不声称完整覆盖。卡片历史与证据保存在本地私有运行 DB。

本版支持有原文证据的昵称别名和主播语音主动赠歌，不做音频旋律识别、声纹身份识别或凭机器翻译猜昵称。语音缺失、歧义称呼或多个同名观众仍可能导致无法关联；模型对“已经唱完”的语义判断也不保证准确。没有明确身份和歌曲语音证据时拒绝新建主动赠歌。既有误判不自动批量改写，由主线程可撤销人工修正并留证。

## 启动与回滚范围

先构建前端；激活本 worktree `.venv`。由主线程注入 `OLLAMA_API_KEY`（只读取环境，不写日志、前端或 Git）。命令示例均为虚构参数：

```bash
npm run build --prefix frontend
python -m bilibili_live_monitor.assistant_cli serve --db .local/assistant.sqlite --port 8766 --upstream http://127.0.0.1:8765 --static frontend/dist
python -m bilibili_live_monitor.assistant_cli worker --db .local/assistant.sqlite --source-db .local/source.sqlite --room 123456 --session example-session --source-session example-generation --since 2030-01-01T12:00:00+00:00 --upstream http://127.0.0.1:8765
```

两进程都启动后AI仍关闭。仅在用户明确授权启用时执行以下CLI操作；启动worker/sidecar后再次执行才对本次生命周期生效。control只修改已有助手库的开关，不启动worker，不改变固定场次或原始采集：

```bash
python -m bilibili_live_monitor.assistant_cli control --db .local/assistant.sqlite --session example-session --enable
python -m bilibili_live_monitor.assistant_cli control --db .local/assistant.sqlite --session example-session --disable
```

`--enable`与`--disable`必须明确提供其一且互斥；错误session或不存在的助手DB拒绝。部署验收默认保持关闭，不为测试上线主动调用enable。

worker 首次启动登记场次，serve 可先启动（列表为空）。可加 `--owner-pid` 与 `--owner-create-time`，两者必须成对；模型/超时/预算等参数见 CLI `--help`。模型 URL 固定默认 `https://ollama.com/api/chat`，只有 runtime 请求携带 key。

默认参数：`--timeout 20 --interval 3 --debounce 2 --max-calls 1200 --max-events 20 --max-bytes 800000 --max-tokens 1800 --max-tool-turns 8 --max-tools-per-turn 8 --context-tokens 200000 --recent-seconds 120 --tool-result-bytes 16000`。旧显式参数继续有效；max_bytes是独立字节硬上限，context_tokens是输入token估算阈值。max_tool_turns是单批持久累计上限，触及后须调整配置或人工处理，不会后台无限重开。`--source-db`必须是现有采集库文件；固定scope和owner参数不变。原资源采样仍透传至8765。

迁移只在助手库CREATE TABLE IF NOT EXISTS，不修改原表或重置cursor。旧运行没有active_agent_run时从未完成观察开始，旧待办和manual/history原样带入。conversation保存配对单元，工作索引每次请求重新载入。旧别名保留记录，但缺少关系证明时标待复核，不直接用于赠歌。前端可在折叠区查看工具名、原因、证据关联审计、命中幂等回执、compaction及截断次数；不需要重启原采集进程。

部署由主线程执行：在原 supervisor ownership 下启动两进程、验证助手状态，再把既有 Tailscale 8447 代理目标从 8765 切到 8766。无需重启录制、ASR 或 Chrome。回滚把代理切回 8765，回收仅两个新进程，保留助手 DB 以便审计。实现线程不执行这些生产操作。当前范围不包括自动回复、自动欢迎改写、跨场复用画像、多服务协调或对既有采集 schema 的迁移。

本次送礼语义修正部署只需构建前端并重启助手sidecar/worker，沿用同一助手库、room/session/source/since与owner配置；不重启8765、录制、Chrome或ASR。worker首次重算会从已保存的本场证据创建真实礼物卡、隐藏旧佩戴牌卡，cursor/history不删除、不回退。旧卡版本更新可能令在途手工操作返回409，刷新即可。上线应核验新卡gift_evidence、时间/数量和旧卡隐藏情况；无需为本补丁重放真实模型请求。

## 离线验收结果

`tests/test_assistant.py` 与 `tests/test_assistant_proxy.py` 使用虚构事件和 HTTP mock 检查事件幂等、持久恢复、跨场拒绝、ASR 修订、人工版本保护、全事务回滚、工具契约、超时/预算/owner、下播不可复活，以及流式代理首块和断开资源回收。`frontend/tests/assistant.test.tsx` 检查状态修改、恢复/撤销、证据、读音 override 与冲突提示。

PR19独立审阅后新增 `tests/test_assistant_review.py`，通过真实Store和协议构造旧→当前→后继来源、无session管理占位、旧完整榜单→购买上舰、乱序观测和语音关闭灯牌等场景；还验证旧版误熔断恢复与真正后继保护、medal人工锁和证据版本、异基数游标不作为UI进度对照。

送礼语义修正补充了真实Store/旧JSON和V2 protobuf回归：高等级佩戴牌不触发、普通/同名异ID/同ID异名/盲盒不触发、固定场次范围、未知guard等待后续entry/chat、舰长排除、去重与时间/数量、人工完成不复活、旧卡隐藏且history保留。旧回归也已改为先发生目标礼物，不再固化佩戴牌触发的错误预期。

G1另补12/16笔不同tid目标礼物与连续状态更新的真实Store→worker→工具回归，检查有界witness可完成操作、旧全量卡收敛、完整礼物审计及manual保护。Agent升级在 `tests/test_assistant_agent.py` 通过真实Store+mock Ollama HTTP完成14轮查证、别名持久化、跨重启/裁剪的主动赠歌与唱完处理；另测配对事务回滚、写工具重放、人工竞争、逐轮预算/超时、旧稿只读、新输入顺序、200K/120秒及密集窗口裁剪。沿用欢迎tick与前端拼音测试，不访问真实模型或生产环境。最新全套结果记录在working.md；独立复核、PR推送和助手重启交还主线程。

R1–R4/N1–N2审阅后新增 `tests/test_agentic_review.py` 的21项回归：500/2000积压连续推进、关系否定/无关/歧义与自称、短摘录关系校验、ASR修订停用与重确认审计、别名链依赖失效、150别名分页完成、超长页缩小后可读取、容量暂停恢复、usage校准与字节/token独立、同段多观众赠歌、旧快照finish状态一致。前端同步验证待复核标记和独立预算显示；合成测试不等于真实模型的语义准确率或线上验收。

显式启用控制另增 `tests/test_assistant_control.py` 的13项回归：fresh/旧库默认禁用、禁用时导入与人工修改、请求前和轮次间撤销、另一个数据库连接关闭并重新启用后旧响应仍被拒绝、重启不继承启用且续跑不重复写、禁用状态直接工具应用拒绝、API严格类型/场次/sidecar重启、请求前及在途确认下播后结束、CLI显式开关。此前所有运行worker的模型mock测试在构造worker后显式enable；前端新增默认off且只有点击才发控制请求的回归。
