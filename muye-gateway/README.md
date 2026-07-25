# Muye 网关

Muye Gateway 保留为 Nginx 安全边界：TLS、Bearer Token、SSE 透传与服务端口收口都由它负责。运维控制台只监听回环地址，不通过公网网关反代。

| 路径 | 目标 | 鉴权 |
| --- | --- | --- |
| `/agentMain/` | 主 Agent | Bearer Token |
| `/api/v1/travel/` | Travel public profile | Bearer Token |

Order、SDK internal API、LLM 和运维控制台均没有公网路由。控制台只测试主 Agent 与 Travel public profile；Order 的 internal profile 仅供可信服务调用。

## Gateway Token

`MUYE_GATEWAY_API_KEY` 是生产 Nginx Gateway 的共享 Bearer Token。渲染 Nginx 配置时，
`scripts/render-nginx-config.sh` 会要求该变量非空，并将其写入仅部署环境可读的生成配置。外部
客户端访问 `/agentMain/` 或 `/api/v1/travel/` 时，必须携带请求头：

```http
Authorization: Bearer <MUYE_GATEWAY_API_KEY>
```

该变量不参与本地控制台运行。根目录 `main.py` 启动的是绑定到 `127.0.0.1:9870` 的
Dashboard API，不会启动 Nginx；服务状态探测和本地在线体验也不会读取或发送 Gateway Token。
因此，本地一键启动无需配置 `MUYE_GATEWAY_API_KEY`，生产部署 Nginx 时则必须配置。

Gateway Token 只保护经过 Nginx 的公网 allowlist 路由，不能保护直接暴露的 Agent、LLM 或
Dashboard API 端口。生产环境必须同时将上游服务限制在回环地址或可信私有网络，并通过防火墙
禁止公网直连。Token 应由密钥管理系统生成和注入，不得写入源码、提交到版本控制或展示在 Web
页面中。

## 启动

本地可使用仓库根目录的 `python main.py`。它会将控制台启动在
`http://127.0.0.1:9870/console/online.html`。控制台可与 Muye 对话、选择 Travel public
profile，并在浏览器 `localStorage` 中保存会话历史；SSE 工具和思考过程会随对话展示。该本地
入口用于开发和内网运维，不替代 Nginx。

生产环境启动控制台 API：

```bash
cd muye-gateway
python -m pip install -r requirements.txt
scripts/run-dashboard-api.sh
```

生产部署前执行 `cp .env.example .env`，填写 TLS、Gateway Token 与服务地址，然后渲染和
校验 Nginx：

```bash
scripts/render-nginx-config.sh
scripts/validate-nginx-config.sh
```

模板 [muye-gateway.conf.template](nginx/conf.d/muye-gateway.conf.template) 定义独立的 80 重定向与 443 TLS server block。证书、私钥和 Token 只能由部署环境注入，禁止进入仓库。

Dashboard API 默认仅绑定 `127.0.0.1:9870`。生产 Nginx 的 allowlist 仍只公开 `/agentMain/`
与 `/api/v1/travel/`，不会反代 `/console/`、LLM、Order 或 SDK internal endpoint。

## 脚本说明

- `scripts/run-dashboard-api.sh`：启动本地运维控制台 API，默认监听 `127.0.0.1:9870`，提供
  `/console/` 静态页面和服务状态接口。可通过 `MUYE_DASHBOARD_PYTHON`、
  `MUYE_DASHBOARD_HOST` 与 `MUYE_DASHBOARD_PORT` 覆盖默认值。
- `scripts/render-nginx-config.sh`：读取 `.env` 和 Nginx 模板，生成
  `build/nginx/conf.d/muye-gateway.conf`。脚本会校验网关 Token、域名及 TLS 证书配置，并将
  生成文件权限限制为 `600`。
- `scripts/validate-nginx-config.sh`：使用 `nginx -t` 校验渲染后的 Nginx 配置。默认检查
  `build/nginx/conf.d/muye-gateway.conf`，也可将配置路径作为第一个参数传入。
- `scripts/smoke-test.sh`：验证已部署网关的基础连通性和 Bearer 鉴权，包括健康检查、未鉴权
  请求返回 `401`，以及携带正确 Token 后可访问 Agent 健康检查。运行前需设置
  `MUYE_GATEWAY_BASE_URL` 和 `MUYE_GATEWAY_API_KEY`。
