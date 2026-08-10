# Muye Model Gateway

[中文说明](README.md)

`muye-llm` is a model gateway for trusted internal services. It maintains separate
model-alias registries for Chat, Embedding, and Rerank; validates capabilities;
and applies upstream timeouts and bounded retries. Rerank currently supports
DashScope and is disabled by default.

The service listens on `http://127.0.0.1:9850` by default. It does not authenticate
callers itself, so production access must be restricted at the network boundary.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Gateway process liveness |
| `GET` | `/api/v2/models` | Available model aliases and thinking capability |
| `POST` | `/api/v2/chat` | Complete non-streaming chat response |
| `POST` | `/api/v2/chat/stream` | SSE chat increments and tool calls |
| `POST` | `/api/v2/embed` | Embeddings for a batch of text |
| `POST` | `/api/v2/rerank` | Candidate-document ranking |

Copy `.env.example` to `.env` and configure only stable aliases for callers,
rather than provider model names. The API never exposes upstream URLs, provider
model names, or credentials. LangSmith tracing is optional and records redacted
operational metadata only.

The [Chinese README](README.md) is the detailed API reference, including request
schemas, SSE events, error semantics, Rerank configuration, and tests.
