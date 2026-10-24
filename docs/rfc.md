# 技术设计

每个直播间独立 Chrome Page，播放器始终 `muted=true` 持续播放。页面内通过 Web Audio 采集独立音轨 PCM，后端共用一个常驻预热 ASR 模型逐段识别，结果通过 SSE 推向看板。

## 结构
- Python 3.12 + FastAPI 服务，React/Vite/TypeScript 看板，SQLite 存储。
- CDP 连接专用 Chrome；新页面 init script 在播放器创建和播放时保持静音。
- 主采集不再使用 `MediaRecorder`/WebM/ffmpeg，改为 `video.captureStream()` 独立音轨 → `AudioContext(16000Hz)` → `AudioWorklet` 收取 PCM。Worklet 输出置 0 不外放，播放器保持静音。
- 页面缓冲最多保留 75 秒 PCM，溢出显式统计为 `dropped_samples`；无每两分钟重置录音器的间隙。播放器节点更换或断流触发自动重连。
- UTC窗口取真实墙钟，PCM时长另存；覆盖率须90%～120%，媒体进度至少推进墙钟80%。源音轨须live、enabled且非mute；缺输入最多1600样本、源mute累计最多100ms，缓冲丢样则拒绝。无输入不能当作正常静音。播放等待有10秒外层超时，读取有窗口时长加15秒超时。
- 每次有效采集后连续失败预算归零；异常按 `RETRY_DELAY`（默认2秒）指数退避、上限30秒，连续失败达 `MAX_RECONNECTS`（默认5）才以error关闭房间。
- 停止房间立即移除其排队项、取消本房间inflight。模型结果返回后再次校验generation，房间已停止则不发布done；队列满时丢弃最旧待处理段以接纳新段，并显示过载。
- 单个JSONL ASR子进程常驻并提前预热，启动与单次推理分别30秒超时。通信或进程失败对同一WAV重试一次并重启子进程；合法JSON识别error也重试同段，但不重载模型。重启、重试、错误指标可见。

## 状态与存储
`rooms`存房间状态，`segments`存每房间递增序号与完整识别结果，`events`存递增SSE事件。`timeline`以(room_id,kind,source_key)去重，转写按seq更新原item，弹幕重复帧不重复插入；时间索引支持每房间最近80条查询。已有segments在升级时补齐时间线。

浏览器通过Playwright监听网页已有WebSocket，后端解码普通/zlib/Brotli消息，筛选DANMU_MSG，保留文本、显示名、服务器时间和去重键。注册发生在页面导航前，房间解析完成前有界缓存消息，随后绑定实际room_id与generation；停止后不再写入该会话的弹幕。

每条弹幕与每段转写各为一个item，按事件时间显示。转写事件时间取窗口结束时间，弹幕取服务器时间；另存接收时间。前端按房间增量处理SSE，200ms合批更新，不因每条弹幕重新请求整个state。卡片独立滚动，默认跟随底部，向上阅读时暂停。页面只显示最近记录，不删除数据库历史。

GET /api/export以JSONL导出全部或指定房间混合时间线；导出起始时固定最大ID，分批读取，不追逐新插入的消息。该导出不提供数据库事务级一致性快照保证。

结果包含UTC起止、PCM时长、墙钟时长、覆盖率、媒体进度、缺失/丢弃样本、源mute时长、RMS、VAD判定、队列等待和推理耗时。有效输入的低RMS为silent，VAD无人声为no_speech，过载丢弃为dropped，模型空文本为empty，截断为truncated，错误为error。RMS不提供语音/音乐分类。看板任务占用包含reconnecting和pending。

窗口级Silero v5.1.2 ONNX VAD在CPU后台线程运行，按generation隔离循环状态，只决定整段窗口是否送入ASR，不裁剪、不拼接，UTC窗口时间戳不变。模型由显式准备脚本带SHA256校验下载；服务不联网下载，缺模型或VAD故障显式降级为原音频识别。不额外排除歌声，演唱召回率尚待验收。近5分钟指标包括worker忙碌率、按音频秒数计的VAD及静音跳过比例、最老队列等待、队列音频时长和已返回样本平均窗口结束到结果延迟；另列失败段数与推理等待超时次数（含重试）。worker忙碌率包含重试/加载等待，不代表GPU利用率。

同一DATA_DIR通过服务锁保证单实例。服务启动恢复历史，旧房间改为stopped、未完成段取消，并清理本工具遗留临时WAV。SSE首包snapshot，随后按递增id提供事件，重连使用Last-Event-ID。历史段每房间查询最新20条，完整历史保留SQLite。

## 配置与运行
`ASR_PYTHON` 解耦推理环境；`APP_ROOT_PATH` 驱动后端 root_path 与前端 base。前端构建产物由 FastAPI 同源托管，绑定回环地址。默认不持久保存原始录音；队列中的 WAV 在完成、丢弃或关闭时清理。

`python -m bilibili_live_monitor run --until 2030-01-01T22:00:00-08:00` 为自管Chrome与后端进程组的本地控制进程，示例须换为未来带时区绝对时间。它拒绝占用端口和无效截止时间，将所有权写入 `DATA_DIR/runtime.json`，截止前优雅退出、到点清理自有进程组，覆盖ASR与Chrome Helper。不调用外部Background Process Manager；手动停止向manifest中的owner发送SIGTERM。

## 后续演进
长期运行再决定事件保留策略与定时开播。当前已实现窗口级VAD、连续失败重连和绝对截止退出，不将短测视为长期稳定性保证。
