# Muye Main Agent

`agent-main` 是 Muye Multi-Agent 的主编排服务。它接收用户对话，协调 `muye-llm`、网页与
子 Agent 工具，并通过 HTTP SSE 返回模型正文和执行过程。服务默认仅监听
`127.0.0.1:9860`；需要外部访问时应经由 `muye-gateway` 的 allowlist 和 Bearer Token。

## 运行

在仓库根目录完成依赖安装与环境配置后启动：

```bash
cd agents/agent-main
../../.venv/bin/python main.py
```

单独部署此服务时，可在当前目录使用 `requirements.txt` 安装依赖；其中 SDK 固定从公开 GitHub
仓储安装，不依赖本地相邻的 `sdk/` 目录。

根目录也可按依赖顺序启动全部服务：

```bash
.venv/bin/python main.py --timeout 20
```

独立启动前可执行 `cp .env.example .env`。模板列出模型网关、数据库、检索和子 Agent 的常用
配置，并默认使用 SQLite、关闭外部记忆服务。包含 API Key、Token 或数据库连接串的 `.env`
仅供本地运行，不得提交。
常用服务地址为 `MUYE_LLM_BASE_URL`、`MUYE_AGENT_TRAVEL_URL` 和
`MUYE_AGENT_ORDER_URL`；实际可用配置以 `config/` 与部署环境为准。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/v1/chat/` | 非流式对话 |
| `POST` | `/api/v1/chat/stream` | Block Stream V2 SSE 对话 |
| `GET` | `/api/v1/chat/history/{session_id}` | 读取服务端会话历史 |
| `DELETE` | `/api/v1/chat/history/{session_id}` | 清理服务端会话历史 |

请求体 `ChatRequest` 的必填字段为 `user_input`。`user_id` 和 `session_id` 可选，但生产调用
应显式传入稳定、非默认的值以隔离会话；还支持 `files`、`user_location`、
`enable_knowledge` 和 `user_informations`。

```bash
curl -N -X POST http://127.0.0.1:9860/api/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"规划一次西安三日游","user_id":"operator","session_id":"console-demo"}'
```

## 流式协议

`/api/v1/chat/stream` 返回 `text/event-stream`。每个 SSE 帧使用如下信封：

```json
{
  "event": "block",
  "sessionId": "console-demo",
  "streamId": "stream_...",
  "userId": "operator",
  "seq": 3,
  "timestamp": 1784858624834,
  "data": {}
}
```

| SSE `event` | `data` 关键字段 | 客户端处理 |
| --- | --- | --- |
| `session_start` | `model` | 初始化本次流 |
| `block` | `id`、`type`、`delta` 或 `content` | 同一 `id` 的 `delta` 按顺序追加；保留原始换行和 Markdown |
| `tool` | `id`、`name`、`status`、`input`、`log`、`blocks`、`duration` | 展示工具的 `start`、`running`、`result`、`complete`、`error` 状态 |
| `thinking` | `id`、`content`、`collapsed` | 展示或折叠推理过程 |
| `error` | `code`、`message`、`details` | 显示可恢复或终止错误 |
| `done` | `totalBlocks`、`totalEvents`、`duration` | 标记内容生成结束 |
| `session_end` | `totalBlocks`、`duration` | 释放本次流状态 |

Muye 的模型正文以 `block` / `markdown` 实时透传；不使用自定义 `<md>`、`<table>`、`<json>`
标签，也不按段落或固定字符数裁切。客户端应使用 Markdown 渲染器并遵循 SSE 事件边界，而不是
根据段落、空行或工具结果猜测输出边界。

### SDK 与主 Agent 的职责边界

SDK 负责 internal Agent 通信协议、checkpointer 生命周期、同会话执行控制，以及 SSE 信封、
事件序列和 block 计数。主 Agent 保留业务工具路由和 Block Stream V2 payload 投影，不把自身
业务编排等同于 SDK 的 ReAct 模式。

主 Agent 当前通过 Prompt 与工具描述约束旅行、订单等业务路由，并使用
`ClarificationMiddleware` 处理澄清、`LoopDetectionMiddleware` 检测循环。SDK 的
`IntentGuard` 是独立、默认关闭的可选输入分类能力；主 Agent 没有自动启用它，也没有用它替换
已有意图约束。

## 本地控制台

`muye-gateway` 的本地 Dashboard API 提供
`http://127.0.0.1:9870/console/online.html`。控制台生成随机 Session ID，并在浏览器
`localStorage` 保存对话历史；该浏览器历史与服务端 `/history` 接口相互独立。工具过程在完成
后会自动折叠，正文按原始 Markdown 换行渲染。

## 验证

从仓库根目录运行：

```bash
PYTHONPATH=agents/agent-main \
  .venv/bin/python -m pytest -q agents/agent-main/tests/test_muye_service_integration.py
```

此测试不需要真实模型凭据；端到端运行仍需要可用的 `muye-llm` 与相关部署配置。
