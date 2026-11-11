# 直播录像归档

压缩前自动调用[PK参数感知读取](media-codec.md)：过期解码头先无损恢复到独立副本，再以该副本探测、计帧、转码。分片内画幅切换走专用归档路径，逐帧等比缩放、居中补边到1920×1440，不拉伸或裁切；普通固定画幅剪辑仍拒绝此类原片。完成记录保存实际输入、参数摘要、归一化前元数据及预期解码画布，原清单身份不变。

把已完成直播的 H.264 录像按原分段压为 HEVC 2.8 Mbps，保持原有分析结果可读，再按明确授权清理原片。真实路径、录像及运行回执只放私有工作区。

## 入口和结果

```bash
python -m bilibili_live_monitor archive inspect --sessions-root /PRIVATE/live_sessions
python -m bilibili_live_monitor archive plan --sessions-root /PRIVATE/live_sessions --session SESSION_ID
```

`inspect/plan` 只读已有 config、workflow、offline manifest 和文件元数据，不建目录、锁或缓存、不算媒体哈希、不启动转码。stdout 返回逐文件动作及原因。只有 `workflow.stage=completed` 的场次进入计划；支持清单中位于录制目录或本场 `offline/recovered/` 的素材。WAV、其他外部路径、缺失或改变的原片、未验收的同名输出不会自动处理。恢复副本归档不自动删除另存的损坏原件；没有离线清单的历史录像须先明确归属，不把目录里所有文件视为可清理。

```bash
# run 不带 --execute 仍只出计划；默认最多处理一份
python -m bilibili_live_monitor archive run --sessions-root /PRIVATE/live_sessions \
  --session SESSION_ID --max-files 1 --execute
python -m bilibili_live_monitor archive verify --sessions-root /PRIVATE/live_sessions \
  --session SESSION_ID --max-files 1
```

输入支持 H.264、逐帧最大1920×1440、方形像素、29.97/30/60fps、8-bit BT.709 和单路 AAC，VideoToolbox HEVC Main 目标视频码率2800k。稳定画幅保持原尺寸；混合画幅归一到上述画布，禁用滤镜重建以保留缓冲帧。禁用软件回退，保持分段、帧数、时间轴与色彩。音轨copy并显式保留开头非关键AAC包。其他规格报告失败后保留原片，不静默套用。成品须比原片小，完整解码、时长、帧数、逐帧画幅及音轨规格检查通过才算archived。

原录制流偶有重复视频时间戳。编码用微秒时间基准，将不递增时间戳推进到前帧后一微秒；事先遍历全部解码帧，只允许任一帧累计修正不超过1毫秒，超限保留原片，不把大幅时间倒退悄悄抹平。不通过固定帧率同步丢帧或插帧。容器猜测的平均帧率可能变化，验收仍要求实际解码帧数相同、时长误差不超过0.25秒。

新归档逐帧时间戳误差不超过2微秒；AAC包数与包内容摘要必须一致，音频PTS与有效展示跨度误差不超过1毫秒。MKV的AAC名义时长可能与相邻包PTS重叠，比较时按两者较短的有效跨度，不能以容器时长字段不同误判丢包。验收证据以压缩时间轴保存，cleanup时复用同一证据重验，失败不清理原片。

原片 `recordings/ROOM/NAME.mkv` 对应 `recordings/ROOM/archive/NAME.mkv.mp4` 和同名 `.json` 完成记录。记录保存路径、大小、mtime、参数、验收证据及原有清单字段，核对原SHA但不改旧清单。一个sessions-root使用一把归档写锁；其FFmpeg子进程继承锁，父进程退出后不会因锁提前释放而启动第二个编码器。使用同一sessions-root执行历史回填和周期任务。

## 原片退役与实际释放空间

```bash
# 默认只读预览；执行默认仅移入系统Trash
python -m bilibili_live_monitor archive cleanup --sessions-root /PRIVATE/live_sessions --session SESSION_ID
python -m bilibili_live_monitor archive cleanup --sessions-root /PRIVATE/live_sessions \
  --session SESSION_ID --execute
# 仅在用户明确授权永久清理这些原片时使用
python -m bilibili_live_monitor archive cleanup --sessions-root /PRIVATE/live_sessions \
  --session SESSION_ID --execute --permanent
```

清理前重验成品和原片元数据、检查写入者，只处理计划内已验收原片。Trash 不立即释放空间，不能自动清空整个废纸篓。永久清理只删除清单中的原视频，保留原始事件、离线清单、转写、标注和通知账本。清理失败只影响归档任务，不回滚或重跑直播发布。

原片在时，既有媒体读取保持原检查。原片不在时，离线 ASR、回填、试听导出和预告片读取固定位置的已验收归档；不拿原片旧哈希与有损副本比较。旧 ASR 缓存、窗口编号及清单身份不改，重做 ASR 的输出另记实际 `media_input`；压缩副本不承诺与原片字节或音频 PCM 完全相同。显式重做分析仍应使用新输出目录，避免覆盖已审核结果。

## 交付验收

先用隔离副本真实转码、移走或清理该副本，验证已有转写读取、归档解码、试听导出与剪辑输入解析。通过后才扩大到一场及历史批量。检查源变化、成品损坏、无音轨、同名输出、编码中断和并行启动都不会清理原片。

## 独立周期任务与历史回填

```bash
# 默认只读盘点；sessions-root 可重复指定，跨目录同源只处理一次。
python -m bilibili_live_monitor.archive_daily \
  --sessions-root /PRIVATE/live_sessions --sessions-root /PRIVATE/guest_sessions \
  --workers 2 --trash-originals
# 每日调度与一次性历史回填使用同一个命令；先取得明确执行授权。
python -m bilibili_live_monitor.archive_daily \
  --sessions-root /PRIVATE/live_sessions --sessions-root /PRIVATE/guest_sessions \
  --workers 2 --trash-originals --execute --report-dir /PRIVATE/archive_reports
```

`--workers` 支持1至5，默认2；增加并发前同时核查CPU解码/验收负载、VideoToolbox编码吞吐和实时采集健康，普通GPU利用率不能代表硬件编码器利用率。`--max-files` 可限制一次验收的数量。执行前按固定顺序取得所有根目录的既有归档锁，编码子进程继承全部锁；与手动归档及重叠周期互斥。CLI遇到已有归档持锁时返回 `skipped=true, reason=archive_writer_busy`，不重开一批；其他IO失败照常报错。同源身份冲突或仍有未完成场次引用时跳过。仅处理清单内的 completed 视频，不修改直播收尾、ASR、发布或通知状态。

每份成品先完成原有完整解码与规格验收，再检查旧清单身份和归档读取入口。仅显式 `--trash-originals` 才执行重验后Trash，不支持永久删除或清空废纸篓。失败逐文件记录并保留原片，其他作业继续；退出码1表示有失败，`complete=true`仅表示本轮结束，不代表全部成功。重复执行跳过已移走原片的归档，继续新录像或尚未成功的文件。每轮计划与进度保存在 `report-dir/<UTC时间>/`，`latest.json` 指向本轮状态。周期频率、真实路径及启用状态留在本机私有配置，新增调度须取得授权。
