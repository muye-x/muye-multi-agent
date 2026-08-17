# Hermes 接入 Muye Main Agent 指南

## 概述

本文档用于指导 Hermes 系统接入 Muye Multi-Agent Scaffold 的主 Agent（agent-main）。接入后，Hermes 可以将特定话题（如"muye酒店"相关问题）路由到主 Agent 进行回复，主 Agent 会自动选择合适的子 Agent（如酒店员工手册助手）进行知识库检索和回答。

## 网络架构

```
Hermes (内网) ──HTTP──▶ agent-main:9860 ──▶ muye-llm (模型推理)
                          │
                          ├──▶ muye-data (知识库检索)
                          │
                          └──▶ agent-hotel-employee (酒店员工手册子Agent)
```

**前提条件**：Hermes 与 agent-main 在同一内网，可直接访问 `http://localhost:9860`。

---

## API 接口说明

### 端点

```
POST http://localhost:9860/api/v1/chat/stream
```

### 请求头

| Header | 值 | 必填 | 说明 |
|--------|-----|------|------|
| `Authorization` | `Bearer <YOUR_TOKEN>` | 是 | 服务间调用凭据 |
| `X-Muye-User-Id` | `hermes_test` | 是 | 用于授权隔离和会话管理（已预注册并授权） |
| `Content-Type` | `application/json` | 是 | 固定值 |

### 请求体

```json
{
  "user_input": "用户的问题",
  "session_id": "会话ID",
  "enable_knowledge": true,
  "user_id": "用户ID"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_input` | string | 是 | 用户输入的问题 |
| `session_id` | string | 否 | 会话 ID，同一用户同一话题应保持一致以维持上下文。默认为 `default_session` |
| `enable_knowledge` | boolean | 否 | 是否启用知识库检索。设为 `true` 时可触发子 Agent 的 RAG 检索 |
| `user_id` | string | 否 | 用户 ID，与 `X-Muye-User-Id` 作用相同，后者优先 |

### 响应格式

响应为 SSE（Server-Sent Events）流，格式如下：

```
event: <事件类型>
data: <JSON 数据>

```

#### 事件类型

| 事件类型 | 说明 |
|----------|------|
| `session_start` | 会话开始，包含 session_id、model 等信息 |
| `block` | 文本块，包含 `delta`（增量文本）或 `content`（完整内容） |
| `tool` | 工具调用，包含工具名称、状态、输入输出 |
| `thinking` | 思考过程（如启用） |
| `error` | 错误信息 |
| `done` | 流结束 |
| `session_end` | 会话结束 |

#### 响应示例

```
event: session_start
data: {"event":"session_start","sessionId":"hermes-001","userId":"user_001","data":{"model":"deepseek-v4-flash"}}

event: block
data: {"event":"block","sessionId":"hermes-001","data":{"id":"b1","type":"markdown","delta":"你好"}}

event: block
data: {"event":"block","sessionId":"hermes-001","data":{"id":"b1","type":"markdown","delta":"！我是 Muye 酒店助手。"}}

event: done
data: {"event":"done","sessionId":"hermes-001","data":{"totalBlocks":1,"totalEvents":4,"duration":1234}}

event: session_end
data: {"event":"session_end","sessionId":"hermes-001","data":{"totalBlocks":1,"duration":1234}}
```

---

## Hermes 自接入 Prompt

将以下 Prompt 提供给 Hermes，Hermes 即可根据 Prompt 自行完成接入配置：

---

### Prompt 内容

```
你是一个智能助手，现在需要接入 Muye 酒店知识库系统来回答与"muye酒店"相关的问题。

## 接入配置

当你识别到用户的问题与以下话题相关时，需要调用 Muye Main Agent API：

### 触发话题
- muye 酒店
- 酒店员工手册
- 酒店规章制度（考勤、迟到、请假、加班、入职、离职、晋升、处分等）
- 酒店服务规范（制服管理、客用设施、公告栏等）
- 酒店安全管理（消防安全、保密制度等）

### API 调用方式

**端点**：`POST http://localhost:9860/api/v1/chat/stream`

**请求头**：
Authorization: Bearer <YOUR_TOKEN>
X-Muye-User-Id: hermes_test
Content-Type: application/json


**请求体**：
{
  "user_input": "<用户的问题>",
  "session_id": "<会话ID，同一用户同一话题保持一致>",
  "enable_knowledge": true
}

### 响应解析

**⚠️ 重要：必须读取完整的 SSE 流，直到收到 `event: done` 或 `event: session_end`！**

响应是 SSE 流，你需要：
1. 持续读取，直到收到 `event: done` 或 `event: session_end`
2. 解析每个 `event:` 和 `data:` 行
3. 提取 `event: block` 中的 `data.delta` 字段，拼接为完整回复
4. 可选：解析 `event: tool` 了解子 Agent 调用过程
5. 忽略 `session_start`、`done`、`session_end` 等元数据事件

**典型响应流程**：
session_start → block(初始回复) → tool(子Agent调用) → block(知识库内容) → done → session_end

**错误处理**：
- 如果收到 `event: error`，检查 `data.message` 字段获取错误信息
- 如果收到 `event: done` 但没有 `block` 事件，可能是 Agent 内部错误

### 回复策略

1. **直接回复**：将 Muye Agent 返回的文本直接作为回复内容
2. **引用来源**：如果返回中包含知识库引用（如"来源：muye大酒店员工手册"），在回复中保留引用
3. **错误处理**：如果 API 调用失败，回复"抱歉，酒店知识库暂时不可用，请稍后再试"
4. **话题切换**：如果用户的问题不再与酒店相关，正常回复，不调用 Muye API

### 示例对话

**用户**：muye酒店的员工如果迟到了怎么办？

**你的处理**：
1. 识别到"muye酒店"+"迟到"，触发 Muye API 调用
2. 调用 API：`POST http://localhost:9860/api/v1/chat/stream`
3. 请求体：`{"user_input":"muye酒店的员工如果迟到了怎么办","session_id":"session_001","enable_knowledge":true}`
4. 解析 SSE 响应，提取 block 事件中的 delta 文本
5. 将拼接后的文本作为回复

**用户**：今天天气怎么样？

**你的处理**：
1. 未识别到酒店相关话题
2. 正常回复，不调用 Muye API
```

---

## 接入验证

### 1. 测试 API 连通性

```bash
curl -X POST 'http://localhost:9860/api/v1/chat/stream' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -H 'X-Muye-User-Id: hermes_test' \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"你好","session_id":"test-001"}'
```

预期返回：SSE 流，包含 `session_start`、`block`、`done`、`session_end` 事件。

### 2. 测试知识库检索

```bash
curl -X POST 'http://localhost:9860/api/v1/chat/stream' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -H 'X-Muye-User-Id: hermes_test' \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"muye酒店的员工如果迟到了怎么办","session_id":"test-002","enable_knowledge":true}'
```

预期返回：SSE 流，包含 `tool` 事件（子 Agent 调用）和酒店员工手册相关内容。

### 3. 测试子 Agent 授权

如果知识库检索未生效（返回的是联网搜索结果而非酒店手册内容），需要：

1. 访问 `http://localhost:8080/grants` 页面
2. 为 `hermes_test` 用户（或你的用户标识）勾选"酒店员工手册助手"
3. 保存后重新测试

---

## 常见问题

### Q1: 返回 401 Unauthorized

**原因**：`Authorization` 头中的 token 不正确。

**解决**：确认 token 为 `<YOUR_TOKEN>`（来自 `agents/agent-main/.env` 的 `MUYE_MAIN_CALLER_TOKEN`）。

### Q2: 返回联网搜索结果而非酒店手册内容

**原因**：用户未被授权使用酒店员工手册子 Agent。

**解决**：
1. 访问 `http://localhost:8080/grants`
2. 为对应用户勾选"酒店员工手册助手"
3. 保存后重试

### Q3: 连接超时

**原因**：网络不通或 agent-main 未启动。

**解决**：
1. 确认 Hermes 与 agent-main 在同一内网
2. 检查 agent-main 是否健康：`curl http://localhost:9860/health`
3. 检查防火墙规则

### Q4: 响应内容不完整

**原因**：SSE 流被提前截断。

**解决**：确保读取到 `event: done` 或 `event: session_end` 后才结束解析。

---

## 配置参数速查

| 参数 | 值 | 来源 |
|------|-----|------|
| API 端点 | `http://localhost:9860/api/v1/chat/stream` | compose.yaml |
| Caller Token | `<YOUR_TOKEN>` | agents/agent-main/.env |
| User ID | `hermes_test` | 已预注册并授权酒店员工手册子 Agent |
| 健康检查 | `http://localhost:9860/health` | agent-main API |
| 知识库 | `kb.hotel_employee` | config/knowledge/hotel-employee.yaml |
| 子 Agent | `agent_hotel_employee` | config/generated/agent-catalog.json |

---

## 更新日志

- 2026-08-15：初始版本，支持酒店员工手册知识库检索
- 2026-08-15：修复用户授权问题，添加 `hermes_test` 用户并授权酒店员工手册子 Agent
- 2026-08-15：补充响应解析说明，强调必须读取完整 SSE 流
