# v3 数据库迁移

阶段 1 开始，在 `sql/` 中按 `NNNN_description.sql` 命名新增只进不退的 PostgreSQL 迁移。例如：

```text
0001_create_agent_tables.sql
0002_add_revision_indexes.sql
```

使用以下命令先检查迁移计划：

```bash
PYTHONPATH=. .venv/bin/python -m muye_core.migrations.runner plan
```

只有受控部署环境才可应用迁移。数据库 URL 必须经环境变量注入，命令不会输出该值：

```bash
PYTHONPATH=. .venv/bin/python -m muye_core.migrations.runner apply \
  --database-url-env MUYE_CORE_DATABASE_URL
```

迁移文件一经应用不得修改或删除。Runner 会保存文件 checksum；发现同一版本的内容漂移时会拒绝继续执行。阶段 0 不包含业务表迁移。
