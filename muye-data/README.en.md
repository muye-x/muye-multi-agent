# muye-data

[中文说明](README.md)

`muye-data` is a read-only Milvus retrieval service. It handles query parsing,
candidate retrieval, Hybrid fusion, and optional reranking. Database creation,
indexing, embedding production, writes, updates, and deletes belong to the
Knowledge Worker or an external data project.

## Boundaries

- The first release supports Milvus only.
- The public API never accepts database connections, physical collection/index
  names, or native query strings.
- The adapter protocol has no write methods; use a read-only database account in
  production as a defense-in-depth measure.
- Physical field names are configured by users; `id`, `content`, `vector`, and
  `keyword` are logical roles.

## Configure and run

```bash
cd muye-data
cp .env.example .env
cp config.example.yaml config.yaml
../.venv/bin/python -m pip install -r requirements.txt
../.venv/bin/python main.py
../.venv/bin/python -m pytest -q
```

`config.yaml` uses `version: 1` and defines connections, logical resources,
exposed logical fields, and filterable logical fields. Never add vector fields to
`exposed_fields`. The service does not create, load, or repair collections or
indexes automatically; dependency failures appear through `/ready` and request
errors.

## Public API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/retrieve` | Retrieval, fusion, and optional reranking |
| `GET` | `/api/v1/resources/{resource}/capabilities` | Static public capability information |
| `GET` | `/health` | Process liveness without dependency probes |
| `GET` | `/ready` | Redacted resource, database, and model readiness |

Production SubAgent identity uses the active Control Catalog and a distinct Data
token for every `agent_id`. Enable it with `MUYE_DATA_AGENT_AUTH_ENABLED=true`;
the default disabled state is only for standalone development and offline tests.

See the [Chinese README](README.md) for the full API schema, authorization model,
Resource Snapshot lifecycle, retry semantics, and production configuration.
