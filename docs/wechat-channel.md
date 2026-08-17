# 微信 Channel 接入

`muye-channels` 是微信 iLink 与 Muye Agent 的隔离服务。它负责扫码、凭据、消息
游标和回复关联；MainAgent 只接收 SDK 标准化的文本请求。

## 启用

复制 `muye-channels/.env.example` 为 `.env`，配置 PostgreSQL 连接并生成三个互不相同的随机值：
`MUYE_CHANNELS_CALLER_TOKEN`、`MUYE_CHANNELS_MAIN_TOKEN` 和
`MUYE_CHANNELS_ENCRYPTION_KEY`。后者必须为 base64 编码的 32 字节 AES-GCM 密钥。
同时将 `MUYE_CHANNELS_MAIN_TOKEN` 设置到 `agents/agent-main/.env`，并将 channels
caller token 设置到 Gateway 环境。渠道状态写入 PostgreSQL 的 `channel_*` 表。

本地启动器仅在 `MUYE_CHANNELS_ENABLED=true` 时启动该服务。生产 Compose 使用
`--profile channels` 启动；渠道绑定、游标、消息和投递状态写入 PostgreSQL。
绑定页面位于控制台的“微信”导航项，登录用户只能管理自己的一个活动微信绑定；再次确认二维码会替换旧绑定。

## 安全与语义

- iLink 凭据、二维码轮询令牌和 `context_token` 加密保存，绝不传入 Agent。
- 微信发送者只用于计算隔离会话 ID；Agent 授权使用扫码绑定者的 Control grants。
- 首版只处理微信文本消息；图片、语音、文件和视频均被忽略。
- 入站消息先去重持久化，再至多一次调用 MainAgent。Agent 或投递失败会记录服务端日志，但不会回复微信用户。
- 仅允许 `WECHAT_ILINK_ALLOWED_HOSTS` 中的 HTTPS iLink 地址，确认阶段的重定向同样受限。
- 当前实现以单实例服务为部署前提；多副本或跨地域部署前应将 state store 替换为带租约的 PostgreSQL 实现。
