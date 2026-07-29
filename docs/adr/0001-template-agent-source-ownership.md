# ADR-0001: 模板 Agent 源码所有权与交付方式

- 状态：已接受
- 日期：2026-07-29
- 决策范围：Scaffold `v2.0.x-dev` / SDK `v2.0.x-dev`

## 背景

v2.0 面向开发者交付知识 Agent。原始知识需要经过确认和评测，而后续 Prompt、工具和业务逻辑应由开发者
在常规工程流程中维护，不能由聊天请求或 Web 管理界面隐式改写。

## 决策

- LLM 只输出受严格 Schema 约束的 Proposal，不能输出或执行 Python、Shell、Dockerfile、依赖、URL 或凭据。
- Generator 从版本化 `react-knowledge` 模板确定性生成一次源码到 `agents/agent-<slug>/`；已存在目录默认失败。
- 首次生成后，源码、`agent.yaml`、测试和 Prompt 由开发者及 Git 接管。模板升级通过新目录、Diff 和人工合并完成。
- Web 和 Control Server 只显示状态、拓扑与授权关系，不能写源码、运行 Git、构建镜像、执行 Shell 或访问 Docker Socket。
- 每个 SubAgent 以独立 SDK HTTP 服务部署；发布、下线与回滚由 Git、CI、镜像和脚本显式执行。v2.0 不承诺热更新或零停机发布。

## 后果

- 生成基线可重放、可审阅，开发者修改也有明确的责任边界。
- Agent 数量增长会带来独立镜像、进程和日志成本；v2.0 的目标规模是 1 至 20 个 SubAgent。
- Generator、模板与 CI 必须提供 source checksum、provenance、测试和构建证明，不能以运行时动态代码替代。
