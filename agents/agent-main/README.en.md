# Muye Main Agent

[中文说明](README.md)

`agent-main` is the primary orchestration service for Muye Multi-Agent. It
receives user conversations, coordinates `muye-llm`, web tools, and SubAgents,
and returns model output plus execution details through HTTP SSE. It listens on
`127.0.0.1:9860` by default; external access must go through `muye-gateway` with
its allowlist and Bearer Token controls.

## Run

After installing dependencies and configuring the environment at the repository
root:

```bash
cd agents/agent-main
../../.venv/bin/python main.py
```

Copy `.env.example` to `.env` before an independent deployment. The template
lists model gateway, database, retrieval, and SubAgent settings. It defaults to
SQLite with memory disabled. Files containing API keys, tokens, or database
connection strings are local-only and must not be committed.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `POST` | `/api/v1/chat/` | Non-streaming chat |
| `POST` | `/api/v1/chat/stream` | Block Stream V2 SSE chat |
| `GET` | `/api/v1/chat/history/{session_id}` | Read server-side session history |
| `DELETE` | `/api/v1/chat/history/{session_id}` | Clear server-side session history |

`ChatRequest` requires `user_input`. In production, pass stable, non-default
`user_id` and `session_id` values to isolate sessions. The service also supports
`files`, `user_location`, `enable_knowledge`, and `user_informations`.

The streaming endpoint returns `text/event-stream` with the lifecycle:

```text
session_start -> block / tool / thinking -> done -> session_end
```

Append `delta` values for the same block ID in order and preserve raw newlines and
Markdown. See the [Chinese README](README.md) for the complete event envelope and
field contract.

## Local UI and verification

Run `./scripts/muye.sh agent dev <slug>` from the repository root, then open
`http://127.0.0.1:5173/chat`. The Vue UI uses a local Vite proxy to inject the
development identity, so the browser never receives internal tokens.

```bash
PYTHONPATH=agents/agent-main \
  .venv/bin/python -m pytest -q agents/agent-main/tests/test_muye_service_integration.py
```

The test uses fakes and does not need model credentials. End-to-end operation
requires a running `muye-llm` and relevant deployment configuration.
