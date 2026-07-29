# ADR-0003: Catalog 与 Compose 生成物生命周期

- 状态：已接受
- 日期：2026-07-29
- 决策范围：Scaffold `v2.0.x-dev` / SDK `v2.0.x-dev`

## 背景

`agent.yaml`、BuildRecord、Catalog 和 Compose aggregate 共同决定可运行的 Agent 集合。若这些文件都能手工
修改或每次同步带入墙上时钟时间，代码审阅、回滚和 CI 漂移检查都会失去意义。

## 决策

- `agent.yaml`、`.muye-generation.json`、测试和 `config/generated/builds/<agent_id>/<version>.json` 是可提交的输入或构建证明。
- `config/generated/agent-catalog.json` 是可提交的 `AgentCatalogSnapshotV1`。它以排序后的 descriptor、source 与 BuildRecord checksum 派生 revision/checksum，不能包含墙上时钟时间或凭据。
- `compose.agents.generated.yaml` 和 `config/generated/catalog-report.json` 是部署聚合与诊断输出，不提交 Git；已在 `.gitignore` 中忽略。
- `agent sync` 先在临时位置校验并生成全部候选文件，再原子替换输出。CI 使用无写入的 `agent sync --check` 重新生成并比较受提交管理的 Catalog。
- 部署在同一命令中重建 Compose aggregate 并校验它与 Catalog 的输入 checksum 一致；不接受手工修改的 aggregate。

## 后果

- 提交的 Catalog 可以审阅、回滚和驱动 MainAgent 的原子加载；Compose 文件不会造成环境无关的 Git 噪声。
- 生成器必须定义稳定排序、规范 JSON 序列化和 checksum 算法，并在阶段 3 提供 `--check`。
- BuildRecord 缺失、镜像非 digest 固定、Catalog/Compose 输入不一致时必须阻断同步和部署。
