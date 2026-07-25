# 订单 Agent

仅供内部调用的确定性 LangGraph 参考服务，使用 SDK 的 `GraphAgent` 展示节点状态与流式协议。
它不会访问订单系统、不会支付、不会执行真实下单。默认监听 `127.0.0.1:8012`。

## 接口与边界

服务只启用 SDK internal profile，提供 `/health`、`/capabilities`、`/invoke`、
`/invoke/stream` 和 `/cancel`。流式响应遵循 SDK 生命周期：
`session_start -> 中间事件 -> done -> session_end`。

Order 不应配置 public profile，也不应由 Gateway 路由到公网。它只可由受信任的内部服务通过
`MUYE_AGENT_ORDER_URL` 调用。

## 运行

```bash
.venv/bin/python -m pip install -r agents/agent-order/requirements.txt
cd agents/agent-order
../../.venv/bin/python main.py
```

从当前目录构建独立镜像：

```bash
docker build -t muye-agent-order .
docker run --rm -p 127.0.0.1:8012:8012 \
  muye-agent-order
```

独立启动前可执行 `cp .env.example .env`。可使用 `MUYE_AGENT_HOST`、`MUYE_AGENT_PORT` 和
`MUYE_SDK_*` 配置监听地址、模型网关与 SDK 行为；本地 `.env` 已被忽略且不得提交，任何凭据
均由部署环境提供。
镜像内默认监听 `0.0.0.0`，直接运行源码时仍默认监听 `127.0.0.1`。

## 验证

从仓库根目录运行：

```bash
PYTHONPATH=agents/agent-order \
  .venv/bin/python -m pytest -q agents/agent-order/tests
```
