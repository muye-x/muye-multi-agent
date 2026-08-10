# Muye 模型中心

[English overview](README.en.md)

`muye-llm` 是仅供可信内网服务调用的模型网关。它为 Chat、Embedding 和 Rerank 提供独立的
模型别名注册表，并负责模型能力校验、上游超时与有限重试。Rerank 首版适配 DashScope，默认
关闭。服务不包含计费、用量上报或 `usage_context` 兼容层。Tracing 默认关闭，开启后只上报
`trace_id`、模型、thinking、延迟、工具数量和状态，不上报任务正文或业务 metadata；LangSmith
故障不会阻断模型响应。

## 接口

服务默认监听 `http://127.0.0.1:9850`，仅面向可信内网服务。应用本身不实现调用方鉴权，
生产部署必须通过网络边界限制访问。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 检查模型网关进程是否存活。 |
| `GET` | `/api/v2/models` | 获取允许调用的模型别名与 thinking 能力。 |
| `POST` | `/api/v2/chat` | 获取一次性完整对话结果。 |
| `POST` | `/api/v2/chat/stream` | 以 SSE 持续返回对话增量和工具调用。 |
| `POST` | `/api/v2/embed` | 为一组文本生成 Embedding 向量。 |
| `POST` | `/api/v2/rerank` | 对候选文档排序，只返回原始索引和分数。 |

除健康检查外，JSON 接口的成功响应均使用以下信封。`timestamp` 是 UTC ISO 8601 时间；
`data` 的结构由具体接口定义。

```json
{
  "success": true,
  "code": 200,
  "message": "ok",
  "data": {},
  "timestamp": "2026-07-24T00:00:00+00:00"
}
```

请求模型使用严格 schema，未知字段会被拒绝。已移除的 `usage_context` 不能再发送；
Pydantic 参数校验失败返回 HTTP `422`，业务参数错误（如空 `messages`、未知模型或不支持
thinking）返回 HTTP `400`。Rerank 未启用返回 HTTP `503`；Rerank 上游失败返回 HTTP `502`。
非流式对话和向量接口沿用原有信封内 `code: 502` 失败语义；未处理的服务异常返回 HTTP `500`。

### `GET /health`

用于负载均衡、容器编排或运维探针检查网关进程是否已完成启动。它只表示本服务存活，
不探测上游模型服务，也不代表任意模型都可调用。

响应字段：

| 字段 | 说明 |
| --- | --- |
| `status` | 固定为 `ok`。 |
| `service` | 服务标识，当前为 `4_llm`。 |
| `uptime` | 从 FastAPI 生命周期启动开始计算的运行秒数。 |

```bash
curl http://127.0.0.1:9850/health
```

### `GET /api/v2/models`

返回当前配置允许请求使用的模型别名。调用 `chat` 或 `chat/stream` 时，`model` 必须使用这里的
`id`，不能直接透传上游 provider 的真实模型名。

响应 `data` 字段：

| 字段 | 说明 |
| --- | --- |
| `default_model` | 请求未传 `model` 时使用的模型别名。 |
| `default_thinking` | 请求未传 `enable_thinking` 时使用的默认开关。 |
| `models` | 可调用模型数组。 |
| `models[].id` | 调用时使用的模型别名。 |
| `models[].name` | 用于界面展示的名称。 |
| `models[].supports_thinking` | 是否允许启用 `enable_thinking`。 |
| `models[].is_default` | 是否为默认模型。 |
| `default_embedding_model` | Embedding 请求未传 `model` 时使用的 alias。 |
| `embedding_models` | 可调用的 Embedding alias、展示名、预期维度及默认标记。 |
| `default_rerank_model` | Rerank 默认 alias；功能关闭时为 `null`。 |
| `rerank_models` | 功能开启时可调用的 Rerank alias；关闭时为空数组。 |

该接口不会返回上游模型名、上游 URL 或密钥等内部配置。

```bash
curl http://127.0.0.1:9850/api/v2/models
```

### 对话请求字段

`POST /api/v2/chat` 与 `POST /api/v2/chat/stream` 共用下列请求体。`messages` 必填且至少包含
一条消息；未提供的可选采样参数会使用服务端环境配置默认值。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `messages` | `ChatMessage[]` | 是 | OpenAI-compatible 消息列表。 |
| `trace_id` | string | 否 | 调用链关联标识；默认空字符串，仅用于日志与脱敏 tracing。 |
| `model` | string | 否 | `/models` 返回的模型别名；缺省时使用 `default_model`。 |
| `enable_thinking` | boolean | 否 | 是否启用模型思考能力；缺省时使用 `default_thinking`。不支持该能力的模型传 `true` 会失败。 |
| `max_tokens` | integer | 否 | 最大输出 token 数，必须大于等于 `1`。 |
| `temperature` | number | 否 | 采样温度，范围为 `0` 至 `2`。 |
| `tools` | object[] | 否 | OpenAI function calling 工具定义，原样转发给上游。 |
| `tool_choice` | string 或 object | 否 | OpenAI-compatible 工具选择策略，原样转发给上游。 |

每个 `ChatMessage` 的 `role` 必须是 `system`、`user`、`assistant` 或 `tool`。`content` 可以是
字符串、内容块数组或 `null`；`name`、`tool_call_id` 和 `tool_calls` 用于传递工具调用上下文。
例如，工具调用后应以 `assistant.tool_calls` 记录模型请求，再使用带对应 `tool_call_id` 的
`tool` 消息回填工具结果。

### `POST /api/v2/chat`

发起非流式对话，并在模型完成后返回完整文本。该接口会剥离上游文本中的 `<think>...</think>`
内容，避免将模型推理正文暴露给调用方。

```bash
curl -X POST http://127.0.0.1:9850/api/v2/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "trace_id": "travel-plan-001",
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "介绍西安两日游路线"}],
    "temperature": 0.2,
    "max_tokens": 800
  }'
```

成功时 `data.content` 为最终可见文本。若上游以 function calling 结束，`data.content` 是包含
`tool_calls` 数组的 JSON 字符串，而非普通回答文本；调用方应解析该字符串并执行工具流程。
上游未返回可见内容时，接口返回 `success: false`、`code: 502`。

```json
{
  "success": true,
  "code": 200,
  "message": "ok",
  "data": {"content": "第一天参观兵马俑..."},
  "timestamp": "2026-07-24T00:00:00+00:00"
}
```

### `POST /api/v2/chat/stream`

发起流式对话，响应类型为 `text/event-stream`。响应头包含调用方传入的 `X-Trace-Id`，并设置
`Cache-Control: no-cache` 与 `X-Accel-Buffering: no`，以避免代理缓冲。请求校验和模型能力校验
会在 SSE 开始前完成；此时失败仍返回普通 JSON 错误响应。

流中事件含义如下：

| 事件 | `data` 内容 | 说明 |
| --- | --- | --- |
| `token` | `{"content":"..."}` | 可见文本增量。思考标签及其内容不会发出。 |
| `tool_calls` | `{"tool_calls":[...]}` | 已合并完成的 OpenAI-compatible 工具调用数组。 |
| `error` | `{"message":"流式生成服务内部异常"}` | SSE 已开始后发生上游调用失败。 |
| `done` | `{"finish_reason":"stop"}` | 终止事件；`finish_reason` 也可能是 `tool_calls` 或 `error`。 |

```bash
curl -N -X POST http://127.0.0.1:9850/api/v2/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"介绍西安两日游路线"}]}'
```

这是模型网关的原始流协议。`agent-main` 消费该流后，会投影为自身的 Block Stream V2
（`session_start`、`block`、`tool`、`thinking`、`done`、`session_end`）；两者的事件名与
数据结构不能混用。客户端不应从 `token` 文本中猜测工具调用 JSON，应只处理 `tool_calls` 事件。

### `POST /api/v2/embed`

为一批文本生成向量，适用于检索和相似度计算。服务按输入顺序返回向量，调用方应将第 `n` 个
向量与第 `n` 个输入文本对应。可选 `model` 只能使用 `/models` 返回的 alias，不能传上游模型名。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `texts` | string[] | 是 | 待向量化文本，至少包含一项。 |
| `trace_id` | string | 否 | 调用链关联标识，仅用于日志。 |
| `model` | string | 否 | Embedding alias；缺省时使用 `default_embedding_model`。 |

```bash
curl -X POST http://127.0.0.1:9850/api/v2/embed \
  -H 'Content-Type: application/json' \
  -d '{"trace_id":"knowledge-001","texts":["兵马俑","回民街美食"]}'
```

成功响应的 `data.embeddings` 是 `number[][]`，`data.count` 是实际返回向量数量，`data.model`
是实际 alias，`data.dimensions` 是本次向量的实际维度。服务会拒绝向量数量错误、维度不一致、
非有限数值以及与注册表预期维度不一致的响应。上游失败仍沿用 `success: false`、`code: 502`。

新部署使用 `MUYE_LLM_EMBED_MODELS_JSON` 和 `MUYE_LLM_EMBED_DEFAULT_MODEL` 配置注册表。为保持
兼容，如果没有配置注册表，服务会把旧 `MUYE_LLM_EMBED_MODEL` 的值同时作为公开 alias 和
provider 模型名；此兼容模式的预期维度未知，但响应仍返回实际维度。

### `POST /api/v2/rerank`

对调用方提供的候选文档做相关性排序。服务不保存候选内容，响应也不回显文档；`index` 始终指向
请求中原始 `documents` 的下标。`model` 只能使用 `/models` 返回的 Rerank alias。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 非空查询文本，长度受服务端预算限制。 |
| `documents` | string[] | 是 | 非空候选文本，数量、单项长度和总字符数均有限制。 |
| `top_n` | integer | 是 | 返回数量，范围为 `1..len(documents)`。 |
| `model` | string | 否 | Rerank alias；缺省时使用 `default_rerank_model`。 |
| `trace_id` | string | 否 | 调用链关联标识，仅用于脱敏日志。 |

```bash
curl -X POST http://127.0.0.1:9850/api/v2/rerank \
  -H 'Content-Type: application/json' \
  -d '{
    "trace_id":"knowledge-001",
    "query":"西安有哪些历史景点？",
    "documents":["兵马俑位于临潼区。","回民街以美食闻名。"],
    "top_n":1
  }'
```

成功响应的 `data.results` 是按分数降序排列的 `[{"index": 0, "score": 0.95}]`，同时返回
实际 `model` alias 和 `count`。服务会拒绝重复、越界 index、非有限分数和超过 `top_n` 的上游
结果。网络错误、超时、HTTP `408`、`429` 和 `5xx` 会有限重试；认证错误、其他 `4xx` 及协议
错误不重试。日志只记录 trace、alias、文档数、结果数、耗时、尝试次数和错误类别。

## 运行

```bash
cp .env.example .env
../.venv/bin/python main.py
```

默认监听 `127.0.0.1:9850`。`.env.example` 列出 Chat、Embedding、Rerank 模型注册表和可选
LangSmith 配置；`MUYE_LLM_API_KEY` 与 `MUYE_LLM_EMBED_API_KEY` 必须填入实际运行值。只有
`MUYE_LLM_RERANK_ENABLED=true` 时才要求 `MUYE_LLM_RERANK_API_KEY`。模板可以提交，包含真实
密钥的 `.env` 不得写入仓库。

## 验证

```bash
../.venv/bin/python -m pytest -q tests
```
