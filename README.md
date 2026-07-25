# Muye Multi-Agent Scaffold

Muye Multi-Agent Scaffold 是一个基于
[`muye-multi-agent-sdk`](https://github.com/muye-x/muye-multi-agent-sdk) 的多服务参考工程。仓储包含
统一模型网关、主编排 Agent、两个子 Agent 示例以及可选的 Nginx Gateway；SDK 在独立仓储
维护，并通过 Python 包依赖接入。

## 界面预览

<table>
  <tr>
    <th>服务概览</th>
    <th>SDK 架构与部署</th>
    <th>在线体验</th>
  </tr>
  <tr>
    <td><img src="docs/images/dashboard-overview.png" alt="Muye 运维控制台的服务拓扑与实时状态" width="100%"></td>
    <td><img src="docs/images/sdk-deployment-guide.png" alt="Muye SDK 架构与部署说明" width="100%"></td>
    <td><img src="docs/images/online-experience.png" alt="Muye Multi-Agent 在线对话体验" width="100%"></td>
  </tr>
</table>

## 架构

```text
Client
  |
  +-- muye-gateway: 80/443 (可选，仅 /agentMain/ 与 /api/v1/travel/)
          |
          +-- agent-main: 9860
                  |
                  +-- muye-llm: 9850 -> OpenAI-compatible 上游
                  +-- agent-travel: 8011 (internal + public)
                  +-- agent-order: 8012 (internal only)
```

| 服务 | 端口 | 职责 |
| --- | ---: | --- |
| `muye-llm` | 9850 | 模型注册、thinking 校验、Chat/SSE/Embedding 网关 |
| `agent-main` | 9860 | 对话、SSE、工具调用与子 Agent 编排 |
| `agent-travel` | 8011 | ReAct 风格旅行参考 Agent |
| `agent-order` | 8012 | Graph 风格订单参考 Agent，不执行真实交易 |
| `muye-gateway` | 80/443 | Bearer Token、TLS 与公网路由 allowlist |

## 依赖

Python 3.11 或更高版本。核心 SDK 使用 [muye-multi-agent-sdk](https://github.com/muye-x/muye-multi-agent-sdk) v1.0.0。

## 安装

安装 GitHub 上固定版本的 SDK 与全部服务依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## 配置

仓储提供根目录和各服务目录两类 `.env.example`。它们只是可提交的配置模板，程序不会直接
读取 `.env.example`；使用时需要复制为同目录下的 `.env`，并填写实际运行值。

| 配置模板 | 配置范围 | 适用方式 |
| --- | --- | --- |
| `.env.example` | 全部服务的一键启动聚合配置 | 本地开发、整体联调 |
| `muye-llm/.env.example` | LLM、Embedding 与可选 LangSmith 配置 | 单独部署 `muye-llm` |
| `agents/agent-main/.env.example` | 主 Agent、存储、检索与子 Agent 地址 | 单独部署 `agent-main` |
| `agents/agent-travel/.env.example` | Travel Agent 与 SDK 配置 | 单独部署 `agent-travel` |
| `agents/agent-order/.env.example` | Order Agent 与 SDK 配置 | 单独部署 `agent-order` |
| `muye-gateway/.env.example` | Nginx、TLS、Gateway 与控制台配置 | 单独部署 Gateway |

本地一键启动只需创建根目录 `.env`：

```bash
cp .env.example .env
```

`MUYE_LLM_API_KEY` 与 `MUYE_LLM_EMBED_API_KEY` 是当前 LLM 服务启动所需配置；默认模型必须
存在于 `MUYE_LLM_MODELS_JSON`，主 Agent 使用的 `MUYE_LLM_MODEL` 也必须存在于同一注册表。
根启动器会在启动任何子进程前给出缺失项或格式错误。

通过根启动器运行时，配置优先级为：

```text
Shell 环境变量 > 根目录 .env > 服务目录 .env > 源码默认值
```

通过服务入口独立运行时，不读取根目录 `.env`，配置优先级为：

```text
Shell 环境变量 > 当前服务目录 .env > 源码默认值
```

## 启动

### 方式一：一键启动全部服务

该方式由根目录 `main.py` 统一加载根 `.env`，按依赖顺序启动全部服务并等待健康检查，适合
本地开发和整体联调：

```bash
.venv/bin/python main.py --dry-run
.venv/bin/python main.py --timeout 20
```

`--dry-run` 只检查服务入口和配置，不启动进程；即使没有真实密钥也可用于 CI 结构检查。
启动器按 `muye-llm -> agent-main -> agent-travel -> agent-order -> dashboard-api` 顺序等待
健康检查。本地控制台位于：

```text
http://127.0.0.1:9870/console/online.html
```

### 方式二：独立启动或部署单个服务

该方式在目标服务目录复制其配置模板，只安装或注入该服务需要的配置，适合容器部署、生产
环境和单服务调试。例如单独启动 LLM 服务：

```bash
cd muye-llm
cp .env.example .env
../.venv/bin/python main.py
```

Agent 服务采用相同方式，例如单独启动 Travel：

```bash
cd agents/agent-travel
cp .env.example .env
../../.venv/bin/python main.py
```

其他服务的完整命令与部署边界见各自目录的 README。Gateway 的 `.env` 主要由
`scripts/render-nginx-config.sh` 读取以生成 Nginx 配置；本地 Dashboard API 可直接使用默认值
或由进程环境注入 `MUYE_DASHBOARD_*`。

## 协议

子 Agent internal API 为 `/health`、`/capabilities`、`/invoke`、`/invoke/stream` 和
`/cancel`。流式生命周期为：

```text
session_start -> block/tool/thinking -> done -> session_end
```

同一 `block.id` 的 `delta` 按到达顺序追加；不同 block ID 必须独立处理。Travel 注册 public
profile，Order 仅注册 internal profile。

## 测试

仓储只保留长期质量资产：模块单元测试、服务/协议集成测试和 Gateway 系统 smoke test。测试均
隔离真实模型与外部网络，不包含临时调试脚本或生成结果。

```bash
PYTHONPATH=muye-llm:muye-gateway \
  .venv/bin/python -m pytest -q muye-llm/tests muye-gateway/dashboard_api/tests
PYTHONPATH=agents/agent-main \
  .venv/bin/python -m pytest -q agents/agent-main/tests
PYTHONPATH=agents/agent-travel:agents/agent-order \
  .venv/bin/python -m pytest -q agents/agent-travel/tests agents/agent-order/tests
.venv/bin/python -m pytest -q tests
.venv/bin/python main.py --dry-run
```

生产 Gateway 的连通性与鉴权检查使用：

```bash
muye-gateway/scripts/smoke-test.sh
```

## 安全边界

- `agent-order` 仅用于 Graph 和协议演示，不执行真实下单。
- 生产 Nginx 只公开 `/agentMain/` 与 `/api/v1/travel/`。

项目许可证：[MIT](LICENSE)。内置前端资源及迁移代码的许可证见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
