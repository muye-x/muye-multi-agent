# muye-data

`muye-data` 是一个只读、多数据库兼容的检索服务。它负责查询解析、候选召回、Hybrid
融合、可选 Rerank 和数据库适配；数据库建库建表、索引、Embedding 生产、数据写入、
更新和删除均由独立数据项目负责。

## 职责边界

```text
外部数据项目                     muye-data                    muye-llm
建库/建表/索引/写入  --->  只读查询/召回/RRF/重排编排  --->  Embedding/Rerank 推理
                                  ^
                                  |
                         所有 Agent 经 SDK 按需调用
```

- 默认数据库为 Milvus，首版同时提供 OpenSearch 适配器。
- 公共 API 不接受数据库连接、物理 collection/index 或原生查询字符串。
- 适配器协议没有任何写方法。生产部署仍必须使用数据库只读账号作为权限兜底。
- `id`、`content`、`vector` 和 `keyword` 都是逻辑角色，物理字段名由使用者配置；
  `vector` 仅在 Dense/Hybrid 时需要，`keyword` 仅在 Keyword/Hybrid 时需要。
- Milvus Collection、BM25/sparse 字段和索引，或 OpenSearch index、k-NN/text mapping，
  必须在服务启动前由外部数据项目准备。

## 配置

使用仓库已有 `.venv`，并从模板创建本地配置：

```bash
cd muye-data
cp .env.example .env
cp config.example.yaml config.yaml
../.venv/bin/python -m pip install -r requirements.txt
```

YAML 顶层版本固定为 `version: 1`，包含：

- `connections`：Milvus/OpenSearch 连接。Token、用户名和密码只能写环境变量名。
- `resources`：逻辑资源 alias、既有物理目标、最小字段映射和 pipeline。
- `exposed_fields`：调用方可通过 `return_fields` 选择的逻辑字段 allowlist。
- `filterable_fields`：过滤 AST 可引用的逻辑字段 allowlist。

向量字段永远不能加入 `exposed_fields`。服务不会自动创建、加载或修复 collection/index；
远端资源异常通过 `/ready` 和请求错误反映。
未被任何资源引用的 connection 不会初始化或要求凭据，便于同一模板保留多个数据库示例；
一旦资源引用该 connection，其凭据环境变量会在启动阶段严格校验。

## API

服务仅公开四个入口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/retrieve` | 完整召回、融合和可选重排 |
| `GET` | `/api/v1/resources/{resource}/capabilities` | 静态公开能力，不探测数据库 |
| `GET` | `/health` | 进程存活，不探测依赖 |
| `GET` | `/ready` | 脱敏的资源、数据库和实际模型 alias/维度状态 |

请求示例：

```json
{
  "resource": "product_knowledge",
  "query": "产品如何申请退款？",
  "top_k": 5,
  "pipeline": "hybrid",
  "filter": {
    "op": "and",
    "conditions": [
      {"op": "eq", "field": "enabled", "value": true},
      {"op": "in", "field": "category", "values": ["policy", "faq"]}
    ]
  },
  "return_fields": ["title", "source"],
  "trace_id": "request-01"
}
```

过滤器只支持 `eq/ne/gt/gte/lt/lte/in/not_in/and/or/not`。响应的 `score` 仅保证在
当前响应内越高越相关，不承诺跨数据库或模型可比。Hybrid 的可选通道或可选 Rerank
失败时返回可用结果，同时设置 `partial=true` 和稳定 `warnings`；required 阶段失败则
返回结构化错误。`MUYE_DATA_RERANK_MAX_DOCUMENTS` 应与 muye-llm 的候选上限保持一致，
且不得低于公共 `top_k` 上限 100。

`/ready` 会读取 muye-llm 的模型列表，而不只检查进程 `/health`。Embedding alias 不存在、
已声明的固定维度不一致或 required Rerank alias 不存在时，对应 pipeline 会标为不可用；旧版
Embedding 注册未声明维度时标为 `degraded`。仍有 Keyword 或可选阶段回退时，资源同样标为
`degraded`。OpenAPI 同步声明 400/404/422/502/503/504 的统一 `ErrorResponse`，便于 SDK 和
其他调用方生成严格客户端。

## 启动与验证

```bash
cd muye-data
../.venv/bin/python main.py
../.venv/bin/python -m pytest -q
../.venv/bin/python -m compileall -q .
```

测试全部使用 fake/mock，不需要数据库、模型服务或外部网络。`pymilvus` 和
`opensearch-py` 在首次使用对应 connection 时才导入，但生产启用相应资源前必须安装。
