# 持久房间自动化

## 生命周期

固定房间跨场守候复用平台的状态检查，不再由单场外部supervisor结束整套进程。两次新鲜离线结果确认下播；网络错误、未知状态不作下播。下播只结束本房间媒体与自动欢迎，本地API、历史时间线和账号管理继续提供服务。程序退出仍回收自己创建的Chrome，重启不清除开关、通知记录或账号选择。

## 配置与身份

`AUTOMATION_CONFIG` 指向本机私有JSON。配置的实际房间、文案、profile路径、发信命令不进入公开仓库。支持以下结构（虚构示例）：

```json
{
  "active_profile": "secondary",
  "profiles": {
    "secondary": {"label": "小号", "path": "/private/example/secondary"},
    "primary": {"label": "大号", "path": "/private/example/primary"}
  },
  "rooms": {
    "123456": {
      "label": "示例主播",
      "greeting": "欢迎 {names} {suffix}",
      "intro": "这里是示例唱歌直播间，请按直播间说明点歌。"
    }
  },
  "notification_command": ["/private/example/send-notification"],
  "assistant_databases": []
}
```

既有登录目录可直接登记为小号，不复制Cookie到另一账号。每个profile有独立的Chrome user-data-dir，切换时先撤销外发资格、暂停平台调度并结束当前采集，再回收旧Chrome并打开另一profile。切换会造成短暂采集缺口，不宣称无缝。账号密码、二维码与验证码由用户在官方页面输入。

## 自动欢迎

仅已配置房间默认开启，开关持久化且与AI推理开关相互独立。30秒内2个本场首次可识别UID触发，至少300秒冷却。禁用期间仍推进已见UID，不在重新启用时补发旧入场；按页面宽度拆分配置文案，分段间隔5秒。每段通过网页正常输入框发送，点击前和实际请求放行前再次核验账号、开关及新鲜直播状态。

已确认分段计入冷却。结果未知的发送暂停本场后续欢迎，不自动重发；下一场有独立会话身份。更换账号不重置冷却或已见UID。

## 更新房间介绍

房间介绍以主播最新确认的规则为依据，具体文案留在私有`AUTOMATION_CONFIG` JSON。当前进程启动时读取配置，改文件不会热加载，需通过已有部署流程应用。应用前检查没有正在发送的欢迎和正在执行的点赞；保存开关、同一场session、last_sent，不删库重置冷却。

用真实页面的`message_limit`和`welcome_messages`只读生成，逐条检查宽度及分段。介绍拆成两条时，加上单独的点名欢迎共三条，仍按既有间隔发送，不额外测试发送。应用后确认登录、同一场会话、冷却、录制和转写恢复。

## 信息与CTA文案轮换

每房间私有配置可新增 `welcome_variants` 列表；无配置沿用旧 `intro`。每项含非空唯一的 `id` 及非空的 `info`、`cta`。可选字符串映射 `copy_values` 通过 `{key}` 展开变量，缺失配置报错。直播时段只写已确认的时间，不推断结束时间。以下为虚构示例：

```json
{
  "copy_values": {"broadcast_hours": "每周六上午九点"},
  "welcome_variants": [
    {"id": "schedule", "info": "示例主播{broadcast_hours}开播。", "cta": "喜欢可以点个关注哦。"},
    {"id": "chat", "info": "欢迎来听歌聊天。", "cta": "想聊什么可以告诉主播哦。"}
  ]
}
```

每次现有欢迎触发时随机洗牌整组抽取，全部组用过才重新洗牌；有多组时跨轮避免立即重复。未用队列、上次 `id` 和配置指纹按房间持久保存，重启不重抽；配置变化时重建队列。

发送点名欢迎、信息、CTA三条，后两条各自完整发送。发送前验证全部组符合实际页面字数。维持原30秒2新人、300秒冷却、5秒间隔；未知发送不因换文案而重试。没有新增定时发送、唱完歌识别或实时AI。真实文案、主播及运行路径只保存在私有配置。

## 开下播邮件

仅真实观察到的状态转换生成通知；首次观察到在线发送“检测到开播”，首次观察到离线作为基线不发旧下播。通知包含房间、观察时间和链接，不发送弹幕、Cookie或录音。发信适配器从stdin接收JSON，成功以0退出并输出`{"id":"receipt"}`。适配器负责实际收件人、Pushover CC和稳定幂等键；任务ID来自房间、会话和事件。持久化意图后再外发，重启不重复已接受或结果未知的投递。无法证明投递的条目显示unknown，不能声称已收到邮件。

## 点赞与动态控制

复用网页`.chat-control-panel.__vue__.handleLikeBtnClick(true)`，遵守网页冷却，增加0～450毫秒随机间隔。只有显式指定房间和次数才执行；每次请求1～1000次，使用request_id去重，重复ID不新增任务。账号切换、失去登录、离线、页面改变时停止，不自动续跑。计数仅为通过网页检查的触发次数，不等于服务器最终计入数。

```bash
python scripts/automation_control.py status
python scripts/automation_control.py welcome 123456 off
python scripts/automation_control.py welcome 123456 on
python scripts/automation_control.py account primary
python scripts/automation_control.py login
python scripts/automation_control.py like 123456 20 --request-id example-request-1
python scripts/automation_control.py cancel-like example-request-1
```

相同操作也可以通过看板完成。所有API只部署于既有本机回环服务/私网入口；不新增公网接口。

## 验证

离线测试覆盖跨场/重启通知去重、未知状态、动态关闭、账号撤销资格、文案宽度、点赞原网页冷却和请求幂等。真实登录由用户完成；没有实际执行的点赞或邮件不记为已验收。
