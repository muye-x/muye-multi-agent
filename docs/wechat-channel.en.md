# WeChat Channel Integration

`muye-channels` is the boundary between WeChat iLink and Muye Agents. It owns QR-code binding, iLink credentials, message cursors, and reply correlation; MainAgent receives only SDK-normalized text requests.

## Enable the channel

Copy `muye-channels/.env.example` to `.env` for local development and set a PostgreSQL connection plus three distinct values: `MUYE_CHANNELS_CALLER_TOKEN`, `MUYE_CHANNELS_MAIN_TOKEN`, and `MUYE_CHANNELS_ENCRYPTION_KEY`. The encryption key must be a base64-encoded 32-byte AES-GCM key. Set the same main token in `agents/agent-main/.env`, and set the caller token in Gateway's environment.

Root Compose starts `channels` by default. Bindings, cursors, messages, and delivery state are stored in PostgreSQL `channel_*` tables. Local development reads `muye-channels/.env`; production Compose reads the deployment `.env`. In either case, all three channel values are required.

The console's **WeChat** navigation item lets a signed-in user manage one active binding. Confirming a replacement QR code replaces the previous binding. Gateway maps browser requests from `/api/v2/channels/` to the Channel service's `/api/v1/` routes only after Control-session authentication, then adds a caller token and trusted user ID. Do not expose the Channel service directly.

## Production verification

Configure the three values in the appropriate local or production environment. The caller token must match Gateway's value and the main token must match Agent Main's value. From the repository root, start the services:

```bash
docker compose up -d --build channels gateway
docker compose ps channels gateway
```

Sign in to the console, open **WeChat**, select **Get QR code**, and scan the displayed code. After the mobile confirmation, the page should show that it is bound. Enter the phone-displayed verification code when requested. Inspect binding and message-polling logs with `docker compose logs -f channels`.

## Security and behavior

- iLink credentials, QR polling tokens, and `context_token` are encrypted at rest and never sent to an Agent.
- The WeChat sender only derives an isolated session ID. Agent authorization uses the Control grants of the user who completed the scan.
- The initial implementation handles text messages only. Images, voice messages, files, and video are ignored.
- Incoming messages are persisted for deduplication before at-most-once MainAgent invocation. Agent or delivery failures are logged server-side and do not produce a WeChat reply.
- Only HTTPS iLink hosts listed in `WECHAT_ILINK_ALLOWED_HOSTS` are accepted, including confirmation redirects.
- The service assumes a single instance. Replace the state store with a lease-aware PostgreSQL implementation before deploying multiple replicas or regions.
