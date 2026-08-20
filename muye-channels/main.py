"""Muye 第三方通道服务的微信文本实现。

该服务是唯一保存 iLink 凭据与上下文令牌的边界。它将入站文本规范化为 SDK
Channel 协议后调用 MainAgent，Agent 进程永远不会收到微信凭据或 context_token。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import uuid
from io import BytesIO
from contextlib import asynccontextmanager
from dataclasses import dataclass
from secrets import compare_digest
from typing import Any
from urllib.parse import urlsplit

import httpx
import qrcode as qrcode_lib
import qrcode.image.svg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from muye_multi_agent_sdk import ChannelAgentClient, ChannelInvokeRequest


logger = logging.getLogger(__name__)


class ChannelConfig(BaseModel):
    """通道服务的启动配置；密钥只由进程环境提供。"""

    model_config = ConfigDict(extra="forbid")
    caller_token: str = Field(min_length=16)
    main_url: str = Field(min_length=8)
    main_token: str = Field(min_length=16)
    encryption_key: str = Field(min_length=40)
    database_url: str = Field(min_length=16)
    agent_timeout_seconds: float = Field(default=240, gt=0, le=300)
    ilink_base_url: str
    allowed_hosts: set[str]

    @classmethod
    def from_env(cls) -> "ChannelConfig":
        base_url = os.getenv("WECHAT_ILINK_BASE_URL", "https://ilinkai.weixin.qq.com/ilink/bot").rstrip("/")
        allowed = {item.strip().lower() for item in os.getenv("WECHAT_ILINK_ALLOWED_HOSTS", "ilinkai.weixin.qq.com").split(",") if item.strip()}
        return cls(
            caller_token=os.getenv("MUYE_CHANNELS_CALLER_TOKEN", "").strip(),
            main_url=os.getenv("MUYE_CHANNELS_MAIN_URL", "http://127.0.0.1:9860").rstrip("/"),
            main_token=os.getenv("MUYE_CHANNELS_MAIN_TOKEN", "").strip(),
            encryption_key=os.getenv("MUYE_CHANNELS_ENCRYPTION_KEY", "").strip(),
            database_url=os.getenv("MUYE_CHANNELS_DATABASE_URL", "").strip(),
            agent_timeout_seconds=float(os.getenv("MUYE_CHANNELS_AGENT_TIMEOUT_SECONDS", "240")),
            ilink_base_url=base_url,
            allowed_hosts=allowed,
        )


class QrCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verify_code: str = Field(min_length=1, max_length=32)


class CryptoBox:
    """对 PostgreSQL 中的 provider 私密字段使用 AES-GCM 加密。"""

    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except Exception as exc:
            raise ValueError("MUYE_CHANNELS_ENCRYPTION_KEY 必须是 base64 编码的 32 字节密钥") from exc
        if len(key) != 32:
            raise ValueError("MUYE_CHANNELS_ENCRYPTION_KEY 必须解码为 32 字节")
        self._cipher = AESGCM(key)
        self._identity_key = hashlib.sha256(key + b"muye-channel-identity").digest()

    def encrypt(self, value: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher.encrypt(nonce, value.encode("utf-8"), None)

    def decrypt(self, value: bytes) -> str:
        return self._cipher.decrypt(value[:12], value[12:], None).decode("utf-8")

    def stable_id(self, *values: str) -> str:
        digest = hashlib.sha256(self._identity_key + "\x1f".join(values).encode("utf-8")).hexdigest()
        return digest[:48]


@dataclass(frozen=True, slots=True)
class Binding:
    binding_id: str
    user_id: str
    bot_token: str
    base_url: str


class ChannelStore:
    """PostgreSQL 渠道仓储；一个用户仅可拥有一个活动微信绑定。"""

    def __init__(self, database_url: str, crypto: CryptoBox) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("MUYE_CHANNELS_DATABASE_URL 必须是 PostgreSQL URL")
        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError as exc:  # pragma: no cover - dependency gate
            raise RuntimeError("channels PostgreSQL 存储需要 psycopg_pool") from exc
        self._pool = AsyncConnectionPool(database_url, open=False, kwargs={"autocommit": False})
        self._crypto = crypto
    async def initialize(self) -> None:
        await self._pool.open()
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS channel_bindings (
                      user_id TEXT PRIMARY KEY, binding_id TEXT UNIQUE NOT NULL,
                      bot_token BYTEA NOT NULL, base_url TEXT NOT NULL,
                      cursor TEXT NOT NULL DEFAULT '', active BOOLEAN NOT NULL DEFAULT TRUE,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    CREATE TABLE IF NOT EXISTS channel_qr_sessions (
                      session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                      qrcode BYTEA NOT NULL, qr_content TEXT NOT NULL, verify_code BYTEA,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    CREATE TABLE IF NOT EXISTS channel_messages (
                      message_key TEXT PRIMARY KEY, binding_id TEXT NOT NULL,
                      provider_message_id TEXT NOT NULL, sender_key TEXT NOT NULL,
                      context_token BYTEA NOT NULL, target_user BYTEA NOT NULL, content BYTEA NOT NULL,
                      state TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      UNIQUE(binding_id, provider_message_id)
                    );
                    CREATE TABLE IF NOT EXISTS channel_deliveries (
                      idempotency_key TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL,
                      status TEXT NOT NULL, provider_message_id TEXT, error_code TEXT,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    CREATE TABLE IF NOT EXISTS channel_leases (
                      binding_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, lease_until TIMESTAMPTZ NOT NULL
                    );
                """)
            await connection.commit()

    async def close(self) -> None:
        await self._pool.close()

    async def create_qr(self, user_id: str, qrcode: str, content: str) -> str:
        session_id = f"wqr_{uuid.uuid4().hex}"
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("DELETE FROM channel_qr_sessions WHERE user_id = %s", (user_id,))
                await cursor.execute("INSERT INTO channel_qr_sessions (session_id,user_id,qrcode,qr_content) VALUES (%s,%s,%s,%s)", (session_id, user_id, self._crypto.encrypt(qrcode), content))
            await connection.commit()
        return session_id

    async def qr_session(self, user_id: str, session_id: str) -> tuple[str, str] | None:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT qrcode, verify_code FROM channel_qr_sessions WHERE session_id = %s AND user_id = %s", (session_id, user_id))
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._crypto.decrypt(row[0]), self._crypto.decrypt(row[1]) if row[1] else ""

    async def set_verify_code(self, user_id: str, session_id: str, code: str) -> bool:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("UPDATE channel_qr_sessions SET verify_code = %s WHERE session_id = %s AND user_id = %s", (self._crypto.encrypt(code), session_id, user_id))
                changed = cursor.rowcount == 1
            await connection.commit()
        return changed

    async def activate(self, user_id: str, bot_token: str, base_url: str) -> Binding:
        binding = Binding(f"wechat_{uuid.uuid4().hex}", user_id, bot_token, base_url)
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("DELETE FROM channel_bindings WHERE user_id = %s", (user_id,))
                await cursor.execute("INSERT INTO channel_bindings (user_id,binding_id,bot_token,base_url) VALUES (%s,%s,%s,%s)", (user_id, binding.binding_id, self._crypto.encrypt(bot_token), base_url))
                await cursor.execute("DELETE FROM channel_qr_sessions WHERE user_id = %s", (user_id,))
            await connection.commit()
        return binding

    async def binding(self, user_id: str) -> Binding | None:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT binding_id,user_id,bot_token,base_url FROM channel_bindings WHERE user_id = %s AND active", (user_id,))
                row = await cursor.fetchone()
        return Binding(row[0], row[1], self._crypto.decrypt(row[2]), row[3]) if row else None

    async def remove(self, user_id: str) -> None:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("DELETE FROM channel_bindings WHERE user_id = %s", (user_id,))
                await cursor.execute("DELETE FROM channel_qr_sessions WHERE user_id = %s", (user_id,))
            await connection.commit()

    async def bindings(self) -> list[Binding]:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT binding_id,user_id,bot_token,base_url FROM channel_bindings WHERE active")
                rows = await cursor.fetchall()
        return [Binding(row[0], row[1], self._crypto.decrypt(row[2]), row[3]) for row in rows]

    async def cursor(self, binding: Binding) -> str:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT cursor FROM channel_bindings WHERE binding_id = %s", (binding.binding_id,))
                row = await cursor.fetchone()
        return str(row[0]) if row else ""

    async def record_messages(self, binding: Binding, next_cursor: str, messages: list[tuple[str, str, str, str]]) -> list[tuple[str, str, str, str]]:
        """持久化游标与去重 inbox；返回本次首次领取的消息。"""
        accepted: list[tuple[str, str, str, str]] = []
        async with self._pool.connection() as connection:
          async with connection.cursor() as db_cursor:
            for provider_message_id, sender, context_token, content in messages:
                message_key = self._crypto.stable_id(binding.binding_id, provider_message_id)
                await db_cursor.execute(
                    "INSERT INTO channel_messages (message_key,binding_id,provider_message_id,sender_key,context_token,target_user,content,state) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending') ON CONFLICT DO NOTHING",
                    (message_key, binding.binding_id, provider_message_id, self._crypto.stable_id(binding.binding_id, sender), self._crypto.encrypt(context_token), self._crypto.encrypt(sender), self._crypto.encrypt(content)),
                )
                if db_cursor.rowcount:
                    accepted.append((message_key, sender, context_token, content))
            await db_cursor.execute("UPDATE channel_bindings SET cursor = %s, updated_at = now() WHERE binding_id = %s", (next_cursor, binding.binding_id))
          await connection.commit()
        return accepted

    async def claim(self, message_key: str) -> bool:
        """至多一次领取 Agent 调用；重启后的 processing 消息保持静默而非重复执行。"""
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("UPDATE channel_messages SET state = 'processing' WHERE message_key = %s AND state = 'pending'", (message_key,))
                changed = cursor.rowcount == 1
            await connection.commit()
        return changed

    async def finish(self, message_key: str, state: str) -> None:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("UPDATE channel_messages SET state = %s WHERE message_key = %s", (state, message_key))
            await connection.commit()


class ILinkClient:
    """iLink 的受限 HTTP 适配器，仅实现首版文本收发与二维码绑定。"""

    def __init__(self, config: ChannelConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token", "iLink-App-Id": "bot", "iLink-App-ClientVersion": "132100", "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode()}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def create_qr(self) -> tuple[str, str]:
        payload = await self._request(self._config.ilink_base_url + "/get_bot_qrcode?bot_type=3", {"local_token_list": []})
        if not isinstance(payload.get("qrcode"), str) or not isinstance(payload.get("qrcode_img_content"), str):
            raise RuntimeError("iLink 二维码响应无效")
        return payload["qrcode"], payload["qrcode_img_content"]

    async def qr_status(self, qrcode: str, verify_code: str) -> dict[str, Any]:
        return await self._request(f"{self._config.ilink_base_url}/get_qrcode_status?qrcode={qrcode}&verify_code={verify_code}", {})

    async def updates(self, binding: Binding, cursor: str) -> tuple[list[tuple[str, str, str, str]], str]:
        payload = await self._request(
            binding.base_url + "/getupdates",
            {"get_updates_buf": cursor, "base_info": {"channel_version": "2.4.4"}},
            binding.bot_token,
        )
        next_cursor = payload.get("get_updates_buf", cursor)
        if not isinstance(next_cursor, str):
            raise RuntimeError("iLink 游标无效")
        messages: list[tuple[str, str, str, str]] = []
        for raw in payload.get("msgs", []):
            if not isinstance(raw, dict) or raw.get("message_type") != 1:
                continue
            sender, context = raw.get("from_user_id"), raw.get("context_token")
            if not isinstance(sender, str) or not isinstance(context, str) or not sender or not context:
                continue
            text = "\n".join(item.get("text_item", {}).get("text", "").strip() for item in raw.get("item_list", []) if isinstance(item, dict) and item.get("type") == 1 and isinstance(item.get("text_item"), dict) and isinstance(item["text_item"].get("text"), str)).strip()
            if not text:
                continue
            message_id = str(raw.get("message_id") or hashlib.sha256(context.encode("utf-8")).hexdigest())
            messages.append((message_id, sender, context, text))
        return messages, next_cursor

    async def send_text(self, binding: Binding, target_user: str, context_token: str, content: str, client_id: str) -> None:
        payload = await self._request(
            binding.base_url + "/sendmessage",
            {"msg": {"from_user_id": "", "to_user_id": target_user, "client_id": client_id, "message_type": 2, "message_state": 2, "context_token": context_token, "item_list": [{"type": 1, "text_item": {"text": content}}]}, "base_info": {"channel_version": "2.4.4"}},
            binding.bot_token,
        )
        if payload.get("errcode") not in (0, None):
            raise RuntimeError("iLink 发送失败")

    async def _request(self, url: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in self._config.allowed_hosts:
            raise RuntimeError("iLink 地址不受信任")
        response = await self._client.post(url, headers=self._headers(token), json=payload)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("iLink 响应无效")
        return body


def _caller_user(request: Request, config: ChannelConfig) -> str:
    authorization = request.headers.get("authorization", "")
    actual = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    user_id = request.headers.get("x-muye-user-id", "").strip()
    if not user_id or not compare_digest(actual, config.caller_token):
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "Channel caller 无效"})
    return user_id


def _render_qr_svg(content: str) -> str:
    """Render provider QR content as a self-contained SVG data URI."""
    image = qrcode_lib.make(content, image_factory=qrcode.image.svg.SvgPathImage)
    output = BytesIO()
    image.save(output)
    return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")


async def _poll_binding(store: ChannelStore, ilink: ILinkClient, agent: ChannelAgentClient, crypto: CryptoBox, binding: Binding) -> None:
    """读取一批文本，先落库去重，再以至多一次语义调用 Agent 和投递微信。"""
    messages, cursor = await ilink.updates(binding, await store.cursor(binding))
    for message_key, sender, context_token, content in await store.record_messages(binding, cursor, messages):
        if not await store.claim(message_key):
            continue
        request = ChannelInvokeRequest(
            protocol_version="muye-agent-channel/2.0",
            channel="wechat",
            user_id=binding.user_id,
            session_id=f"wechat_{crypto.stable_id(binding.binding_id, sender)}",
            trace_id=f"wechat-{uuid.uuid4().hex}",
            message_id=message_key,
            channel_account_id=binding.binding_id,
            conversation_id=f"wechat_{crypto.stable_id(binding.binding_id, sender)}",
            reply_handle=context_token,
            message={"type": "text", "content": content},
        )
        try:
            response = await agent.invoke(request)
            if response.message is not None and response.status in {"success", "clarification_needed"}:
                await ilink.send_text(binding, sender, context_token, response.message.content, f"muye-{message_key}")
                await store.finish(message_key, "delivered")
            else:
                await store.finish(message_key, "failed")
        except Exception:
            logger.exception("微信消息处理失败 [binding=%s message=%s]", binding.binding_id, message_key)
            await store.finish(message_key, "failed")


def create_app(config: ChannelConfig | None = None, *, client: httpx.AsyncClient | None = None) -> FastAPI:
    """创建同源绑定 API；Gateway 负责登录态并注入可信用户身份。"""
    resolved = config or ChannelConfig.from_env()
    crypto = CryptoBox(resolved.encryption_key)
    store = ChannelStore(resolved.database_url, crypto)
    upstream = client or httpx.AsyncClient(timeout=40, trust_env=False)
    ilink = ILinkClient(resolved, upstream)
    agent = ChannelAgentClient(
        resolved.main_url,
        resolved.main_token,
        timeout_seconds=resolved.agent_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await store.initialize()
        stopped = asyncio.Event()

        async def poll_loop() -> None:
            while not stopped.is_set():
                for binding in await store.bindings():
                    try:
                        await _poll_binding(store, ilink, agent, crypto, binding)
                    except Exception:
                        logger.exception("微信轮询失败 [binding=%s]", binding.binding_id)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=2)
                except TimeoutError:
                    pass

        task = asyncio.create_task(poll_loop(), name="wechat-updates")
        try:
            yield
        finally:
            stopped.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await agent.aclose()
            await store.close()
            if client is None:
                await upstream.aclose()

    app = FastAPI(title="Muye Channels", version="2.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "muye-channels"}

    @app.get("/api/v1/bindings/wechat")
    async def status(request: Request) -> dict[str, str]:
        binding = await store.binding(_caller_user(request, resolved))
        return {"status": "active" if binding else "unbound"}

    @app.post("/api/v1/bindings/wechat/qrcode")
    async def create_qrcode(_: QrCodeRequest, request: Request) -> dict[str, str]:
        user_id = _caller_user(request, resolved)
        token, content = await ilink.create_qr()
        session_id = await store.create_qr(user_id, token, content)
        return {"session_id": session_id, "qr_svg": _render_qr_svg(content), "status": "wait"}

    @app.get("/api/v1/bindings/wechat/qrcode/{session_id}")
    async def qrcode_status(session_id: str, request: Request) -> dict[str, str]:
        user_id = _caller_user(request, resolved)
        session = await store.qr_session(user_id, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "二维码会话不存在"})
        token, verify_code = session
        payload = await ilink.qr_status(token, verify_code)
        status_value = payload.get("status")
        if not isinstance(status_value, str):
            raise HTTPException(status_code=502, detail={"code": "UPSTREAM_ERROR", "message": "二维码状态无效"})
        if status_value == "confirmed":
            bot_token, base_url = payload.get("bot_token"), payload.get("baseurl")
            if not isinstance(bot_token, str) or not isinstance(base_url, str):
                raise HTTPException(status_code=502, detail={"code": "UPSTREAM_ERROR", "message": "微信凭据无效"})
            parsed = urlsplit(base_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in resolved.allowed_hosts:
                raise HTTPException(status_code=502, detail={"code": "UPSTREAM_ERROR", "message": "微信重定向地址不受信任"})
            await store.activate(user_id, bot_token, base_url.rstrip("/") + ("/ilink/bot" if not base_url.rstrip("/").endswith("/ilink/bot") else ""))
        return {"status": status_value}

    @app.post("/api/v1/bindings/wechat/qrcode/{session_id}/verify")
    async def verify(session_id: str, body: VerifyRequest, request: Request) -> dict[str, str]:
        if not await store.set_verify_code(_caller_user(request, resolved), session_id, body.verify_code):
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "二维码会话不存在"})
        return {"status": "accepted"}

    @app.delete("/api/v1/bindings/wechat", status_code=204)
    async def unbind(request: Request) -> None:
        await store.remove(_caller_user(request, resolved))

    return app


if __name__ == "__main__":
    import uvicorn

    settings = ChannelConfig.from_env()
    uvicorn.run(create_app(settings), host=os.getenv("MUYE_CHANNELS_HOST", "127.0.0.1"), port=int(os.getenv("MUYE_CHANNELS_PORT", "9890")))
