# 两步创建知识 Agent

本指南面向只持有资料文件、希望生成本地可测试 Agent 源码的使用者。该流程不会构建镜像、部署容器或授予用户访问权限。

## 前置准备

创建流程需要一个可用的 `muye-llm` 和可访问的 Milvus。按模块分别配置，根目录 `.env` 不参与此流程：

1. 将 `muye-llm/.env.example` 复制为 `muye-llm/.env`，配置 Chat、Embedding 上游凭据和模型注册；`project.yaml` 默认使用的 `chat-default` 与 `text-embedding-default` 必须已注册。
2. 启动 `muye-llm`，并确认其地址可由本机访问。默认地址是 `http://127.0.0.1:9850`。
3. 将 `tools/agent_creation/.env.example` 复制为 `tools/agent_creation/.env`，填写该流程使用的 LLM 地址、Milvus 地址，以及需要时的 Milvus token。

`muye-llm/.env` 管理上游 LLM 与 Embedding 密钥；`tools/agent_creation/.env` 只管理创建流程到已启动服务的连接，不能把密钥或数据库连接写入项目 YAML。

## 项目目录

创建一个项目目录，并将 Markdown、TXT、DOCX 或 PDF 放入 `sources/`。项目目录必须位于 `agents/` 之外，因为 `agents/agent-<slug>/` 是 `create` 的最终输出目录，不能预先存放项目文件。

```text
agent-projects/hotel-employee/
├── project.yaml
└── sources/
    └── 员工手册.md
```

只有一个 Markdown 文件时，可先执行：

```bash
mkdir -p agent-projects/hotel-employee/sources
cp /path/to/员工手册.md agent-projects/hotel-employee/sources/员工手册.md
```

最小 `project.yaml`：

```yaml
schema_version: muye.ai/agent-project/v1
slug: hotel-employee
agent_id: agent_hotel_employee
display_name: 酒店员工手册助手
objective: 根据员工手册回答制度问题，并提供来源引用。
prohibited_actions:
  - 不得把模型常识冒充手册规定
  - 没有检索依据时不得猜测
  - 不执行请假审批或工资调整
examples:
  - 请事假需要提前多久申请？
```

Markdown 与 TXT 无需 Docling；DOCX/PDF 需要 Docling，扫描 PDF 还需要在 `project.yaml` 中启用 `ocr_available: true` 并提供 OCR capability。Embedding 默认每批发送 16 个 chunk；已知上游支持更小或更大批量时，可在 `project.yaml` 设置 `embedding_batch_size`（1 至 256）。

## 生成计划

确认 `muye-llm` 与 Milvus 可用后，执行：

```bash
./scripts/muye.sh agent prepare agent-projects/hotel-employee
```

该命令校验模型 alias、解析资料和生成 chunk；LLM 只提出 Profile 与评测用例，输出会保存为 `config/generated/agent-creation-plans/<slug>/current.json`。审阅其中的职责边界、评测问题、`summary.evaluation_evidence` 中的来源定位与摘录、以及预计规模。此步骤不会写入 Milvus，也不会写入或覆盖 `config/knowledge-*`、`config/agents` 中的兼容配置。

## 确认并创建

确认计划后，使用输出中的完整 checksum 执行：

```bash
./scripts/muye.sh agent create agent-projects/hotel-employee \
  --plan-checksum <checksum> \
  --approved-by <principal>
```

命令会复核项目未漂移，构建不可变 Collection，自动启动仅监听 loopback 的 candidate `muye-data`，完成 Dense、Keyword 和 Hybrid 评测。评测 Job 的状态必须为 `SUCCEEDED`，才会发布 Snapshot、生成 `agents/agent-hotel-employee/` 并运行其契约测试。若已有同 slug 的高级配置且其内容不同，命令会拒绝覆盖并报告冲突；请改用新的 slug 或明确迁移原配置。

评测失败时不会发布新 Snapshot 或生成 Agent；根据 Job 报告调整资料结构、项目声明或评测计划后重新运行 `prepare`。生成后的契约测试失败时，可使用相同 checksum 重跑 `agent create`；它会验证目录仍精确对应同一计划，再仅重跑校验和测试，不覆盖目录。生成成功后的项目目录仍保留在 `agent-projects/`，生成源码位于 `agents/agent-<slug>/`。
