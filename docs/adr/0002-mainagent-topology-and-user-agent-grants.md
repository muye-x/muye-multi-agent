# ADR-0002: MainAgent 单向拓扑与 User-Agent 授权

- 状态：已接受
- 日期：2026-07-29
- 决策范围：Scaffold `v2.0.x-dev` / SDK `v2.0.x-dev`

## 背景

初版需要让已登录用户调用获授权的知识 Agent，同时避免多层委派、调用环和将 Cool 的界面权限混入 Agent
运行权限。

## 决策

- 调用拓扑固定为 `Client -> Gateway -> MainAgent -> SubAgent -> muye-llm / muye-data`。
- 只允许 `MainAgent -> SubAgent`。SubAgent 不获得 `InternalAgentClient` 或任何调用 MainAgent、其他 SubAgent 的服务凭据。
- Agent 调用授权只有 `user_agent_grants(user_id, agent_id)`。新用户默认没有授权，授权绑定稳定 `agent_id`，不绑定 URL、镜像或知识物理表。
- MainAgent 是登录用户固定入口，不进入 grant 表。每次请求只把 `ACTIVE Catalog ∩ grants(user_id)` 投影为模型工具，并在执行前再次校验。
- Cool 控制面只有内置 `Admin` 角色；Admin 管理用户和映射，但不自动拥有生产 Agent 调用权限。普通用户可以无角色登录。
- 可信身份只来自经 Gateway/Control 验证的会话和 CallerContext；请求体或客户端 Header 中的 `user_id`、`agent_id` 与 URL 不可信。

## 后果

- 授权模型与 Cool 的界面权限解耦，且发布、回滚无需迁移用户授权。
- 复杂多 Agent 协作、外部 API Key、Hermes 与 ServicePrincipal 延后至后续 ADR。
- MainAgent、Control Server、Gateway、SDK 和引用查询都必须测试撤销、伪造身份、空授权及跨 Agent 隔离。
