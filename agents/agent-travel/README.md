# 旅游 Agent

确定性旅行参考服务，基于 SDK 的 `ReActAgent` 实现。它只生成示例行程，不访问旅行供应商、
不执行预订，也不保存业务数据。默认监听 `127.0.0.1:8011`。

## 接口

服务同时启用 SDK 的 internal 和 public profile：

| Profile | 路径 |
| --- | --- |
| internal | `/health`、`/capabilities`、`/invoke`、`/invoke/stream`、`/cancel` |
| public | `/api/v1/travel/invoke`、`/api/v1/travel/invoke/stream` |

public profile 仅投影可展示的结果，由 Gateway 路由为 `/api/v1/travel/`；internal profile
供可信服务（例如 `agent-main`）调用。两种流式接口均使用 SDK 的 SSE 生命周期：
`session_start -> 中间事件 -> done -> session_end`。

## 运行

```bash
.venv/bin/python -m pip install -r agents/agent-travel/requirements.txt
cd agents/agent-travel
../../.venv/bin/python main.py
```

从当前目录构建独立镜像：

```bash
docker build -t muye-agent-travel .
docker run --rm -p 127.0.0.1:8011:8011 \
  --add-host=host.docker.internal:host-gateway \
  -e MUYE_SDK_MODEL_BASE_URL=http://host.docker.internal:9850 \
  muye-agent-travel
```

SDK 默认同时启用 `internal`、`public` profile，并使用 `/api/v1/travel` 作为 public 路径；
显式环境配置会覆盖这些服务默认值。独立启动前可执行 `cp .env.example .env`；本地 `.env`
已被忽略且不得提交。常用字段包括
`MUYE_AGENT_HOST`、`MUYE_AGENT_PORT`、`MUYE_SDK_API_PROFILES`、
`MUYE_SDK_PUBLIC_PATH` 和 `MUYE_SDK_MODEL_BASE_URL`。模型凭据仅由部署环境注入。
镜像内默认监听 `0.0.0.0`，直接运行源码时仍默认监听 `127.0.0.1`。

## 验证

从仓库根目录运行：

```bash
PYTHONPATH=agents/agent-travel \
  .venv/bin/python -m pytest -q agents/agent-travel/tests
```
