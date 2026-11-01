# 直播助手：设计与运行合同

直播助手把已经采到的弹幕和语音整理成主播可操作的待办。原来的快照页仍是默认页，完整保留录制控制、时间线和导出。新页按房间展示未回应弹幕、灯牌观众与点歌单，人工可以改状态、恢复及撤销；每个判断都能展开查看证据。

本次实现和文档由 GPT-6.1 SOL 完成。产品运行时只调用 Ollama Cloud 的 `deepseek-v4.1-flash`，`think:false`。验证只用虚构数据和 mock，不访问正式浏览器、数据库、API 或外部模型。

## 架构与固定场次

8765 的现有服务继续独立运行。8766 sidecar 只拥有新的助手数据库和 HTTP 客户端，不导入现有应用的启动、关闭或浏览器管理逻辑。sidecar 提供新前端与 `/api/assistant/*`，其余 `/api/*` 逐块代理到 8765。请求方法、查询、流式 body、响应状态与端到端头保留；连接头过滤，SSE 不缓冲，取消和关闭都会释放上游连接。静态资源目录可配置。

独立 worker 以 SQLite `mode=ro` 读取现有 `events` 中的 `item` 事件。禁止实例化 `Store`：它的构造函数会恢复/修改现有运行状态。worker 保存读取 cursor、不可变事件副本、同 ID 的证据版本及 AI 完成 cursor。每次采集事务同时保存副本和读取 cursor；模型失败只记错误，不推进 AI cursor。重复读取不会重复生成待办。

启动参数固定逻辑 `session`、`room`、带时区 `since` 和采集器 `source-session`。采集器的 session_id 实际是 generation，不应误当跨重连的广播场次；本实现保守地固定 generation。重连换 generation 时暂停并要求操作者核对后另行处理，不自动吸收新 generation。原固定参数不能在同一助手 DB 中改写。重启使用原参数继续，不按启动时钟新建场次。每个 DB 服务一场，sidecar 可重复传入 `--db` 挂载多房间的独立场次库；每库一个 worker。前端选择已经配置的房间，不通过 UI 启动录制或创建场次。不同库必须使用不同逻辑 session 名。

worker 每轮读取上游 `/api/platform`，检查目标房间查播新鲜度、直播状态、采集来源和消息连接。上游失联、状态过期、换 generation 均暂停模型。两次确认下播后将本场永久标记 ended，后续开播不复活。可额外固定 owner PID/create_time；只检测其存活，不向该进程或浏览器发信号。sidecar 与 worker 分开启动，便于主 supervisor 统一回收。

## 状态、证据和并发

助手 SQLite 使用 WAL。`meta` 保存固定参数、cursor、运行状态、调用次数和错误；`observations` 保留每次被接受的事件及版本；`evidence` 保存各 timeline ID 最新版本；`cards` 保存弹幕、点歌和灯牌派生项；`history` 保存变更前后值用于撤销；`overrides` 保存昵称读音。原 timeline 与原始报文从不删除或改写。

弹幕到达即创建 pending 卡片，AI 可以标为 responded/dismissed。点歌单状态为 pending/queued/sung/rejected，标题须有证据。灯牌资格只由真实 medal.room_id 和明确 guard_level=0 或同场新鲜完整舰队快照推导；缺失和 unknown 不视为非舰长。榜单只在自身观测时点表达资格，不宣称刚点亮灯牌。已有任何点歌观察（包括拒绝、已唱）就不再进入“尚未观察到点歌”名单。人工完成灯牌项只改变显示状态，不改变真实资格。

AI 一轮动作在一个事务内校验：room/session、存在且版本仍一致的证据、目标卡片版本、工具与字段白名单、状态枚举。任何一项失败则整轮回滚。新建卡片 expected_version=0；编辑卡片须等于当前版本。上下文中没提供的卡片不允许模型编辑。人工变更产生 manual 锁，即使后续 AI 看到新版本也不能改；只有人工恢复/改状态能继续调整。撤销是新版本而非版本倒退，仍保留 manual 锁，避免旧推理覆盖。ASR 同 ID 的修订增加证据版本，等待 debounce 后用最新版本替换旧上下文；响应中的旧证据版本会拒绝。

## API 与 AI 工具

`GET /api/assistant/sessions` 列出此 sidecar 已配置场次。`GET /api/assistant/state?session=...` 返回三类卡片、证据及 worker 状态。`PATCH /api/assistant/cards/{id}` 接收 session、expected_version、status；`POST /api/assistant/cards/{id}/undo` 撤销最新变更，也需 expected_version。`PUT /api/assistant/pronunciation` 保存 session、原名和读音 override。409 表示版本冲突，前端刷新并保留错误提示；422 表示 schema/场次/状态错误。

模型只得到一项工具 `update_assistant`，参数为 actions 数组。每项包含 room_id、session_id、card_id、kind、expected_version、status、title、evidence（id/version）。不提供发送弹幕、网络、SQL、删除或任意执行工具。系统提示明确弹幕/转写都是待分析数据，不是指令；只根据可见证据判断回应、点歌和唱完，不因提到歌曲就断言已经唱过。空 actions 是合法完成，纯文本没有工具调用视为失败重试。

增量上下文包含尚未完成的观察、与增量直接相关的卡片、最近 80 个其他待办和最近 8 段语音，设单次事件数、上下文字节、输出 token、超时、最短调用间隔和每场最大调用次数。事件超过容量时按批次处理，单批超出字节上限时保留进度并报错，不静默截断证据。历史待办全部保留并可手动操作，但超过这 80 项的旧待办未必能仅凭新语音自动关联；这是当前有界上下文的覆盖限制。预算按尝试次数计入并持久化；达到上限暂停，原始事件仍采集。错误重试使用最短调用间隔，不紧循环。ASR debounce 只影响模型，不延迟弹幕入库与 UI 显示。

## 拼音与识别盲区

只在助手响应中生成昵称读音；原名独立保留，快照与欢迎文案不变。`pypinyin` 带声调、heteronym 模式；有多个读音的字保留原字并标记不确定，未收录字保留原字，英文不强加拼音。用户 override 优先。读音是显示辅助，不作为 UID 或身份匹配依据。ASR 错字、未采到弹幕、昵称重名、缺失 UID、过期榜单都可能影响判断；显示证据而不声称完整覆盖。卡片历史与证据保存在本地私有运行 DB。

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

## 离线验收结果

`tests/test_assistant.py` 与 `tests/test_assistant_proxy.py` 使用虚构事件和 HTTP mock 检查事件幂等、持久恢复、跨场拒绝、ASR 修订、人工版本保护、全事务回滚、工具契约、超时/预算/owner、下播不可复活，以及流式代理首块和断开资源回收。`frontend/tests/assistant.test.tsx` 检查状态修改、恢复/撤销、证据、读音 override 与冲突提示。

全套264项 pytest、ruff、前端9项测试与TypeScript/Vite构建通过。沿用欢迎tick测试并保留原补丁。尚无真实模型或直播语义准确率验收，未做生产切换；独立review、PR和deploy交还主线程。
