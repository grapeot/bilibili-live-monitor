# PK 编码参数感知的离线读取

## 调用入口

```bash
# 只读检查：不创建目录、锁、缓存或恢复文件
bili-live media-codec inspect --source /PRIVATE/recordings/ROOM/CAPTURE-00016.mkv
# 执行离线准备：原片只读，必要时创建并验证独立恢复副本
bili-live media-codec prepare --source /PRIVATE/recordings/ROOM/CAPTURE-00016.mkv
```

`prepare` 返回 `media_input`，后续 ffmpeg 必须使用这个实际输入。剪辑、视频导出调用
`archive_media.resolve_media(source, expected_sha=original_sha, video=True)`；自动压缩
`recording_archive.encode` 也调用同一个 `prepare_video`。音频转写、试听和仅枚举来源的读取不启用视频恢复。

## 状态与边界

| 状态 | 处理 |
|---|---|
| `unchanged` | 正常分片，返回原片，不创建副本 |
| `repairable` | 文件头过期，从同一capture的前序码流恢复实际SPS/PPS，返回验收后的副本 |
| `mixed_parameters` | 分片内部参数变化，固定画幅剪辑/压缩拒绝处理，保留原片；须另做按参数边界拆分 |
| `unmanaged` | 非本录制器命名的素材沿用原读取与验证流程 |

范围：H264 MKV、单组SPS/PPS、最多回查32个同capture分片，新参数不能超过原CodecPrivate分配空间。
缺少历史、超出范围、数据损坏或恢复验收失败时明确报错，不猜参数、不重编码修复。
参数变动背景参考[录播姬的修复技术文档](https://rec.danmuji.org/dev/repair/)。

## 原片、副本与压缩

- 原片不覆盖、不改名、不删除。恢复副本放在原目录下 `.codec-media/`，不进入录制素材枚举。
- 副本绑定原片SHA、参数和策略版本，写入时按键互斥，保留原音视频包和时间戳；只更新容器参数，并将因此失效的祖先CRC声明等长替换为Void，完整解码及包哈希校验通过后才接纳。
- 压缩清单继续引用原片及旧SHA；归档完成记录另存 `media_input` 和 `codec_parameters`。前序原片已归档时，只从已验收归档记录读取后续解码所需参数。
- 恢复不等于允许清理原片。压缩仍须通过规格、收益、时长、帧数和成品解码检查；既有Trash授权与锁规则不变。
- 缓存校验失败或存在未验收同名副本时不覆盖。缓存不是源资料，不发布、不提交Git。

## 生效核对

离线工具每次启动加载代码，采集进程不需要重启。若周期压缩的 `cwd/PYTHONPATH` 指向冻结release，合并PR本身不会更新它：须更新该任务实际使用的离线模块，再只读核验import路径与压缩调用链。
保留周期任务已有配置与报告。旧版本记忆的 `source_decode_error` 跳过记录不能永久挡住新参数策略，但不可因此清空其他规格/收益跳过记录或重跑所有历史归档。
