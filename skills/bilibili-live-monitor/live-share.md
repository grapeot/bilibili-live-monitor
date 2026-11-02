# Bilibili 单场直播实时共享看板

本文件由根 `SKILL.md` 路由调用，定义单场直播实时共享看板的目标、契约、工具链与验收边界，只注册根技能。

## 1. 目标与职责边界

- **成功标准**：在不干扰独立录制器的前提下，为单场直播构建可验证的共享看板（仅公开 `public/index.html` 与 `public/snapshot.json`），呈现四维实时图表、分段统计、经信源核验的歌名及已授权公开昵称的点歌跟进。
- **与 `session-followup.md` 的分工**：`session-followup.md` 负责等待未开播场次、确认全新开播身份，以及下播后的离线复盘；本技能伴随独立录制器处理单场实时看板。未来场次须先确认真实新开播身份再执行 `init`。
- **状态、进程与副作用隔离**：不复用前序场次状态，不从进程重启推断直播身份。停止或冻结看板只停止看板更新与后续 AI 调度，不停止独立录制器，也不强杀已在处理的 AI 会话。
- `init`、`export`、`verify` 不联网、不启动采集或AI、不发布。`once --local-only` 会读取本机监控 API 并更新本地状态，但不调用AI或上传。它们均不发送消息、点赞或注册未来场次任务。
- **授权边界**：授权按单场次、可见性范围、昵称范围明确绑定；新主播不继承其他主播的公开分享或消息发送权限。

## 2. 工作区文件映射（`PRIVATE_DIR`）

| 路径 | 性质 | 职责与契约 |
| :--- | :--- | :--- |
| `config.json` | 私有 | 房间、数据库、起止时间、AI/发布参数及人工覆盖配置 |
| `state.json` | 私有 | 最后确认的截断点、健康样本、发布与调度状态 |
| `annotations.json` | 私有 | 带转写指纹的活动标注、点歌跟进与阶段 |
| `verified_songs.json` | 私有 | 来源登记；程序校验结构，操作者核对歌词语义 |
| `song_sources/` | 私有 | 支撑歌名核验的 Firecrawl/Tavily 全文搜索结果 JSON |
| `ai_input.json` / `full_context.json` | 私有 | 同场有界全量弹幕与最终转写上下文 |
| `public/index.html` | 公开 | 页面不含 AI/ASR 实现说明、内部路径、UID 或原始转写 |
| `public/snapshot.json` | 公开 | 字段白名单生成的公开快照；全工作区仅这两个公开文件允许上传 |

## 3. CLI 与脚本工具链

### 初始化工作区

```bash
bili-live live-share init --workspace PRIVATE_DIR --database DATABASE --room 123456 --label LABEL --start 2030-01-01T12:00:00+08:00
```

可选参数：`--hours`（默认及上限12）、`--api`、`--launcher`、`--model provider/model`、`--ai-session ID`、`--opencode-cwd DIRECTORY`、`--opencode-python PYTHON`、`--remote HOST:/absolute/path/`、`--url HTTPS_URL`、可重复 `--blind-gift-name NAME`、`--no-blind-gifts`、`--include-guard-lamps`。

创建新的配置及空标注/来源表；拒绝非空工作区。`--start` 必须带时区，程序将其转换为平台北京时间场次身份；先核对实际开播时间。`remote` 与 `url` 同时配置才有远端目标，初始化本身不会发布。

AI默认关闭，仅同时提供已存在的会话、完整模型名、OpenCode工作目录和解释器才启用。用安装环境的 OpenCode 提交技能核验路由与凭据；不自动创建新会话，不静默换模型，不启动或重启用户的 OpenCode 服务。模型可自行使用已安装的网页检索技能，部署机须具备该资源。

### 持续发布器与本地单次执行

- 经授权，用进程启动器设置准确的仓库 `cwd` 和解释器：`python scripts/live_share_watch.py run --config PRIVATE_DIR/config.json`。每分钟导出，每五分钟向同一AI会话追加复核；单工作区单实例，不另起重复自链。
- 本地取证：`python scripts/live_share_watch.py once --config PRIVATE_DIR/config.json --local-only`。它也会记录本场终态，不能拿正式状态文件模拟下播。
- 立即触发空闲AI复核：`python scripts/live_share_watch.py ai-once --config PRIVATE_DIR/config.json`；若有远端目标也会刷新发布，纯本地请加 `--local-only`。
- 停止看板：`python scripts/live_share_watch.py stop --config PRIVATE_DIR/config.json`。只冻结本场看板，不停止录制器。

### 本地导出、核验与歌名登记

- `bili-live live-share export --config PRIVATE_DIR/config.json`：按已保存截断点本地重建，支持终态后的修正，不联网、不上传。
- `bili-live live-share verify --config PRIVATE_DIR/config.json`：检查本地身份、截止点、页面与隐私字段，列出 `unpublished_songs`、未命名唱段、未审阅转写。结构通过不代表语义准确、未决项已解决或远端已更新。
- `bili-live live-share register-song --config PRIVATE_DIR/config.json --title TITLE --source SEARCH_PAYLOAD_JSON --result-index 0`：导入完整搜索结果，核对页面标题、HTTPS地址和正文，锁内合并来源表。索引从0开始。操作者仍须独立比对歌词/报幕；登记后执行 `export` 或 `once` 并检查可见结果。不要直接把任意模型文本塞入展示名单。

## 4. 礼物与点歌跟进规则

- 仅实际送礼触发；灯牌默认接受两个已支持的协议ID，排除已知大航海，`--include-guard-lamps` 可纳入。配置中的 `followup_rules.lamp_ids` / `lamp_name` 可指定其他已核协议值。
- 默认纳入盲盒，`--no-blind-gifts` 可关闭；重复的 `--blind-gift-name` 按原始盲盒名过滤，不能按抽中奖品名过滤。例如一个主播接受心动盲盒，另一个接受假面盲盒；未指定名称时纳入所有盲盒。同单盲盒结果按订单去重，普通灯牌保留批内项目。
- 送灯牌不等于首次入团；终身首次资格需另外取得历史证据，不能从本场数据推断。
- `config.json` 的 `followup_overrides` 以UID为键，允许 `excluded`、`done`、`awaiting_response`；后者必须有带时区的 `at`。人工修正优先，旧证据不能覆盖新人工边界。
- 同一观众再次送礼需新完成证据。现有 `done` 只表示点歌已收到/确认，不表示整首已唱；`song_requests` 分开记录 `accepted` / `performed` 及原始证据。可引用送礼前5分钟内的请求，但必须有最新送礼之后的有效证据；完成跟进引用的证据还须全部在首次送礼之后。

## 5. 看板视图与数据契约

- 四图依次为每分钟入场、上报在线人数、关注人数、每分钟礼物柱图。左右纵轴相同，横向滚动联动，打开与新数据刷新后移到最右侧。现场演唱候选绿色、其他无底色、无转写覆盖灰色，悬停显示歌名。
- 跟进仅显示授权公开昵称、时间、礼物计数、状态及已核验点歌曲目/阶段；UID、转写、证据、内部路径不进入公开产物。
- 唱歌/杂谈表含区间、净涨粉、礼物数和平台金额；统计至每段结束后30秒，未满尾窗明确提示，缺失不冒充零。
- 相邻统计窗可重叠，全场汇总必须独立去重，不能累加分段行；看过人数用平台最新累计值。平台金额不是主播实收，盲盒奖品标价不是消费金额，金瓜子、银瓜子、大航海与SC按其字段单位处理。

## 6. 主动调查与单场终态

- AI可读同场全量弹幕与最终转写，按时间、昵称变体、UID和歌词回溯；不局限于送礼者或末尾片段。每10–20个窗口合并保存，保留其他标注与人工修正，避免长思考耗尽输出而未落盘。
- 未知歌名主动查公开短歌词，优先Firecrawl取6条并保存完整正文，按安装的Tavily技能兜底；不将私有弹幕、UID、观众昵称送入搜索。
- 分开判断歌曲出现和现场演唱，考虑“放原唱、录音、对口型”等后文反证；文本研判不等于听音，不能认证唱功。标注必须匹配当前转写指纹。
- 已识别但未登记来源也会显示待确认。优先处理 `unpublished_songs`，不能把 `reviews.song` 有值当作页面已修好。
- 两次新鲜下播确认且录制停止、明确换场或到达配置截止时间，冻结最后确认的上界，停止发布器及后续AI调度。原始事件、时间线和转写同界，不能纳入下一场；查询错误不是下播。
- 保留未决状态，不强行标完成。终态后的更正使用本地 `export`，再按已有授权范围上传并核验；不重开旧场次或清掉停止标记。

## 7. 验收终点

- 用独立第二房间配置与合成/只读历史数据验收，不改当前录制器。初始化与本地工具不产生未授权外发。
- 公开字段检查通过；未决歌名和证据缺口有可查清单；至少一个原“已识别但未显示”的实际或合成案例通过登记、导出、页面可见性验收。
- 实际发布后核对精确URL和产物、桌面/移动端四图、悬浮歌名、联动滚动及真实刷新。HTTP200不代表内容正确；检查录像/转写实际推进和场次身份，不能以进程存在替代。
