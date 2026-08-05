# 内网重建酒店员工知识库操作手册

本文用于将本仓库的酒店员工手册从原始文件重新构建到内网既有 Milvus，并供 Hermes 直连 `agent-hotel-employee` 做内部测试。

本流程不复制 Milvus 数据目录，不部署 MainAgent、Gateway、Control 或 PostgreSQL。知识 Worker 是唯一写入 Milvus 的组件；`muye-data` 和酒店 Agent 始终只读。

## 目标架构

```text
知识 Worker --写入--> 内网 Milvus
                         ^
muye-data（候选快照）----+--评测
                         ^
muye-data（active 快照）-+--> agent-hotel-employee <--- Hermes
                                      |
                                   muye-llm
```

本次知识库的固定逻辑资源为 `kb.hotel_employee`。成功构建后会创建一个由知识版本派生的不可变 Collection；不得手工向其中 insert、upsert、truncate 或删除数据。

## 前置条件

执行者需要拥有以下权限和条件：

- 内网 Linux 服务器，建议 x86_64、Git、`systemd`；构建过程需要可写的工作目录和足够存放 Python 依赖、原始资料及构建日志的磁盘空间。
- Python 3.11 或更高版本。本仓库根 `README.md` 的最低要求为 Python 3.11；所有 Python 命令使用项目根目录的 `.venv/bin/python`。
- 已运行的 Milvus，且构建账号只在构建窗口内具有创建 Collection、创建索引、插入和 load Collection 的权限。运行账号应为只读账号。
- Milvus 必须支持 `VARCHAR` analyzer、BM25 Function、`SPARSE_INVERTED_INDEX` 与 `FLAT + COSINE` 索引。服务器端 Milvus 与项目 `pymilvus` 版本须兼容。
- 可访问的 `muye-llm`，其中已注册 `text-embedding-v3`，并且该模型返回 1024 维向量。酒店 Agent 的对话模型别名为 `deepseek-v4-flash`。
- 从本地安全传输的原始资料目录 `agent-projects/hotel-employee/sources/`。当前资料为 `muye大酒店员工手册.md`；不要修改文件内容、文件名或目录层级。
- 已向 Milvus 管理员确认目标 database。以下示例使用 `default`；实际名称不同则同步修改 `muye-data/config.yaml`。

不要将 Milvus、LLM 或服务 token 写入 Git、命令历史、文档或 `config/*.yaml`。凭据只放在权限为 `0600` 的环境文件或密钥管理系统中。

### 组件版本与责任边界

下表只列出当前仓库实际锁定或实际使用的依赖。没有被仓库锁定的服务端版本不能假定为某个固定版本。

| 组件 | 当前项目要求 | 内网部署要求 |
| --- | --- | --- |
| Python | `>=3.11` | 使用 Python 3.11 或更高版本；推荐在构建、`muye-llm`、`muye-data` 与 Agent 中使用同一个项目 `.venv`。 |
| `pymilvus` | `>=2.5.0,<3.0.0`，见 `muye-data/requirements.txt` | 安装该范围内的客户端；服务端 Milvus 必须与所安装客户端兼容。 |
| Milvus Server | `>=v2.5.10` | 使用已被组织验证、且支持本流程所需 BM25 Function、稀疏向量和索引能力的版本。上线前在目标集群用最小测试 Collection 验证，不要仅依据版本号判断。 |
| `muye-llm` | 本仓库 Python 服务 | 必须提供 `text-embedding-v3`，维度为 1024；酒店 Agent 还需要 `deepseek-v4-flash`。上游供应商模型版本由 `muye-llm/.env` 管理。 |
| Docling | `requirements-knowledge-docling.txt` | 原始资料含 PDF/DOCX 时安装；Markdown/TXT 的确定性解析不依赖它。 |
| PaddleOCR | `requirements-knowledge-ocr.txt`，可选 | 只有扫描 PDF 且构建命令使用 `--ocr-available` 时安装。 |

当前项目不是通过 MinIO 导入资料，也不是将本地 Milvus 数据文件复制到 MinIO。原始员工手册经 Worker 解析、向量化后，由 Worker 通过 `pymilvus` 写入目标 Milvus。因此已有的内网 Milvus 集群不需要为本任务额外安装或升级 MinIO。

在实际构建前向 Milvus 管理员确认以下信息：Milvus Server 版本、部署形态（Standalone 或 Cluster）、database 名称、HTTP(S) 地址、构建账号与只读运行账号、以及当前集群是否已经启用 BM25 Function 和稀疏索引。建议内网目标环境与本地已验证环境保持相同的 Milvus major/minor 版本；若不一致，先在非生产 database 建立最小测试 Collection 验证，不要直接向生产 database 构建。

## 1. 准备服务器目录与代码

以下以 `/opt/muye/scaffold` 为部署目录、`muye` 为受限运行用户。目录可以替换，但后续命令必须保持一致。

```bash
sudo install -d -o muye -g muye /opt/muye
sudo -u muye git clone <内部仓库地址> /opt/muye/scaffold
cd /opt/muye/scaffold

if [ ! -x .venv/bin/python ]; then python3.11 -m venv .venv; fi
.venv/bin/python --version
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-knowledge-docling.txt
```

`.venv` 只在首次部署创建一次；后续升级依赖也继续使用这个环境，不创建额外的虚拟环境。

源文件配置使用 `docling-default-v1`。资料中包含扫描 PDF 时，另行安装 OCR 依赖，并在构建命令加入 `--ocr-available`；普通 Markdown、TXT 和可提取文本的 PDF/DOCX 不需要此步骤。

```bash
.venv/bin/python -m pip install -r requirements-knowledge-ocr.txt
```

## 2. 传输原始员工手册

导入根必须包含名为 `sources` 的子目录，因为 `config/knowledge-sources/hotel-employee.yaml` 固定引用该相对路径。

```bash
sudo install -d -o muye -g muye /opt/muye/imports/hotel-employee/sources
rsync -a --checksum \
  agent-projects/hotel-employee/sources/ \
  muye@<内网服务器>:/opt/muye/imports/hotel-employee/sources/
```

在服务器检查资料。不要使用符号链接；Worker 会拒绝指向导入根外部的路径。

```bash
cd /opt/muye/scaffold
find /opt/muye/imports/hotel-employee/sources -type f -printf '%P %s bytes\n'
sha256sum /opt/muye/imports/hotel-employee/sources/*
```

当前源配置限制单文件不超过 25 MiB、总资料不超过 100 MiB、最多 1000 个文件；接受 PDF、DOCX、Markdown 和 TXT。

## 3. 配置构建侧 LLM 与 Milvus

知识 Worker 读取下列环境变量：

| 变量 | 用途 |
| --- | --- |
| `MUYE_KNOWLEDGE_LLM_BASE_URL` | 已启动的内网 `muye-llm` 地址，例如 `http://127.0.0.1:9850` |
| `MUYE_KNOWLEDGE_MILVUS_URI` | 内网 Milvus HTTP(S) 地址，不携带用户名、密码或 token |
| `MUYE_KNOWLEDGE_MILVUS_TOKEN` | Milvus 构建账号 token，仅构建期间使用 |

如尚未运行 `muye-llm`，先复制 `muye-llm/.env.example` 为受限的 `muye-llm/.env`，配置聊天模型及 Embedding 模型。`MUYE_LLM_EMBED_MODELS_JSON` 必须包含 alias `text-embedding-v3` 且 `dimensions` 为 `1024`。随后启动它：

```bash
cd /opt/muye/scaffold/muye-llm
../.venv/bin/python main.py
```

在另一个 shell 建立仅当前会话可见的构建变量。不要在 shell history 中直接输入真实 token；生产环境应由密钥管理系统或 `systemd` 的受限 EnvironmentFile 注入。

```bash
export MUYE_KNOWLEDGE_LLM_BASE_URL=http://127.0.0.1:9850
export MUYE_KNOWLEDGE_MILVUS_URI=http://<milvus-host>:19530
export MUYE_KNOWLEDGE_MILVUS_TOKEN='<构建账号 token>'
```

Milvus URI 必须是 HTTP(S) URL。构建 Worker 不接受把凭据写进 URL 的形式。

## 4. 生成并人工确认 Schema Proposal

进入 Scaffold 根目录并运行 Proposal。该步骤解析源资料、计算知识版本、生成字段与索引计划，但不写入 Milvus。

```bash
cd /opt/muye/scaffold
./scripts/muye.sh knowledge propose-schema hotel-employee \
  --import-root /opt/muye/imports/hotel-employee
```

记录输出中的 `proposal_checksum`，审阅 `config/generated/knowledge-proposals/hotel-employee/current.json`。至少确认：

- `embedding_alias` 为 `text-embedding-v3`，`embedding_dimensions` 为 `1024`。
- `knowledge_id` 为 `kb.hotel_employee`，资料路径和文件数符合预期。
- 计划使用 hybrid 检索，并包含 Dense、BM25 和 Hybrid 索引。

审核人确认后写入审批记录。`<approved-by>` 应使用可审计的稳定英文标识，例如 `ops_alice`。

```bash
./scripts/muye.sh knowledge approve-proposal hotel-employee \
  --checksum <proposal_checksum> \
  --approved-by <approved-by>
```

只要源文件、分块规则、Embedding 配置或 Proposal 有变化，就必须重新执行 Proposal 和确认。不要复用旧 checksum。

## 5. 构建不可变 Milvus Collection

执行构建。Worker 会调用 `muye-llm` 生成 Embedding，再在内网 Milvus 创建 Collection、BM25 Function 和索引并写入 chunks。

```bash
cd /opt/muye/scaffold
./scripts/muye.sh knowledge build hotel-employee \
  --import-root /opt/muye/imports/hotel-employee \
  --llm-base-url "$MUYE_KNOWLEDGE_LLM_BASE_URL" \
  --milvus-uri "$MUYE_KNOWLEDGE_MILVUS_URI"
```

资料确实需要 OCR 时改为：

```bash
./scripts/muye.sh knowledge build hotel-employee \
  --import-root /opt/muye/imports/hotel-employee \
  --ocr-available \
  --llm-base-url "$MUYE_KNOWLEDGE_LLM_BASE_URL" \
  --milvus-uri "$MUYE_KNOWLEDGE_MILVUS_URI"
```

成功后记录输出的 `job_id`，并检查 Job 与 Manifest：

```bash
./scripts/muye.sh knowledge status <job_id>
find config/generated/knowledge-manifests/hotel-employee -type f -maxdepth 1 -print
```

构建失败时，检查 `config/generated/knowledge-reports/<job_id>.json`。修正依赖、资料或 Milvus 权限后，使用 `knowledge retry` 创建新 attempt；不得手工补写 Collection。若同名 Collection 已存在，Worker 只会验证 schema、索引与所有 chunk 的 hash，绝不覆盖或追加。

## 6. 启动隔离的候选 muye-data 并评测

构建成功只会生成 `resource-snapshot.candidate.json`，尚未发布。评测必须通过一个指向候选快照的隔离 `muye-data` 进程完成。

先创建最小连接配置 `/opt/muye/scaffold/muye-data/config.yaml`。这里不声明静态资源，资源完全由候选快照加载。

```yaml
version: 1
connections:
  milvus_default:
    type: milvus
    uri: http://<milvus-host>:19530
    token_env: MUYE_DATA_MILVUS_TOKEN
    database: default
resources: {}
```

创建仅候选服务使用的环境文件 `/etc/muye/muye-data-candidate.env`，权限设为 `0600`。以下值为示例，不要将真实 token 写入本文档或版本库。

```text
MUYE_DATA_HOST=127.0.0.1
MUYE_DATA_PORT=19840
MUYE_DATA_CONFIG_PATH=/opt/muye/scaffold/muye-data/config.yaml
MUYE_DATA_LLM_BASE_URL=http://127.0.0.1:9850
MUYE_DATA_MILVUS_TOKEN=<只读 Milvus token>
MUYE_DATA_RESOURCE_SNAPSHOT_PATH=/opt/muye/scaffold/config/generated/resource-snapshot.candidate.json
MUYE_DATA_AGENT_AUTH_ENABLED=false
```

以临时前台进程启动候选服务。它只监听 loopback，不向 Hermes 开放。

```bash
cd /opt/muye/scaffold/muye-data
set -a
. /etc/muye/muye-data-candidate.env
set +a
../.venv/bin/python main.py
```

另一个 shell 中确认候选身份与依赖状态：

```bash
curl --fail http://127.0.0.1:19840/api/v1/snapshot-identity
curl --fail http://127.0.0.1:19840/ready
```

运行固定评测。评测会检查 Recall、MRR 和 citation coverage；未通过时 active Snapshot 不会改变。

```bash
cd /opt/muye/scaffold
./scripts/muye.sh knowledge evaluate hotel-employee \
  --data-url http://127.0.0.1:19840
```

检查输出的 `job_id` 和 `config/generated/knowledge-reports/<job_id>.json`。成功后，Worker 原子写入 `config/generated/resource-snapshot.json`，即发布该知识版本。

## 7. 启动运行时 muye-data 和酒店 Agent

运行时 `muye-data` 使用 active Snapshot。创建独立的运行环境文件，并使用只读 Milvus token：

```text
MUYE_DATA_HOST=127.0.0.1
MUYE_DATA_PORT=9840
MUYE_DATA_CONFIG_PATH=/opt/muye/scaffold/muye-data/config.yaml
MUYE_DATA_LLM_BASE_URL=http://127.0.0.1:9850
MUYE_DATA_MILVUS_TOKEN=<只读 Milvus token>
MUYE_DATA_RESOURCE_SNAPSHOT_PATH=/opt/muye/scaffold/config/generated/resource-snapshot.json
MUYE_DATA_AGENT_AUTH_ENABLED=false
```

启动后检查：

```bash
curl --fail http://127.0.0.1:9840/api/v1/snapshot-identity
curl --fail http://127.0.0.1:9840/ready
```

为酒店 Agent 创建受限 `.env` 或 `systemd` EnvironmentFile，至少设置：

```text
MUYE_LLM_BASE_URL=http://127.0.0.1:9850
MUYE_SDK_DATA_BASE_URL=http://127.0.0.1:9840
MUYE_AGENT_SERVICE_ID=hotel-employee-internal
MUYE_AGENT_DEPLOYMENT_ID=hotel-employee-internal-v1
MUYE_AGENT_DESCRIPTOR_CHECKSUM=<64 位 descriptor checksum>
MUYE_AGENT_SOURCE_TREE_CHECKSUM=<64 位 source tree checksum>
MUYE_AGENT_MAIN_TOKEN=<Hermes 内部调用 token>
MUYE_AGENT_CONTROL_TOKEN=<不同的随机 token>
MUYE_AGENT_DATA_TOKEN=<不同的随机 token>
```

三个 Agent token 必须非空且互不相同。当前直连测试中，Hermes 用 `MUYE_AGENT_MAIN_TOKEN` 调用 `/invoke`；不要把该 token 发送到浏览器或公网。

在未接入 Catalog 的直连测试中，仍需提供两个 64 位 identity checksum。可在每次源码或 `agent.yaml` 改动后，使用项目自己的稳定算法重新计算，再写入受限环境文件：

```bash
cd /opt/muye/scaffold
.venv/bin/python -c 'from pathlib import Path; from contracts.models import AgentDescriptorV1; from tools.agent_generator.checksums import canonical_checksum; from tools.agent_generator.io import load_yaml_model; descriptor = load_yaml_model(Path("agents/agent-hotel-employee/agent.yaml"), AgentDescriptorV1); print(canonical_checksum(descriptor.model_dump(mode="json")))'
.venv/bin/python -c 'from pathlib import Path; from tools.agent_generator.checksums import source_tree_checksum; print(source_tree_checksum(Path("agents/agent-hotel-employee")))'
```

第一条输出对应 `MUYE_AGENT_DESCRIPTOR_CHECKSUM`，第二条输出对应 `MUYE_AGENT_SOURCE_TREE_CHECKSUM`。这不是 Catalog 部署的替代品；它只保证内部直连测试的 Agent identity 与本地源码一致。

## 8. 用 systemd 守护服务

内部测试至少应以 `systemd` 守护 `muye-llm`、运行时 `muye-data` 与 `agent-hotel-employee`。下面是酒店 Agent 的示例单位；其他两个服务沿用相同模式，替换 `WorkingDirectory`、`EnvironmentFile` 和启动命令。

```ini
[Unit]
Description=Muye hotel employee internal agent
After=network-online.target muye-llm.service muye-data.service
Wants=network-online.target

[Service]
Type=simple
User=muye
Group=muye
WorkingDirectory=/opt/muye/scaffold/agents/agent-hotel-employee
EnvironmentFile=/etc/muye/agent-hotel-employee.env
ExecStart=/opt/muye/scaffold/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/muye/scaffold/agents/agent-hotel-employee

[Install]
WantedBy=multi-user.target
```

保存为 `/etc/systemd/system/muye-hotel-employee.service` 后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now muye-hotel-employee.service
sudo systemctl status muye-hotel-employee.service
journalctl -u muye-hotel-employee.service -f
```

只允许 Hermes 所在网段访问 Agent 的 `8000` 端口；`muye-llm`、`muye-data` 和候选服务保持 loopback 或仅内部服务网段可见。

## 9. 最终验证与回滚

用已通过评测的问题调用 Agent，确认返回答案及引用。请求必须包含 Agent 的 Main token；下例仅展示结构。

```bash
curl --fail-with-body http://<agent-host>:8000/invoke \
  -H 'Authorization: Bearer <MUYE_AGENT_MAIN_TOKEN>' \
  -H 'Content-Type: application/json' \
  --data '{
    "task": "入职需要提交哪些证件？",
    "context": {
      "user_id": "hermes-test-user",
      "session_id": "hotel-smoke-001",
      "trace_id": "hotel-smoke-001"
    }
  }'
```

检查响应为 `success`，正文非空，并且 `result_data._muye_citations` 存在。发布后发现问题时，不要删除 Collection：将运行时 `MUYE_DATA_RESOURCE_SNAPSHOT_PATH` 指向上一个已验证的 Snapshot，重启或等待 `muye-data` 轮询加载即可回退。待问题分析完成后再构建新的知识版本。

## 常见故障

| 现象 | 优先检查 |
| --- | --- |
| Proposal 后无法审批 | 原始资料或配置发生变化；重新 Proposal，使用新的 checksum 审批。 |
| Build 提示 Embedding 维度不匹配 | `muye-llm` 中 `text-embedding-v3` 不是 1024 维，或上游模型配置错误。 |
| Build 提示 Milvus Function/Index 不支持 | Milvus 版本或部署能力不支持 BM25 Function / sparse index；升级或更换兼容集群。 |
| Candidate `/ready` 为 503 | 检查 Milvus 地址、只读 token、Collection 是否已 load，以及 `muye-llm` 中的 Embedding alias。 |
| Evaluate 未通过 | 查看评测报告中的具体 query 和 citation；修正资料或检索配置后重建新版本，不修改现有 Collection。 |
| Agent 返回 401/403 | Hermes 使用的 token 与 `MUYE_AGENT_MAIN_TOKEN` 不一致，或 Agent 三个 token 为空/重复。 |
