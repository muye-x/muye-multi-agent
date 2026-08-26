# Muye Multi-Agent Scaffold Production Deployment Guide

[中文说明](README.md)

## Topology and prerequisites

`deploy/compose.production.yaml` starts PostgreSQL, Control, Dashboard API, `muye-llm`, `muye-data`, MainAgent, the WeChat Channel service, the hotel-employee SubAgent, and Gateway. Milvus is managed separately by `poc/phase1/milvus/compose.yaml`; its Compose project must be named `milvus` so `muye-data` can resolve `milvus-milvus-1` on the external `milvus_default` network.

The deployment needs Docker 24+ with Docker Compose V2, at least 4 GB memory, and an HTTP port such as `8080`. Build or publish these eight application images: Gateway, Control, Dashboard API, MainAgent, `muye-llm`, `muye-data`, `muye-channels`, and the required SubAgent images.

## Deploy

```bash
git clone <repo-url> muye-scaffold
cd muye-scaffold
cp deploy/.env.production .env
# Edit .env with real database credentials and distinct service tokens.

docker compose -f poc/phase1/milvus/compose.yaml up -d
docker compose -f deploy/compose.production.yaml up -d --build
docker exec -i muye-postgres-1 psql -U muye -d muye < deploy/init-db.sql
curl http://localhost:8080/gateway/health
```

Bootstrap the administrator through the console or `POST /api/v2/auth/bootstrap`, then build the required knowledge resources and synchronize the Catalog before granting users access. See the root [README](../README.en.md) for the Agent build and deployment lifecycle.

## WeChat Channel

Production Compose includes `channels`. Add these distinct values to deployment `.env`:

```dotenv
MUYE_CHANNELS_CALLER_TOKEN=<independent gateway-to-channels token>
MUYE_CHANNELS_MAIN_TOKEN=<independent channels-to-main token>
MUYE_CHANNELS_ENCRYPTION_KEY=<base64-encoded 32-byte AES-GCM key>
WECHAT_ILINK_BASE_URL=https://ilinkai.weixin.qq.com/ilink/bot
WECHAT_ILINK_ALLOWED_HOSTS=ilinkai.weixin.qq.com
```

The caller token is shared only by Gateway and channels; the main token is shared only by channels and MainAgent. Neither token nor the encryption key may be reused as a Control, Gateway, or SubAgent credential. Complete QR-code confirmation on the console's **WeChat** page, then verify text message delivery. See [WeChat Channel integration](../docs/wechat-channel.en.md).

## Data and operations

PostgreSQL volumes hold users, grants, conversations, and `channel_*` records. `control-state` holds Catalog and citation state; the separate Milvus volume holds vector data. Back up PostgreSQL according to your operational policy and test recovery in isolation.

Useful operations:

```bash
docker compose -f deploy/compose.production.yaml ps
docker compose -f deploy/compose.production.yaml logs -f agent-main
docker compose -f deploy/compose.production.yaml restart agent-main
docker compose -f deploy/compose.production.yaml pull
docker compose -f deploy/compose.production.yaml up -d
docker compose -f deploy/compose.production.yaml down
```

Do not expose Control, Data, LLM, Channels, or any SubAgent port directly. Terminate TLS at Gateway in production, use unique generated secrets, and keep `.env` outside version control.
