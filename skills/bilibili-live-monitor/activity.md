# 唱歌 / 讲话候选时间轴与试听修订

将完整离线ASR按块投影到趋势图，帮助观察唱歌、讲话与入场/关注的时序。此工具不推断因果，初始分类只是文本启发式候选，不是声学模型或人工真值。长篇独白可能像歌词，歌词可能有“谢谢”等词，ASR也可能漏掉歌声；必须保留不确定和混合，不以背景音乐存在就认定主播在唱歌。

```bash
bili-live activity build --trends /private/trends/data.json \
  --asr /private/asr/full_transcript.jsonl --audio --output /private/review/v1
bili-live activity verify --output /private/review/v1
# 打开v1/index.html，试听、改下拉分类、导出labels.reviewed.json后：
bili-live activity build --trends /private/trends/data.json \
  --asr /private/asr/full_transcript.jsonl --labels /private/labels.reviewed.json \
  --audio --output /private/review/v2
```

输入ASR每行需包含 `room_id`、唯一`source_key`、带时区的`start_utc/end_utc`、`relative_start/relative_end`（原片内秒）、`source_path`、原片`sha256`和`text`；可包含`rms_dbfs`。两个绝对时刻的差必须与相对偏移差一致，源片不应还在写入。当前离线归一化脚本生成此格式；不要把未经时间映射的原生文件相对时间直接当墙钟。趋势范围和ASR房间必须一致，未覆盖区间显示灰色。

绿色为疑似唱歌，蓝色为疑似讲话，黄色为混合，灰色为不确定或无ASR。重叠的估算窗口分类冲突也显示灰色。初始约30秒粒度无法给出逐句精确边界；对口型、播放录音与本人真唱不能只凭转写区分。

`--audio`额外需要ffmpeg和ffprobe：校验原片SHA256后按每个源文件导出48kbps单声道MP3，最多两路并行；与ASR同样补齐容器时间戳空洞，原素材只读。九小时源音频大约生成200MB MP3，先确认预算和磁盘。省略该参数只生成图表/文字，试听按钮禁用。浏览器按块偏移播放，音频结束不会证明分类正确。

输出`index.html`、`activity.json`、`labels.json`、`manifest.json`，启用音频时还有`audio/`。页面可筛选和分页，修改分类立即重画四图色带；导出JSON后用CLI生成新目录，保留旧版。修订文件的`dataset_id`绑定房间、源片SHA、时间与偏移；不能跨场套用。`provenance=automatic_candidate/semantic_review/manual`区分启发式、外部文本语义复核候选与人工修改；语义复核文件沿用相同schema，通过`--labels`读取，runtime不自行调用模型。不能因为导出或验证成功就称全部人工验收。

启用音频时，同时检查块偏移落在导出的实际音频时长内（尾部容差0.2秒）。同一源片的墙钟起点必须一致；verify核验固定产物、全部引用音频及其指纹，不仅检查清单中恰好存在的条目。无音频构建只验证数据/图表，不声称已验证试听。

验收包括：范围与身份匹配、缺口保留、原片未改、verify通过；实际试听若干唱歌/讲话/混合/边界片段，核查修改后色带更新、导出再导入一致。它仍是供用户审阅的草图，未完成试听的部分必须标为候选。页面包含原转写、源信息与本地试听，默认仅本地，不自动上传或部署。分析时将来源缺口、推荐流量变化与内容变化分开，不能把色带与关注曲线相邻直接认定为因果。
