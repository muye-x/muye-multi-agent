# Muye Gateway

Gateway 是唯一公网入口，只公开 Web 控制台、Control 的 `/api/v2/` 与 Main 的
`/agentMain/`。SubAgent、LLM、Data、Control internal API 和 Dashboard API 均在内部网络。

浏览器先调用 `/api/v2/auth/login` 获取短期 access token，refresh token 仅保存在 `HttpOnly`
Cookie。访问 `/agentMain/` 时，Nginx 经 Dashboard API 向 Control session introspection 验证
access token，再注入 `X-Muye-User-Id` 与独立 `MUYE_MAIN_CALLER_TOKEN`。因此不存在共享用户 API
Key，也不公开任何固定业务 Agent 路由。

部署凭据 `MUYE_CONTROL_GATEWAY_TOKEN`、`MUYE_GATEWAY_CONTROL_TOKEN` 与
`MUYE_MAIN_CALLER_TOKEN` 必须分别生成和注入，不能复用。使用 `scripts/render-nginx-config.sh`
渲染配置，`scripts/validate-nginx-config.sh` 校验 Nginx，`scripts/smoke-test.sh` 验证公网健康检查
和未认证 Main 请求被拒绝。
