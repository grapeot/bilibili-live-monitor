# 直播助手：设计与运行合同

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

模型只得到一项工具 `update_assistant`，参数为 actions 数组。每项包含 room_id、session_id、card_id、kind（chat/song/medal）、expected_version、status、title、evidence（id/version）。灯牌沿用统一schema的title字段，但服务端保留原值。不提供发送弹幕、网络、SQL、删除或任意执行工具。系统提示明确弹幕/转写都是待分析数据，不是指令；只根据可见证据判断回应、点歌、唱完及灯牌待办是否已处理，不因提到歌曲就断言已经唱过。空 actions 是合法完成，纯文本没有工具调用视为失败重试。

增量上下文包含尚未完成的观察、与增量直接相关的卡片、最近 80 个其他待办和最近 8 段语音；符合资格的灯牌待办也进入这一上下文和工具可操作范围。设单次事件数、上下文字节、输出 token、超时、最短调用间隔和每场最大调用次数。事件超过容量时按批次处理，单批超出字节上限时保留进度并报错，不静默截断证据。历史待办全部保留并可手动操作，但超过这 80 项的旧待办未必能仅凭新语音自动关联；这是当前有界上下文的覆盖限制。预算按尝试次数计入并持久化；达到上限暂停，原始事件仍采集。错误重试使用最短调用间隔，不紧循环。ASR debounce 只影响模型，不延迟弹幕入库与 UI 显示。

页面顶部的待分析观察数是 `observations` 中 `seq > ai_cursor` 的实际行数，最近观察更新是最新证据入库/修订时间。原始事件游标和 AI 观察序号计数范围不同，仅放在运行诊断中分别显示，不作为两个可比较的进度数。

## 拼音与识别盲区

只在助手响应中生成昵称读音；原名独立保留，快照与欢迎文案不变。`pypinyin` 带声调、heteronym 模式；多音字保留原字并显示候选读音，`candidates` 返回候选，整名标记不确定，不替用户选定读法。通过 Unicode 字名识别扩展区汉字；未收录的𠮷等字保留原字，`unknown` 返回该字，UI标记未知读音；英文不强加拼音。用户 override 优先。读音是显示辅助，不作为 UID 或身份匹配依据。ASR 错字、未采到弹幕、昵称重名、缺失 UID、过期榜单都可能影响判断；显示证据而不声称完整覆盖。卡片历史与证据保存在本地私有运行 DB。

口头赠歌、昵称别名与语音中的对象关联仍有缺口，模型可能误判某人点歌已完成。本次只纠正送礼资格，不实现别名记忆或歌曲识别架构，也不自动批量改写既有完成状态。主线程对已核实的误判用可撤销人工操作修正并留证。

## 启动与回滚范围

先构建前端；激活本 worktree `.venv`。由主线程注入 `OLLAMA_API_KEY`（只读取环境，不写日志、前端或 Git）。命令示例均为虚构参数：

```bash
npm run build --prefix frontend
python -m bilibili_live_monitor.assistant_cli serve --db .local/assistant.sqlite --port 8766 --upstream http://127.0.0.1:8765 --static frontend/dist
python -m bilibili_live_monitor.assistant_cli worker --db .local/assistant.sqlite --source-db .local/source.sqlite --room 123456 --session example-session --source-session example-generation --since 2030-01-01T12:00:00+00:00 --upstream http://127.0.0.1:8765
```

worker 首次启动登记场次，serve 可先启动（列表为空）。可加 `--owner-pid` 与 `--owner-create-time`，两者必须成对；模型/超时/预算等参数见 CLI `--help`。模型 URL 固定默认 `https://ollama.com/api/chat`，只有 runtime 请求携带 key。

默认参数：`--timeout 20 --interval 3 --debounce 2 --max-calls 1200 --max-events 20 --max-bytes 64000 --max-tokens 1800`。`--source-db` 必须是现有采集数据库文件，不是目录；`--source-session` 从主线程已核验的该房本场事件 `session_id` 传入。实现线程没有查询生产数据。重复启动同一库的 worker 会在文件锁处拒绝。原资源采样 `/assets/resource_metrics.json` 也透传至 8765，避免切换 sidecar 后丢失快照资源面板。

部署由主线程执行：在原 supervisor ownership 下启动两进程、验证助手状态，再把既有 Tailscale 8447 代理目标从 8765 切到 8766。无需重启录制、ASR 或 Chrome。回滚把代理切回 8765，回收仅两个新进程，保留助手 DB 以便审计。实现线程不执行这些生产操作。当前范围不包括自动回复、自动欢迎改写、跨场复用画像、多服务协调或对既有采集 schema 的迁移。

本次送礼语义修正部署只需构建前端并重启助手sidecar/worker，沿用同一助手库、room/session/source/since与owner配置；不重启8765、录制、Chrome或ASR。worker首次重算会从已保存的本场证据创建真实礼物卡、隐藏旧佩戴牌卡，cursor/history不删除、不回退。旧卡版本更新可能令在途手工操作返回409，刷新即可。上线应核验新卡gift_evidence、时间/数量和旧卡隐藏情况；无需为本补丁重放真实模型请求。

## 离线验收结果

`tests/test_assistant.py` 与 `tests/test_assistant_proxy.py` 使用虚构事件和 HTTP mock 检查事件幂等、持久恢复、跨场拒绝、ASR 修订、人工版本保护、全事务回滚、工具契约、超时/预算/owner、下播不可复活，以及流式代理首块和断开资源回收。`frontend/tests/assistant.test.tsx` 检查状态修改、恢复/撤销、证据、读音 override 与冲突提示。

PR19独立审阅后新增 `tests/test_assistant_review.py`，通过真实Store和协议构造旧→当前→后继来源、无session管理占位、旧完整榜单→购买上舰、乱序观测和语音关闭灯牌等场景；还验证旧版误熔断恢复与真正后继保护、medal人工锁和证据版本、异基数游标不作为UI进度对照。

送礼语义修正补充了真实Store/旧JSON和V2 protobuf回归：高等级佩戴牌不触发、普通/同名异ID/同ID异名/盲盒不触发、固定场次范围、未知guard等待后续entry/chat、舰长排除、去重与时间/数量、人工完成不复活、旧卡隐藏且history保留。旧回归也已改为先发生目标礼物，不再固化佩戴牌触发的错误预期。

G1另补12/16笔不同tid目标礼物与连续状态更新的真实Store→worker→工具回归，检查有界witness可完成操作、旧全量卡收敛、完整礼物审计及manual保护。修复后全套 **307项 pytest**、ruff通过；无UI改动，复用前轮 **15项前端测试（6 Node + 9 DOM）** 与TypeScript/Vite构建通过结果。沿用欢迎tick测试并保留原补丁。本实现线程未访问生产、未调用真实模型、未做生产切换；独立复核、PR推送和助手重启交还主线程。
