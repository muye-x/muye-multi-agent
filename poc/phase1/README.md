# 阶段 1 垂直 PoC

该 PoC 验证阶段 1 的前半段：受控 Markdown 解析、稳定 checksum、受限 Profile、`AgentGenerationSpecV1`
组装、一次性 ReAct Agent 目录渲染，以及开发者修改后拒绝覆盖。

```bash
cd scaffold
.venv/bin/python -m poc.phase1.run \
  --source-root poc/phase1/samples \
  --document poc/phase1/samples/product-handbook.md \
  --output /tmp/muye-phase1-agents \
  --agent-id agent_product_handbook \
  --slug product-handbook \
  --resource-id kb.product_handbook \
  --scope-value kb.product_handbook
```

生成目录是 PoC 产物，默认 `deployment.enabled: false`，不能直接作为生产部署模板。阶段 2 会替换为
SDK v2 标准模板，阶段 3 才提供正式 Generator。

`milvus/compose.yaml` 仅用于手工验证真实 Milvus Hybrid 环境：

```bash
./poc/phase1/milvus/start-local.sh
```

首次运行会生成随机 MinIO 凭据并保存至被 Git 忽略的 `poc/phase1/milvus/.env`；MinIO 只在数据卷首次创建时使用这组凭据。不要删除或改写该文件后直接重启已有卷。此 PoC 的数据可丢弃，若需要重新初始化，先执行下面命令删除该 Compose project 的三个命名卷，再删除 `.env` 并重新运行启动脚本：

```bash
docker compose \
  --env-file poc/phase1/milvus/.env \
  -f poc/phase1/milvus/compose.yaml \
  -p muye-phase1-milvus \
  down -v
rm -f poc/phase1/milvus/.env
```

`down -v` 会删除全部阶段 1 临时数据卷。

其 `muye-data.config.yaml` 使用逻辑资源 `product_handbook`、Dense + BM25/Sparse Hybrid 配置和固定
`knowledge_id` 过滤字段。该环境需要由外部 seed 工具创建 Collection、BM25 Function 与索引；PoC 不在
应用进程中写入 Milvus，也不会在没有 Docker 的环境中模拟成功。

Milvus 完全启动后，显式重建临时 Collection 并执行三路检索验证：

```bash
.venv/bin/python poc/phase1/milvus/verify_hybrid.py --reset
```

该命令只操作 `phase1_product_handbook_v1`：创建 `content -> sparse_embedding` 的 BM25 Function、Dense
和 `SPARSE_INVERTED_INDEX + BM25` 索引，写入确定性样本，再确认 Dense、BM25/Sparse 与 RRF Hybrid
在 `knowledge_id == "kb.product_handbook"` 过滤下都首先命中退款政策。没有 `--reset` 时，若 Collection
已经存在会失败，避免覆盖此前的手工检查数据。

PoC 的向量维度固定为 4，`muye-data.config.yaml` 中的 `text-embedding-default` 也必须配置为返回 4 维。
该文件只验证 Milvus schema 与资源配置的一致性，尚未替代完整 `muye-data` + Embedding 服务端到端测试。
