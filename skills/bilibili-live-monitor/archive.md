# 直播录像归档

把已完成直播的 H.264 录像按原分段压为 HEVC 2.8 Mbps，保持原有分析结果可读，再按明确授权清理原片。真实路径、录像及运行回执只放私有工作区。

## 入口和结果

```bash
python -m bilibili_live_monitor archive inspect --sessions-root /PRIVATE/live_sessions
python -m bilibili_live_monitor archive plan --sessions-root /PRIVATE/live_sessions --session SESSION_ID
```

`inspect/plan` 只读已有 config、workflow、offline manifest 和文件元数据，不建目录、锁或缓存、不算媒体哈希、不启动转码。stdout 返回逐文件动作及原因。只有 `workflow.stage=completed` 的场次进入计划；WAV、恢复副本、缺失或改变的原片、未验收的同名输出不会自动处理。没有离线清单的历史录像须先明确归属，不把目录里所有文件视为可清理。

```bash
# run 不带 --execute 仍只出计划；默认最多处理一份
python -m bilibili_live_monitor archive run --sessions-root /PRIVATE/live_sessions \
  --session SESSION_ID --max-files 1 --execute
python -m bilibili_live_monitor archive verify --sessions-root /PRIVATE/live_sessions \
  --session SESSION_ID --max-files 1
```

首版配方是已验证的 H.264、1080p、30fps、8-bit BT.709、单路 AAC 输入，VideoToolbox HEVC Main 目标视频码率 2800k，音轨 copy。禁用软件回退，保持分段、帧率和色彩，不增加音频旁车，不合并或裁掉缺口。其他规格报告失败后保留原片，不静默套用。成品须比原片小，完整解码、时长、帧数、画面及音轨规格检查通过才算 archived。

原片 `recordings/ROOM/NAME.mkv` 对应 `recordings/ROOM/archive/NAME.mkv.mp4` 和同名 `.json` 完成记录。记录只保存路径、大小、mtime、配方、验收及原有清单字段，不计算新哈希、不改旧清单。一个 sessions-root 使用一把归档写锁；其 FFmpeg 子进程继承锁，父进程退出后不会因锁提前释放而启动第二个编码器。使用同一 sessions-root 执行历史回填和周期任务。

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

未来可由独立周期任务重复执行 `run --execute --max-files 1`，只扫描新 completed 场次；不要把存储维护插进原 finalization，也不调用 CRM、发布或通知。启用周期任务与自动永久清理需要用户明确授权。先限制一个编码器，在低负载时验证对录制和 ASR 的影响，不以硬件编码推断零资源影响。
