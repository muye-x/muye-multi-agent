# 微信 Channel 接入

`muye-channels` 是微信 iLink 与 Muye Agent 的隔离服务。它负责扫码、凭据、消息
游标和回复关联；MainAgent 只接收 SDK 标准化的文本请求。

## 启用

复制 `muye-channels/.env.example` 为 `.env`，配置 PostgreSQL 连接并生成三个互不相同的随机值：
`MUYE_CHANNELS_CALLER_TOKEN`、`MUYE_CHANNELS_MAIN_TOKEN` 和
`MUYE_CHANNELS_ENCRYPTION_KEY`。后者必须为 base64 编码的 32 字节 AES-GCM 密钥。
同时将 `MUYE_CHANNELS_MAIN_TOKEN` 设置到 `agents/agent-main/.env`，并将 channels
caller token 设置到 Gateway 环境。渠道状态写入 PostgreSQL 的 `channel_*` 表。

根目录 Compose 默认启动 `channels` 服务；渠道绑定、游标、消息和投递状态写入
PostgreSQL。`muye-channels/.env` 必须配置 `MUYE_CHANNELS_CALLER_TOKEN`、
`MUYE_CHANNELS_MAIN_TOKEN` 和 `MUYE_CHANNELS_ENCRYPTION_KEY`。
绑定页面位于控制台的“微信”导航项，登录用户只能管理自己的一个活动微信绑定；再次确认二维码会替换旧绑定。

## 生产验证

在 `muye-channels/.env` 中配置上述三个值，其中 caller token 必须与 Gateway 使用的值
相同，main token 必须与 Agent Main 使用的值相同。之后从仓库根目录执行：

```bash
docker compose up -d --build channels gateway
docker compose ps channels gateway
```

登录控制台后，选择导航栏的“微信”，点击“获取二维码”，用微信扫描页面显示的二维码。
手机确认后，页面应显示“已绑定”；若页面要求验证码，输入手机显示的验证码并确认。可通过
`docker compose logs -f channels` 查看绑定和消息轮询日志。

## 安全与语义

- iLink 凭据、二维码轮询令牌和 `context_token` 加密保存，绝不传入 Agent。
- 微信发送者只用于计算隔离会话 ID；Agent 授权使用扫码绑定者的 Control grants。
- 首版只处理微信文本消息；图片、语音、文件和视频均被忽略。
- 入站消息先去重持久化，再至多一次调用 MainAgent。Agent 或投递失败会记录服务端日志，但不会回复微信用户。
- 仅允许 `WECHAT_ILINK_ALLOWED_HOSTS` 中的 HTTPS iLink 地址，确认阶段的重定向同样受限。
- 当前实现以单实例服务为部署前提；多副本或跨地域部署前应将 state store 替换为带租约的 PostgreSQL 实现。
