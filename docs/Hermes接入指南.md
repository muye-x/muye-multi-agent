# Hermes 接入 Muye Main Agent 指南

## 概述

本文档说明 Hermes 如何通过生产环境反向代理接入 Muye Main Agent。Main Agent 会根据当前用户授权和 Catalog 状态，自动调用酒店员工手册子 Agent 检索并回答。

酒店子 Agent 是内部服务，Hermes 不应直接调用它；所有对外请求都应经过 Main Agent。

## 生产接口

### 端点

```text
POST https://hermes.hscm.net.cn:18443/agentMain/api/v1/chat/stream
```

这是生产环境的对外代理地址。`/chat` 是浏览器页面，不是 API；Docker 内部的 `agent-main:9860` 也不应作为 Hermes 的生产调用地址。

### 请求头

| Header | 值 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Authorization` | `Bearer {{HERMES_MUYE_MAIN_TOKEN}}` | 是 | 从 Hermes 密钥管理读取的 Main Agent 调用凭据。不得写入 Prompt、前端代码、日志或文档。 |
| `Accept` | `text/event-stream` | 是 | 声明接收 SSE 流。 |
| `Content-Type` | `application/json` | 是 | 固定值。 |

生产代理负责可信用户身份处理；调用方不得自行伪造内部 `X-Muye-User-Id`。

### 请求体

```json
{
  "user_input": "用户的问题",
  "session_id": "稳定且唯一的会话 ID",
  "enable_knowledge": true
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_input` | string | 是 | 原样传递用户问题，不附加内部指令或敏感信息。 |
| `session_id` | string | 是 | 同一用户的连续话题保持一致；不同用户或独立会话必须隔离。 |
| `enable_knowledge` | boolean | 否 | 酒店制度、员工手册和服务规范查询应设为 `true`。 |
| `stream` | boolean | 否 | 可省略；该端点默认使用流式输出。 |

## SSE 返回协议

响应类型为 `text/event-stream`。每个 SSE 帧由事件名和 JSON 数据组成：

```text
event: <event_name>
data: <JSON object>
```

JSON 信封包含以下字段：

```json
{
  "event": "block",
  "sessionId": "console-...",
  "streamId": "stream_...",
  "userId": "服务端识别的用户 ID",
  "seq": 2,
  "timestamp": 1787282586622,
  "data": {}
}
```

事件一般按以下顺序到达：

```text
session_start -> block / tool / thinking / error -> done -> session_end
```

| 事件 | `data` 关键字段 | 调用方处理 |
| --- | --- | --- |
| `session_start` | `model` | 初始化当前流并记录 `sessionId`、`streamId`，不作为用户可见正文。 |
| `block` | `id`、`type`、`delta` 或 `content` | 累积 Markdown 正文，见下文拼接规则。 |
| `tool` | `id`、`name`、`status`、`log`、`blocks` | 可展示简短进度；不得暴露内部地址、Token、堆栈或原始入参。引用信息应保留。 |
| `thinking` | `id`、`content`、`collapsed` | 默认不展示给最终用户；如产品需要，可折叠展示。 |
| `error` | `code`、`message`、`details` | 将本次调用视为失败，不能把内部细节直接展示给用户。 |
| `done` | `totalBlocks`、`totalEvents`、`duration` | 正文已生成完成。 |
| `session_end` | `totalBlocks`、`duration` | 释放当前 `streamId` 的本地状态。 |

### 文本拼接规则

1. 按 `seq` 升序处理事件。
2. 使用 `(streamId, data.id)` 作为 block 缓冲区键。
3. 同一 block 的 `data.delta` 按到达顺序直接追加，不插入空格、换行或分隔符，并保留原始 Markdown。
4. 若 `data.content` 存在，它是该 block 的完整正文，应覆盖已缓存内容；后续 `delta` 再继续追加。
5. 按 block 首次出现顺序拼接完整 Markdown，作为最终回答。
6. 收到 `done` 后可输出正文，但应继续读取至 `session_end` 或连接关闭，以完成资源清理。

例如依次收到 `"你好"`、`"！我是"` 和 `" Muye"` 三个 `delta`，最终文本必须为 `你好！我是 Muye`。

## Hermes 自接入 Prompt

将以下内容提供给 Hermes：

```text
你是 Hermes 智能助手。遇到 muye 酒店员工手册、员工制度、酒店运营规范相关问题时，调用 Muye Main Agent 获取可信回答。

[服务]
POST https://hermes.hscm.net.cn:18443/agentMain/api/v1/chat/stream

[认证]
Authorization: Bearer {{HERMES_MUYE_MAIN_TOKEN}}
Accept: text/event-stream
Content-Type: application/json

从 Hermes 密钥管理读取 HERMES_MUYE_MAIN_TOKEN。禁止在 Prompt、日志、回复内容或前端代码中写入 Token 明文。不要自行传入或伪造内部 X-Muye-User-Id。

[请求体]
{
  "user_input": "{{用户原始问题}}",
  "session_id": "{{稳定且唯一的会话ID}}",
  "enable_knowledge": true
}

[调用意图]
当用户提及 muye、牧野酒店、muye 大酒店，或咨询以下主题时调用：
- 酒店员工手册、员工管理、规章制度；
- 考勤、迟到、早退、旷工、排班、调班、加班、请假；
- 入职、离职、晋升、处分、薪资和员工行为规范；
- 服务规范、制服、客用设施、卫生、投诉；
- 消防、安全、保密、宿舍等酒店管理制度；
- 已明确处于 muye 酒店话题中的后续追问。

普通闲聊、天气、通用知识和与 muye 酒店无关的问题不调用。无法判断时先澄清是否咨询 muye 酒店相关事项。

[返回值处理]
接口返回 SSE。持续读取到 done 和 session_end；按 seq 顺序处理。
对相同 (streamId, block.data.id) 的 block.data.delta 直接拼接；若有 block.data.content，以其覆盖该 block 已缓存正文。按 block 首次出现顺序拼接 Markdown，作为最终回答。
保留知识库引用、来源和“未检索到明确规定”等限制性结论。tool 事件只用于显示简短进度，不暴露内部配置或错误细节。

[异常处理]
- HTTP 401：停止重试，检查 Hermes 密钥配置。
- 网络错误、5xx 或 SSE 中断：最多重试一次，使用相同 session_id。
- 收到 error、流结束前没有正文或未收到 done：回复“酒店知识库服务暂时不可用，请稍后再试”。
- 不得自行补全、猜测或编造酒店制度。
```

## 接入验证

Token 仅从环境变量或密钥管理读取：

```bash
curl -N 'https://hermes.hscm.net.cn:18443/agentMain/api/v1/chat/stream' \
  -H "Authorization: Bearer $HERMES_MUYE_MAIN_TOKEN" \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"user_input":"muye酒店员工迟到怎么处理？","session_id":"hermes-test-001","enable_knowledge":true}'
```

预期结果：返回 `session_start`、一个或多个 `block`，随后返回 `done` 与 `session_end`。酒店制度问题通常还会包含 `tool` 事件，表示 Main Agent 调用了酒店员工手册子 Agent。

## 常见问题

| 问题 | 原因与处理 |
| --- | --- |
| `401 Unauthorized` | Token 无效、过期或未由 Hermes 密钥管理正确注入。检查密钥配置，不在日志中打印 Token。 |
| 未获得酒店手册结果 | 检查代理映射的用户是否被授予 `agent_hotel_employee` 权限，并确认请求使用 `enable_knowledge: true`。 |
| SSE 内容不完整 | 不要在首个 `block` 后关闭连接；至少读取到 `done`，并优先等待 `session_end`。 |
| 超时或 `5xx` | 检查生产代理及 Main Agent 状态；最多重试一次，避免同一请求并发重复调用。 |

## 更新日志

- 2026-08-21：更新为生产反向代理地址，使用密钥变量管理调用 Token。
- 2026-08-21：补充 Block Stream V2 SSE 信封、`seq` 排序、block 拼接和终止事件处理规则。
