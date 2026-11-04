# 杂谈场完成通知

`python -m bilibili_live_monitor.completion_notify --config private.json --dry-run` 验证已完成杂谈场的最终发布及通知前提；去掉`--dry-run`才发送。也可在既有归档同步的发布分支调用`notify(config)`，未启用时不发送。

配置包含`room_id`、`database`、`sessions_root`、`public_origin`及可选`public_path`（默认`/share/live-`）。`completion_notifications`包含`enabled`、`not_before`（带时区）、`display_name`、`ledger`（私有持久账本路径）和`command`（现有通知适配器参数数组）。配置和账本不得提交到公开仓库。

本模块仅用于`content_mode=talk`、`ai_enabled=false`的场次。节目已完成、快照冻结边界正确、远端两个产物与本地一致后，回复原下播邮件说明最终统计页面已发布；不声称进行过离线ASR或歌曲AI终审。

通过只读SQLite获取完整通知历史，不依赖HTTP接口的最近20条截断列表。适配器支持`--dry-run`并返回`valid=true`，发送后返回provider `id`。内置`broadcast_email`还支持`--lookup-receipt`回查真实RFC邮件标识。未知、发送中或已接受的账本记录不会自动重发；异常须先核对服务商状态再人工处理。
