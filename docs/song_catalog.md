# 单主播静态歌曲目录

`python -m bilibili_live_monitor.song_catalog --config config.json` 从已完成场次生成静态歌曲搜索页、每首歌的详情页和演唱记录索引。网页点击不访问数据库；新场次完成后重新执行构建即可。

配置包含 `scope`（单主播稳定标识）、`label`、`home_url`、`output_dir`、`state_file` 和 `sessions`。每个场次指定稳定 `id`、显示 `label`、同站点 `url`，以及已完成的公开 `snapshot` 路径。旧版页面可改用 `html` 路径，仅读取已发布图表的歌曲命中区。

只索引已识别的本人演唱，不把歌名来源登记视为演唱记录。歌曲标识包含主播作用域；每次演唱单独留存，重建时按时间重合延续稳定标识。歌曲页保持静态URL，回填合并产生的旧演唱标识作为锚点别名保留。`aliases` 可显式将规范化歌名映射到同一个作品标识；不同作品不要共用映射。

输出 `index.html`、`index.json`、`<song-id>/index.html`。将输出目录部署为归档主页下的 `songs/`；把 `song_navigation.with_song_navigation` 注入目标场次页即可支持 `#song=歌名&at=时间戳` 定位。新版实时页面渲染器自动注入，旧版HTML可幂等追加。未知或被移除作品的旧页面保留更新提示。

目录状态属于私有构建输入，不发布配置、原始转写、账号身份或本地路径。首次生成前保存已有目标文件；部署应使用明确文件清单，并验证歌曲页及其目标场次链接。
