# 工作记录

## 当前状态
当前定位为多路直播文本监控：逐tab静音PCM与本地ASR，复用网页WebSocket采集弹幕，统一为SQLite混合时间线。30路各至少两个有效30秒窗口检查通过，包含恢复。全宽、全屏、自动跟随与历史阅读暂停通过浏览器检查。独立review与限定范围复核已完成，发现已逐项修复并补回归；短测不代表无间隙或长跑保证。

## Changelog
### 2026-10-24
- 新增独立录制、ASR和缩略图选项，默认原始音频与弹幕归档；35项离线测试、ruff和前端构建通过。
- 三路正式300秒实测，覆盖两路默认录音与一路音视频+ASR+缩略图；首轮18个文件封口及尾音频解码检查通过，120条弹幕持久化。后续来源波动导致一轮覆盖不足，保留失败证据，不算作通过。
- 独立review修复停止竞态、HLS签名更新和拒绝处理；100路一小时观察尚未启动。
- 最终修订复测：同一三来源正式300秒通过，18个分段与尾音频解码通过，85条弹幕、15段识别记录；禁ASR来源没有识别段。默认表单三个选项、ASR禁用状态与390px无横向溢出通过浏览器检查；两组自有进程均退出。
### 2026-10-22
- 建立中文项目文档、唯一根 skill 和公开仓库运行数据隔离。
- 选择 SSE 单向推送、REST 控制房间，第一版固定 30 秒窗口和静态封面。
- 离线 pytest 5 passed，ruff 检查通过，TypeScript 与 Vite 生产构建通过。
- 十个不同的实际直播间各完成两段约30秒音频处理，首批20段时长27.90～30.18秒，18段有文本、2段为空；没有队列过载。证据：`.local/live_smoke.json`。
- 首批20段最长队列等待3253毫秒、最长模型推理1325毫秒；该结果不包含30秒采集窗口，不代表端到端只需1秒。
- 十个房间均检查 muted=true、paused=false、音轨live，并继续完成第三、第四段与周期轮转。
- 双标签页合成音隔离验证通过：440/880Hz各自捕获到440.0/879.9Hz，另一频率分量分别为-71.1/-68.4dB；证据：`.local/isolation.json`。
- SSE实测在事件69后续传得到事件70；桌面与390px窄屏检查通过，刷新后保留十张卡片，窄屏无水平溢出。
- 实机重启后十路历史保留，状态均为stopped；随后重新开始十路，静态封面10/10加载成功。证据：`.local/restart_validation.json`、`.local/dashboard.png`。
- 公开文件隐私扫描零匹配，运行数据与本机.env保持忽略；尚未创建远端仓库或提交。
- 后续重复实测出现播放器自动暂停、部分窗口音频不足和一次ASR子进程提前退出，重复验收失败；记录分别见`.local/live_smoke_repeat.json`和`.local/live_smoke_final.json`，不覆盖首轮样例证据。
- 独立关键路径审阅指出墙钟时间与样本时长混用、play()等待无外层超时两项问题，均已修正并补测试；当前pytest 9 passed，ruff与前端构建通过。另一份广泛审阅未在阶段预算内完成，不计作验收依据。
- 实现录制期间暂停后恢复播放、最多两次reload、有界play/录音读取等待；受控双音源测试同时验证了暂停恢复和静音来源隔离。
- 增加墙钟窗口与PCM时长覆盖校验（90%～120%），不足的窗口触发明确异常，不作为完整切片。切换至带后台节流参数的独立Chrome后重新执行严格十路测试，仍未全部满足要求，证据`.local/live_smoke_strict.json`。
- 首轮最长推理/排队耗时仅代表已采到的音频，不能作为完整性或十路连续性证明；下一步应先定位媒体采集连续性，再做更长跑测。

## Lessons Learned
- 当前 Chrome 上 `video.muted=true` 后，`captureStream()` 仍能获取该播放器音轨，已完成单路静音转写验证。
- 连续 WebM 后续块需要保留本周期前缀，不应作为独立音频文件解码。
- 音量 RMS 不等于 VAD，无法区分语音与音乐，音乐幻觉仍需显式说明。
- B站页面未提供可用og:image或video.poster时，直播封面可从页面初始状态的roomInfoRes.data.room_info.cover读取，头像作为备选。
- SSE长连接可能拖延Uvicorn优雅关闭，服务启动需设有限的timeout_graceful_shutdown；重启后将中断的queued/transcribing历史标为cancelled。
- 未暂停与音轨live不足以证明音频完整：必须对照真实墙钟与PCM时长，不能只以切片数量或序号递增判定采集成功。
- 多房间高频弹幕不能复用每事件拉全量state的界面刷新方式；改为SSE增量合批、卡片按房间复用，避免消息量放大网络与渲染负载。

## 修复后验收（2026-10-22）
- 主采集从累积WebM解码改为独立AudioWorklet PCM，缓冲上限75秒，不再周期重置录音器。补充播放器进度、缺输入、音轨enabled与源mute累计检查。
- 停止立即清除本房间排队项，取消inflight，迟到结果校验generation；连续重连预算在成功采集后归零，默认5次、指数退避上限30秒。
- ASR提前预热，通信/进程失败同WAV重启重试一次，合法JSON识别error同段重试一次。看板显示相关指标；服务锁与重启遗留WAV清理已实现。
- 结束旧实验Chrome与旧服务后，以全新数据目录和专用Chrome自管重测。首次220秒等待时9路各满足六段，另一路媒体进度停顿后恢复，未将首次超时改记为通过。证据：`.local/overnight_20261022/ten_rooms_six_segments.json`。
- 使用同次运行的原始序号基线续测，10路均满足至少六个有效30秒窗口。证据：`.local/overnight_20261022/ten_rooms_six_segments_resumed.json`。停顿错误与恢复保留，不称连续无缺口三分钟。
- 最新双静音tab控制音源：440/880Hz分别捕获439.9/880.2Hz，另一源分量-72.0/-68.3dB，缺输入均为0，覆盖率1.014/1.040。原生enabled=false拒绝通过；受控mute事件累计203.7ms，后端按源断供拒绝。证据：`.local/overnight_20261022/isolation.json`。
- 最新19项pytest全部通过，`ruff check src tests scripts`与前端TypeScript/Vite构建通过。仅有依赖弃用警告。
- 自管`run --until`直接拥有Chrome/后端进程组，不使用Background Process Manager。已设置本次运行2026-10-22 22:00 PDT退出，所有权记录在`.local/overnight_20261022/runtime.json`。截止清理已用合成子进程树测试；实际到点清理须届时核对，当前不记为已发生。

## 混合时间线与30路（2026-10-22）
- 用户将目标从40/38路调整为30路，先结束旧实验Chrome/后端，再启动新会话，沿用SQLite历史与原22:00 PDT截止。
- 新增网页已有WebSocket弹幕监听、普通/zlib/Brotli解码，弹幕与识别各一个item混排，完整记录落SQLite；JSONL支持全部或单房间导出。
- 30路启动阶段未全部满足，证据`.local/overnight_20261022/thirty_rooms_initial.json`；沿原基线续测30路各至少两个有效30秒窗口通过，证据`thirty_rooms_resumed.json`。错误与恢复保留，不记为无间隙。
- 22项pytest、ruff、TS/Vite构建通过；真实弹幕已收到。全宽、上翻暂停跟随、回到底部、原生全屏与390px窄屏通过；证据`ui_timeline_qa.json`及私有截图。看板已在专用Chrome打开。
- 定位与公开根skill经Antigravity起草、主线程修正事实，见docs/formulation.md。独立code review进行中，未完成项不写作已验收。
- 首轮独立review的消息异常隔离、导出排序、迟到转写可见性、播放器就绪前绑定及滚动锚定问题已修复；24项离线测试与构建通过，小范围复核进行中。详见docs/review.md。
- 限定复核发现启动期间标题await与stop交错可令房间停在重连状态；取消绑定中的await、元数据返回后再校验generation，并新增启动stop回归。此前25/30、28/30等瞬时状态与失败证据保留，不冒充长跑无间隙。
- 最终25项pytest、ruff、TS/Vite构建通过。修复后已重启30路；首轮90秒严格窗口检查未全部满足，保留`thirty_rooms_review_fixes.json`，正在自动重连，不把此前版本的短测通过当作本轮通过。
- 实现PR #1已合入master，25测试与构建通过GitHub CI；文档先行、零审批者与管理员无绕过保护已回读确认。Context Infrastructure中文PR #123、英文PR #78和registry PR #56（第89项）已依序合并，文案由Antigravity起草、主线程核验。
- 首页面向推荐只显示约20个房间，沿实际/all链接找到69个新候选，补足30路。发现函数补/all并等待真实房间卡片，实测返回59个候选，仍不保证候选可播放。
- 完成视频可行性探索：3来源各3张JPEG连续变化，约5.2秒VP8采样可解26帧；隔离3路1fps MJPEG经Tailscale实际显示并持续更新，22:00退出另有自管owner。原始证据与画面在.local/overnight_20261022/video_probe/，不提交。结论见docs/video_feasibility.md。
- 最终同一修复版在补充实际可播放来源后，30路各至少两个有效30秒窗口及SSE续传检查通过，证据`thirty_rooms_final.json`；早先失败记录不覆盖。核查时30路均capturing、ASR重启/重试/过载为0。最近150段中138 done、5 empty、6 queued、1 transcribing，推理均值791.8ms、P95 1413ms，已完成段排队P95 6398ms；仅代表该时段样本。
