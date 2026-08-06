# 一键创建和测试知识 Agent

本指南面向已有资料文件、希望在本地生成并立即联调知识 Agent 的使用者。推荐工作流会在创建成功后启动 Gateway -> Main -> SubAgent，本地验证真实编排而不进入镜像构建或发布流程。

```text
准备本地依赖和配置
        ↓
agent prepare --auto-approved-by <reviewer> --dev
        ↓
http://127.0.0.1:5173/chat
```

自动审批只省去人工确认计划的停顿；资料漂移检查、Milvus 构建、检索评测、Snapshot 发布和生成后的契约测试仍然必须通过。流程不会构建镜像、部署容器或授予终端用户访问权限。

## 1. 准备配置

在 Scaffold 根目录使用已有的 `.venv`。创建流程不读取根目录 `.env`，而是读取各模块自己的 `.env`。

```bash
cp muye-llm/.env.example muye-llm/.env
cp tools/agent_creation/.env.example tools/agent_creation/.env
```

编辑 `muye-llm/.env`，配置 Chat 和 Embedding 的 OpenAI-compatible 上游地址、密钥与模型注册表。项目的 `project.yaml` 中声明的 `chat_model_alias` 和 `embedding_model_alias` 必须是 `MUYE_LLM_MODELS_JSON` 与 `MUYE_LLM_EMBED_MODELS_JSON` 中已经注册的 alias。

`tools/agent_creation/.env` 只配置创建工具到本地服务的连接。默认值适用于本机运行：

```dotenv
MUYE_KNOWLEDGE_LLM_BASE_URL=http://127.0.0.1:9850
MUYE_KNOWLEDGE_MILVUS_URI=http://127.0.0.1:19530
```

不要把密钥或数据库连接串写进 `project.yaml`。

## 2. 启动本地依赖

先启动 `muye-llm`。保持该终端运行，或使用你的进程管理工具后台运行它：

```bash
cd muye-llm
../.venv/bin/python main.py
```

在另一个终端确认服务和模型注册可用：

```bash
curl --noproxy "*" http://127.0.0.1:9850/health
curl --noproxy "*" http://127.0.0.1:9850/api/v2/models
```

接着选择 Milvus 环境：

- 已有 Milvus：将 `MUYE_KNOWLEDGE_MILVUS_URI` 改为已有服务地址；服务启用鉴权时，再设置 `MUYE_KNOWLEDGE_MILVUS_TOKEN`。不要执行本地 Compose 脚本。
- 没有 Milvus：使用仓库提供的本地启动脚本。Milvus standalone 使用 MinIO 保存对象和索引；脚本会在首次运行时生成随机本地凭据，写入被 Git 忽略且权限为 `0600` 的 `poc/phase1/milvus/.env`，后续运行复用它。

```bash
./poc/phase1/milvus/start-local.sh
```
## 3. 准备资料项目

资料项目位于 `agent-projects/`，但生成后的源码位于 `agents/agent-<slug>/`。两者不能混用，且目标 Agent 目录不能预先存在。

```text
agent-projects/<slug>/
├── project.yaml
└── sources/
    └── <document>.md
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

Markdown 与 TXT 可直接处理。DOCX/PDF 需要 Docling；扫描 PDF 还需在 `project.yaml` 启用 `ocr_available: true` 并提供相应 OCR capability。`embedding_batch_size` 可按上游模型限制设置为 `1` 到 `256`。

## 4. 一键生成

回到 Scaffold 根目录，使用自动审批命令创建 Agent：

```bash
./scripts/muye.sh agent prepare agent-projects/<slug> \
  --auto-approved-by <reviewer> \
  --dev
```

示例：
```bash
 ./scripts/muye.sh agent prepare agent-projects/hotel-employee \
  --auto-approved-by jimmy \
  --dev
```
该命令会依次完成：解析资料、生成 Profile 和评测计划、写入 `<reviewer>` 的审批记录、构建不可变 Milvus Collection、执行 Dense/Keyword/Hybrid 检索评测、发布 active Resource Snapshot、生成 `agents/agent-<slug>/`，并执行生成 Agent 的契约测试。

如果构建失败，CLI 会输出 Knowledge Job ID、错误码和报告路径。常见原因是 Embedding 上游不可达、Milvus 未启动、模型 alias 未注册或资料/评测不满足发布门禁；修正问题后重新运行同一条自动审批命令。

## 5. 一键启动和测试

自动审批命令包含 `--dev` 时会在创建后立即启动联调。已有生成 Agent 则执行：

```bash
./scripts/muye.sh agent dev <slug>
```

该命令会执行以下操作：

- 复用或启动本地 `muye-llm`；
- 启动仅监听 `127.0.0.1:9840` 的 `muye-data`，并保留 SubAgent token、identity 与 Resource 校验；
- 启动当前 Agent、local-dev Main 和仅监听 loopback 的 Vue Gateway；
- 为当前会话创建一个单 Agent 的临时注册表及互不相同的内部 token；
- 输出 `http://127.0.0.1:5173/chat`，以浏览器验证 SSE、工具步骤、citations 与错误事件。

local-dev 注册表不会写入生产 Catalog 或 Control grant；按 Ctrl+C 后，命令只停止本次会话创建的进程，并删除临时运行文件。根 `main.py` 不会因生成 Agent 而发生改动。

`run-local.sh` 仍保留为 SubAgent 单体自测入口。需要排查 Agent 本身时，进入生成目录后执行：

```bash
cd agents/agent-<slug>
./run-local.sh
curl --noproxy "*" http://127.0.0.1:8000/health

curl --noproxy "*" -X POST http://127.0.0.1:8000/invoke \
  -H "Authorization: Bearer $(sed -n 's/^MUYE_AGENT_MAIN_TOKEN=//p' .env)" \
  -H "Content-Type: application/json" \
  -d '{"task":"请根据资料回答一个问题。","context":{"user_id":"local_user","session_id":"local_session"}}'
```

`/invoke` 返回 `tool_calls_made`、回答和 citations。资料没有足够依据时，Agent 应明确说明无法确认，而不是编造答案。

`.env` 包含 `run-local.sh` 产生的本地 token，不应提交或粘贴到终端记录之外；需要轮换这些 token 时，删除该 Agent 的 `.env` 后重新执行 `./run-local.sh`。

## 高级用法：手动审批

只有在需要审阅模型生成的计划、定位资料问题或接入 CI 时，才使用手动两步流程：

```bash
./scripts/muye.sh agent prepare agent-projects/<slug>
./scripts/muye.sh agent create agent-projects/<slug> \
  --plan-checksum <prepare 输出的完整 checksum> \
  --approved-by <reviewer>
```

`prepare` 生成的可审阅计划保存在 `config/generated/agent-creation-plans/<slug>/current.json`。手动模式与自动审批模式使用相同的构建、评测和生成门禁；区别只在于审批发生的时机。
