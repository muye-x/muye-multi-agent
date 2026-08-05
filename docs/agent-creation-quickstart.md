# 两步创建知识 Agent

本指南面向只持有资料文件、希望生成本地可测试 Agent 源码的使用者。该流程不会构建镜像、部署容器或授予用户访问权限；文中的 `<slug>`、`<agent_id>` 和 `<document>` 均为使用者自行替换的占位符。

## `muye.sh` 简要说明

`./scripts/muye.sh` 是 Scaffold 的统一入口，在 Scaffold 根目录执行。`agent prepare` 只解析资料、生成可审阅计划；`agent create` 在人工确认 checksum 后才写入 Milvus、运行发布前检索评测并生成源码。其他常用命令包括：`up`、`down`、`restart` 管理 Compose 服务，`status` 查看服务状态，`logs <service>` 查看日志，`doctor` 检查本地依赖，`smoke` 执行系统冒烟检查。通过 `./scripts/muye.sh agent --help` 查看 Agent 子命令参数。

## 前置准备

创建流程需要一个可用的 `muye-llm` 和可访问的 Milvus。按模块分别配置，根目录 `.env` 不参与此流程：

1. 将 `muye-llm/.env.example` 复制为 `muye-llm/.env`，配置 Chat、Embedding 上游凭据和模型注册；`project.yaml` 默认使用的 `chat-default` 与 `text-embedding-default` 必须已注册。
2. 启动 `muye-llm`，并确认其地址可由本机访问。默认地址是 `http://127.0.0.1:9850`。
3. 将 `tools/agent_creation/.env.example` 复制为 `tools/agent_creation/.env`，填写该流程使用的 LLM 地址、Milvus 地址，以及需要时的 Milvus token。

`muye-llm/.env` 管理上游 LLM 与 Embedding 密钥；`tools/agent_creation/.env` 只管理创建流程到已启动服务的连接，不能把密钥或数据库连接写入项目 YAML。

## 项目目录

创建一个项目目录，并将 Markdown、TXT、DOCX 或 PDF 放入 `sources/`。项目目录必须位于 `agents/` 之外，因为 `agents/agent-<slug>/` 是 `create` 的最终输出目录，不能预先存放项目文件。

```text
agent-projects/<slug>/
├── project.yaml
└── sources/
    └── <document>.md
```

只有一个 Markdown 文件时，可先执行：

```bash
mkdir -p agent-projects/<slug>/sources
cp /path/to/<document>.md agent-projects/<slug>/sources/
```

最小 `project.yaml`：

```yaml
schema_version: muye.ai/agent-project/v1
slug: <slug>
agent_id: <agent_id>
display_name: <agent_display_name>
objective: 根据指定资料回答领域问题，并提供来源引用。
prohibited_actions:
  - 不得把模型常识冒充资料规定
  - 没有检索依据时不得猜测
  - 不执行任何外部系统操作
examples:
  - <一个典型领域问题>
```

Markdown 与 TXT 无需 Docling；DOCX/PDF 需要 Docling，扫描 PDF 还需要在 `project.yaml` 中启用 `ocr_available: true` 并提供 OCR capability。Embedding 默认每批发送 16 个 chunk；已知上游支持更小或更大批量时，可在 `project.yaml` 设置 `embedding_batch_size`（1 至 256）。

## 生成计划

确认 `muye-llm` 与 Milvus 可用后，执行：

```bash
./scripts/muye.sh agent prepare agent-projects/<slug>
```

该命令校验模型 alias、解析资料和生成 chunk；LLM 只提出 Profile 与评测用例，输出会保存为 `config/generated/agent-creation-plans/<slug>/current.json`。审阅其中的职责边界、评测问题、`summary.evaluation_evidence` 中的来源定位与摘录、以及预计规模。此步骤不会写入 Milvus，也不会写入或覆盖 `config/knowledge-*`、`config/agents` 中的兼容配置。

需要由已指定审核人直接确认当前计划时，可将两步合并为一次命令：

```bash
./scripts/muye.sh agent prepare agent-projects/<slug> \
  --auto-approve \
  --approved-by <principal>
```

该模式会先生成计划，再以本次生成的完整 checksum 执行 `create`；`<principal>` 会写入 creation、schema、resource、skill 和 profile 审批记录。它会跳过人工审阅计划的停顿，但不会跳过资料漂移复核、Milvus 构建、检索评测或生成后契约测试。`--approved-by` 只能与 `--auto-approve` 一起使用。

## 确认并创建

确认计划后，使用输出中的完整 checksum 执行：

```bash
./scripts/muye.sh agent create agent-projects/<slug> \
  --plan-checksum <checksum> \
  --approved-by <principal>
```

命令会复核项目未漂移，构建不可变 Collection，自动启动仅监听 loopback 的 candidate `muye-data`，完成 Dense、Keyword 和 Hybrid 评测。评测 Job 的状态必须为 `SUCCEEDED`，才会发布 Snapshot、生成 `agents/agent-<slug>/` 并运行其契约测试。若已有同 slug 的高级配置且其内容不同，命令会拒绝覆盖并报告冲突；请改用新的 slug 或明确迁移原配置。

评测失败时不会发布新 Snapshot 或生成 Agent；根据 Job 报告调整资料结构、项目声明或评测计划后重新运行 `prepare`。生成后的契约测试失败时，可使用相同 checksum 重跑 `agent create`；它会验证目录仍精确对应同一计划，再仅重跑校验和测试，不覆盖目录。生成成功后的项目目录仍保留在 `agent-projects/`，生成源码位于 `agents/agent-<slug>/`。

生成目录包含 `.env.example`。本地启动 Agent 前复制为 `.env`，填写 LLM/Data 地址、部署身份和三个互不相同的内部 token；真实 token 只保存在该模块的 `.env` 或由部署环境注入，不能写入 `agent.yaml`、项目资料或 Git。

## 生成后的三层测试

进入生成目录后，按以下顺序执行。第一项离线执行；后两项只读访问已发布 Snapshot 与已启动的 `muye-llm`，不会重新写入 Milvus。

```bash
cd agents/agent-<slug>
../../.venv/bin/python -m pytest -q tests/test_contract.py
```

`test_contract.py` 检查生成描述符、固定资源、模型预算和 SDK 协议，不验证问答质量。

为后两项测试启动一个仅监听 loopback 的 `muye-data`。在另一个终端执行：

```bash
cd muye-data
set -a
source .env
set +a
MUYE_DATA_HOST=127.0.0.1 \
MUYE_DATA_PORT=19840 \
MUYE_DATA_CONFIG_PATH=../config/generated/agent-creation-candidates/<slug>.yaml \
MUYE_DATA_RESOURCE_SNAPSHOT_PATH=../config/generated/resource-snapshot.json \
MUYE_DATA_AGENT_AUTH_ENABLED=false \
../.venv/bin/python main.py
```

若 Milvus 要求 token，还应在启动前将其映射到 candidate 配置使用的 `MUYE_KNOWLEDGE_MILVUS_TOKEN`，不要在命令或项目 YAML 中写入实际值。确认 `muye-llm` 已运行后，在 Agent 目录执行：

```bash
export MUYE_TEST_DATA_BASE_URL=http://127.0.0.1:19840
export MUYE_TEST_LLM_BASE_URL=http://127.0.0.1:9850

../../.venv/bin/python -m pytest -q tests/test_retrieval.py
../../.venv/bin/python -m pytest -q tests/test_e2e.py
```

`test_retrieval.py` 复跑创建时固化的评测集，验证 Dense、Keyword、Hybrid 的 Recall、MRR 和引用覆盖率。`test_e2e.py` 从评测集选择一个问题，通过 Agent 的 internal `/invoke` 入口验证真实回答、检索工具调用和可信引用。未设置测试服务地址时，这两项会明确标记为 skipped，不会隐式访问网络。
