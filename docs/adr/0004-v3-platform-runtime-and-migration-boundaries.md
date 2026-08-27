# ADR-0004: v3 平台、Runtime 与迁移边界

- 状态：已接受
- 日期：2026-08-27
- 决策范围：Scaffold `v3.0.x-dev`

## 背景

v2.0 将 Agent 生成、Control、MainAgent、Data、LLM、Gateway 和 Channel 分为多个可独立部署模块，并使用多个服务 Token 建立跨模块调用边界。这种设计适合跨主机或零信任网络，但当前目标是单机 Compose：仅 Gateway 对外，内部服务由平台管理员创建并加入私有网络。

同时，v2 的源码生成和 Git 接管模型不能让管理员在 UI 中安全地持续创建、更新和发布 Agent。v3 必须支持从资料到上线的闭环，但不能将 Docker、Git、Shell、任意源码或任意镜像的执行能力暴露给 Web、Core 或模型。

## 决策

- v3 使用模块化单体 `muye-core` 承载身份、授权、Agent Studio、知识构建、评测、Catalog、MainAgent、模型适配、检索、Channel 与审计。模块边界保留在代码中，模块间不再通过同机 HTTP 和服务 Token 通信。
- 平台常驻业务单元固定为 `gateway`、`core` 与 `deployment-runner`。PostgreSQL、Milvus 和 Artifact Storage 是基础设施；每个业务 Agent 仍是独立 Runtime 容器。
- UI 首发只能创建声明式知识 Agent。一个 Agent Revision 是不可变的配置、资料 Asset hash、模型 alias、检索参数、预算和评测集快照；它不能包含 Python、Shell、Dockerfile、依赖、URL、物理路径、容器参数或凭据。
- 所有声明式 Agent 使用固定 digest 的 `knowledge-agent-runtime` 镜像。Runtime 只加载已校验 Bundle，只提供受控只读检索和固定 Runtime HTTP 接口，不接受任意代码或工具。
- `deployment-runner` 是唯一可访问 Docker API 的进程。它只领取 Core 持久化的结构化 Deployment Job，且只接受固定 Runtime digest、已验证 Bundle、平台定义的资源档位、固定网络和只读挂载。它拒绝 image、command、entrypoint、host path、port、environment map、privileged、device、capability 与网络覆盖参数。
- Core、Runtime、Runner、数据库和 Milvus 均不发布宿主机端口。Gateway 是唯一公开入口。
- 在单机可信 Docker 网络前提下，删除 Control、Main、Health、Gateway、Data 和单个 Agent 的内部服务 Token。保留外部用户会话、Admin、`user_agent_grants` 及模型/数据库/Channel 等真实外部系统凭据。
- v2 Agent 不自动迁移。每个 Agent 都要在 v3 UI 中人工重建、重新评测、预览、发布并按 Agent 切流；有 `source_drift` 的 v2 Agent 不能被声明式 Runtime 静默接管。

## 安全接受条件

取消内部 Token 的条件不是“任何内网都可信”，而是以下四项同时成立：

1. Docker 宿主机和内部网络成员只能由受信任管理员控制。
2. 只有 Gateway 发布宿主机端口；内部网络不允许不受控容器加入。
3. Agent Runtime 是固定、受审计、非 root、只读且不运行用户代码的镜像。
4. Runner 的 Docker 权限通过 rootless Docker 或等价最小权限机制隔离，并严格执行输入 allowlist。

任何一项不成立时，例如支持自定义代码 Agent、第三方镜像、跨主机部署或不可信工作负载，必须先采用新的 ADR 引入 mTLS 或工作负载身份。不得沿用“无内部 Token”假设。

## 后果

- 单机部署和配置显著收敛，Web 与 CLI 通过同一 Core API 工作，避免双入口状态分裂。
- Revision、Build、Evaluation、Deployment 和审计将成为 v3 的版本事实源；后续阶段不得重新引入文件直写、生成 Compose 或服务 Token 映射作为第二事实源。
- Core 进程的可用性影响面变大，必须采用清晰模块边界、超时、熔断、后台 Job 和恢复机制；Runtime 仍独立于 Core。
- Runner 仍是宿主机高权限边界。rootful Docker 只能作为受控部署环境的显式风险接受，不能被 Web 或 Core 直接访问。
- 迁移期 v2/v3 并行会增加短期运维成本，但避免将未审查的 v2 业务代码导入新的安全模型。

## 不采纳方案

- **保留每个模块/Agent 独立 Token**：在单机可信网络中维护成本高，且无法解决 UI/CLI 双入口和部署单元过多的问题。
- **完全合并所有 Agent 到 Core 进程**：部署最少，但失去业务 Agent 的资源、故障和发布隔离。
- **Core 挂载 Docker socket**：组件少，但 Core 被攻破即可能获得宿主机控制能力。
- **UI 上传或生成任意 Agent 源码**：需要构建沙箱、供应链扫描和代码审批体系，不属于 v3.0 首发范围。
- **自动迁移 v2 Agent 源码**：无法可靠判断 `source_drift` 的行为语义和权限，需要人工重建与验收。
