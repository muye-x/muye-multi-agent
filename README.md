<div align="center">

# Muye Multi-Agent Scaffold v2.1

**开源、自托管的知识 Agent 构建与多 Agent 运行脚手架**

业务资料 -> 知识构建与评测 -> 独立 SubAgent -> 本地联调 -> 可审计部署

[English](README.en.md) · 简体中文

![License](https://img.shields.io/badge/license-MIT-0f766e) ![Python](https://img.shields.io/badge/python-3.11%2B-0f766e) ![Architecture](https://img.shields.io/badge/architecture-Multi--Agent-0f766e) ![Protocol](https://img.shields.io/badge/streaming-SSE-0f766e)

![Muye v2.1 本地 Agent 联调界面](docs/images/v2.1-local-agent-chat.png)

</div>

Muye Multi-Agent Scaffold 基于
[`muye-multi-agent-sdk`](https://github.com/muye-x/muye-multi-agent-sdk)，将业务资料直接转换为可运行的知识 Agent。单条命令即可完成知识构建、质量评测、代码生成和本地 Web 联调：

```bash
./scripts/muye.sh agent prepare agent-projects/<slug> \
  --auto-approved-by <reviewer> \
  --dev
```

开发者无需先构建 Docker 镜像、发布 Catalog 或配置正式用户权限，即可验证完整的
Gateway -> MainAgent -> SubAgent 调用链路。

> 面向需要将受控业务资料交付为可验证知识 Agent 的开发团队。脚手架将“能生成”与“可评测、可审计、可部署”放在同一条工作流中。

## 1. 解决的问题

| 开发与交付难点 | Scaffold 提供的能力 |
| --- | --- |
| **资料难以成为可运行 Agent** | 从受版本控制的 `project.yaml` 和源资料生成独立 SubAgent、描述符和契约测试。 |
| **检索质量无法证明** | 对不可变 Milvus Collection 执行 Dense、Keyword、Hybrid 评测；达标后才发布 Resource Snapshot。 |
| **本地链路难以复现** | 一条命令启动或复用 LLM、Data、MainAgent、SubAgent 与 Vue Web Gateway。 |
| **流式过程难以调试** | Web 对话展示 SSE 正文、工具执行、citation、错误和逐轮原始事件。 |
| **开发与生产授权相互干扰** | local-dev 使用临时身份和运行目录，正式 Catalog、BuildRecord 与用户 grant 保持隔离。 |
| **Agent 生命周期缺少约束** | 提供构建、Catalog 同步、部署、停止和回滚命令，并校验 checksum、健康状态与调用链。 |

## 2. 能力地图

| 阶段 | 主要能力 | 产出或保障 |
| --- | --- | --- |
| **定义** | `project.yaml`、业务资料、创建计划与审批记录 | 可版本控制的 Agent 输入与可追溯确认。 |
| **知识构建** | 文档解析、不可变 Collection、Embedding 与资源快照 | 资料、知识版本和 Milvus 实体边界清晰。 |
| **质量评测** | Dense、Keyword、Hybrid 检索与 citation 覆盖门禁 | 未通过评测的候选不会成为 active Snapshot。 |
| **生成与验证** | 模板生成、描述符、检索测试与契约测试 | 独立、可验证的 `agents/agent-<slug>/`。 |
| **本地联调** | MainAgent 编排、SSE、Vue Web Gateway 与调试抽屉 | 端到端调用链在生产发布前可真实验证。 |
| **生产生命周期** | Catalog、grant、健康检查、部署、停止与回滚 | 部署状态和授权数据受控且可审计。 |

## 3. 架构

```text
Web / API Client
       |
       v
muye-gateway  -->  agent-main  -->  agent-<slug>
                         |               |
                         v               v
                     muye-llm         muye-data  -->  Milvus
                         |
                         +----------> OpenAI-compatible models

control  -->  Catalog / grant / health / citation authorization
```

| 服务 | 默认端口 | 职责 |
| --- | ---: | --- |
| `muye-llm` | 9850 | Chat、SSE、Embedding 与模型别名网关 |
| `muye-data` | 9840 | Dense、Keyword、Hybrid 检索与 Rerank 编排 |
| `agent-<slug>` | 8000 | 生成的业务知识 SubAgent |
| `agent-main` | 9860 | 对话、工具调用与 SubAgent 编排 |
| `control` | 9880 | Catalog、授权、健康状态与 citation 投影 |
| Web Dev Gateway | 5173 | v2.1 本地对话和 SSE 调试界面 |
| `muye-gateway` | 80/443 | 正式环境 TLS、鉴权与公网路由 |

## 4. 快速开始

### 1. 安装依赖

需要 Python 3.11+。本项目统一使用根目录 `.venv`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

### 2. 配置模型与知识构建服务

首次创建知识 Agent 只需准备两个配置文件：

```bash
cp muye-llm/.env.example muye-llm/.env
cp tools/agent_creation/.env.example tools/agent_creation/.env
```

在 `muye-llm/.env` 中配置 OpenAI-compatible Chat/Embedding 上游、密钥和模型别名；在
`tools/agent_creation/.env` 中配置创建工具使用的服务地址：

```dotenv
MUYE_KNOWLEDGE_LLM_BASE_URL=http://127.0.0.1:9850
MUYE_KNOWLEDGE_MILVUS_URI=http://127.0.0.1:19530
MUYE_KNOWLEDGE_MILVUS_TOKEN=
```

密钥、Token 和数据库连接串只能放在对应服务的 `.env` 或运行环境中，不要写入 `project.yaml`。

### 3. 启动依赖

启动模型网关：

```bash
cd muye-llm
../.venv/bin/python main.py
```

若没有可用的 Milvus，在另一个终端启动仓库提供的本地开发环境：

```bash
./poc/phase1/milvus/start-local.sh
```

若使用已有或托管的 Milvus，只需修改 `MUYE_KNOWLEDGE_MILVUS_URI`，不要再启动本地 Compose。

### 4. 准备资料

资料项目放在 `agent-projects/`，生成结果会写入 `agents/`：

```text
agent-projects/<slug>/
├── project.yaml
└── sources/
    ├── handbook.md
    └── policy.pdf
```

`project.yaml` 用于描述 Agent 身份、目标、禁止行为、模型别名和示例问题。Markdown/TXT 可直接处理；
DOCX/PDF 需要安装相应解析依赖。

仓库内置的 [`agent-projects/hotel-employee/`](agent-projects/hotel-employee/) 是可直接执行的示例输入项目，
包含项目定义和员工手册资料。它不包含 `agents/agent-hotel-employee/`、创建过程配置或 Milvus 数据；这些均由下一步命令在本地生成。这样可以保证示例资料、生成结果和向量数据库的边界清晰，也让首次运行真实验证从资料创建 Agent 的流程。

### 5. 一条命令生成并联调

回到仓库根目录执行：

```bash
./scripts/muye.sh agent prepare agent-projects/<slug> \
  --auto-approved-by <reviewer> \
  --dev
```

该命令会依次完成：

1. 读取项目描述和资料，生成创建计划及 checksum 审批记录。
2. 构建不可变 Milvus Collection，并执行 Dense、Keyword、Hybrid 检索评测。
3. 发布 active Resource Snapshot，生成 `agents/agent-<slug>/` 源码和描述符。
4. 运行生成 Agent 的契约与检索测试。
5. 启动完整本地调用链和 Web Dev Gateway。

`--auto-approved-by` 只记录审批人，不会绕过资料漂移、Embedding、Milvus 或检索评测门禁。

启动成功后访问：

```text
http://127.0.0.1:5173/chat
```

按 `Ctrl+C` 停止本次开发会话。再次联调已生成的 Agent 时可直接运行：

```bash
./scripts/muye.sh agent dev <slug>
```

## 5. Web 联调体验

v2.1 的 `/chat` 页面用于检查真实的 MainAgent -> SubAgent 执行过程：

- 正文按照 SSE `block.delta` 到达顺序流式显示。
- 工具调用、检索日志、citation 等信息归入可折叠的“思考过程”。
- 支持 Markdown、GFM 表格、代码块和列表。
- 每次问答的原始 SSE 事件按 `session_start` 到 `session_end` 独立折叠展示。
- 对话、思考过程和 SSE 调试记录保存在浏览器 `localStorage`，历史对话可单独删除。
- 对话区独立滚动，输入区固定置底，并支持终止当前流式请求。

## 6. 常用命令

| 命令 | 用途 |
| --- | --- |
| `./scripts/muye.sh agent prepare <project> --auto-approved-by <reviewer> --dev` | 从资料生成 Agent 并立即联调 |
| `./scripts/muye.sh agent dev <slug>` | 重新启动已生成 Agent 的本地联调环境 |
| `./scripts/muye.sh agent list` | 查看已生成或已登记的 Agent |
| `./scripts/muye.sh agent validate <slug>` | 校验生成产物和契约 |
| `./scripts/muye.sh agent build <slug> --base-image '<image>@sha256:<digest>'` | 测试并构建正式镜像记录 |
| `./scripts/muye.sh agent sync --check` | 检查 Catalog 聚合结果 |
| `./scripts/muye.sh agent deploy <slug>` | 部署 Agent 并执行调用链 smoke test |
| `./scripts/muye.sh agent stop <slug>` | 从 Catalog 移除并停止 Agent |
| `./scripts/muye.sh agent rollback <slug> --build-record <id>` | 回滚到指定构建记录 |

需要人工审阅创建计划、处理资料变更或接入 CI 时，可使用分步审批流程。详见
[一键创建和测试知识 Agent](docs/agent-creation-quickstart.md)。

## 7. 流式协议

SubAgent 提供 `/health`、`/capabilities`、`/invoke`、`/invoke/stream` 和 `/cancel` internal API。
流式事件生命周期为：

```text
session_start -> block / tool / thinking -> done -> session_end
```

同一 `block.id` 的 `delta` 按到达顺序追加；不同 block 必须独立处理。

## 8. 测试

```bash
PYTHONPATH=muye-llm:muye-gateway \
  .venv/bin/python -m pytest -q muye-llm/tests muye-gateway/dashboard_api/tests
PYTHONPATH=muye-data \
  .venv/bin/python -m pytest -q muye-data/tests
PYTHONPATH=agents/agent-main \
  .venv/bin/python -m pytest -q agents/agent-main/tests
.venv/bin/python -m pytest -q tests
.venv/bin/python main.py --dry-run
```

## 9. 文档

英文读者可先查看 [English documentation index](docs/README.en.md)，以获取各中文权威文档的英文说明和入口。

- [一键创建和测试知识 Agent](docs/agent-creation-quickstart.md)
- [模板 Agent Generator 与开发者接管](docs/v2.0-agent-generator.md)
- [知识 Pipeline 与评测](docs/v2.0-knowledge-pipeline.md)
- [Agent Catalog、权限与部署](docs/v2.0-agent-catalog.md)
- [管理员指南](docs/v2.0-admin-guide.md)
- [运维指南](docs/v2.0-operations.md)
- [发布检查表](docs/v2.0-release-checklist.md)
- [微信 Channel 接入](docs/wechat-channel.md)

## 10. 安全边界

- `agent dev` 只监听 loopback，每次仅注册当前 SubAgent，并使用临时随机 Token 和 local-dev 身份。
- 本地联调数据写入 `config/runtime/dev/<slug>/`，不会修改正式 Control Catalog、BuildRecord 或用户 grant。
- `muye-data` 与 `muye-llm` 只应由可信内网服务访问；数据库账号必须限制为只读权限。
- 生产环境只公开 Gateway 的 Web、`/api/v2/` 和 `/agentMain/`，所有 SubAgent 均使用 internal profile。

## 许可证

本项目采用 [MIT License](LICENSE) 发布。第三方资源许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
