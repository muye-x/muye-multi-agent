# Agent 项目输入

`agent-projects/` 存放可版本控制的 Agent 创建输入。每个子目录都应包含 `project.yaml` 和 `sources/`，并可直接作为 `agent prepare` 的参数。

```bash
./scripts/muye.sh agent prepare agent-projects/<slug> \
  --auto-approved-by <reviewer> \
  --dev
```

`hotel-employee/` 是脚手架内置示例，包含酒店员工手册及其 Agent 定义。它用于演示从资料构建知识库、生成 SubAgent 和启动本地联调的完整流程。

生成的源码位于 `agents/agent-<slug>/`，Milvus Collection、MinIO 数据、创建计划、评测报告和本地运行文件均属于生成或运行产物，不属于本目录。不要将密钥、Token、数据库连接串或其他环境配置写入 `project.yaml` 或 `sources/`。
