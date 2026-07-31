"""MainAgent 的 Control ETag Catalog provider 与请求级授权投影。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from contracts.catalog import build_catalog_snapshot, validate_catalog_snapshot_checksum
from contracts.models import AgentCatalogSnapshotV1

from .registry import SubAgentRegistry


logger = logging.getLogger(__name__)


class CatalogUnavailableError(RuntimeError):
    """Control Catalog 或授权解析不可用；调用方必须保持旧 Catalog 或空授权。"""


@dataclass(frozen=True, slots=True)
class AuthorizationResolution:
    user_id: str
    catalog_revision: str
    catalog_checksum: str
    allowed_agent_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class AuthorizedCatalogView:
    """单个请求捕获的 Catalog/grant 交集，不能在模型执行中扩大。"""

    user_id: str
    catalog_revision: str
    catalog_checksum: str
    allowed_agent_ids: frozenset[str]
    registry: SubAgentRegistry


@dataclass(frozen=True, slots=True)
class CitationEvidence:
    """从已认证 SubAgent 终态中提取的最小可信引用证据。"""

    citation_id: str
    knowledge_version_id: str
    locator: dict[str, object]
    title: str
    source: str


class HttpControlPlaneClient:
    """使用 Main service token 调用固定 Control 内部 API。"""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: SecretStr,
        timeout_seconds: float = 5.0,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Control base URL 必须是不含凭据和路径的 HTTP(S) 根地址")
        if not service_token.get_secret_value().strip():
            raise ValueError("Control Main service token 不能为空")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("Control timeout 必须为 0 至 30 秒")
        self._base_url = base_url.rstrip("/")
        self._token = service_token
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    async def fetch_catalog(self, etag: str | None) -> AgentCatalogSnapshotV1 | None:
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = f'"{etag}"'
        try:
            async with self._client_factory(timeout=self._timeout_seconds) as client:
                response = await client.get(f"{self._base_url}/internal/v1/catalog/active", headers=headers)
                if response.status_code == 304:
                    return None
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalogUnavailableError("无法读取 active Agent Catalog") from exc
        try:
            snapshot = AgentCatalogSnapshotV1.model_validate(response.json())
        except Exception as exc:
            raise CatalogUnavailableError("Control 返回无效 Agent Catalog") from exc
        response_etag = response.headers.get("etag", "").strip('"')
        if response_etag != snapshot.catalog_checksum:
            raise CatalogUnavailableError("Control Catalog ETag 与 payload checksum 不一致")
        return snapshot

    async def resolve_authorization(self, user_id: str) -> AuthorizationResolution:
        try:
            async with self._client_factory(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/internal/v1/agent-authorizations/resolve",
                    headers=self._headers(),
                    json={"user_id": user_id},
                )
                response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CatalogUnavailableError("无法解析用户 Agent 授权") from exc
        if not isinstance(value, dict):
            raise CatalogUnavailableError("Control 授权响应无效")
        allowed = value.get("allowed_agent_ids")
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            raise CatalogUnavailableError("Control 授权集合无效")
        fields = ("user_id", "catalog_revision", "catalog_checksum")
        if any(not isinstance(value.get(field), str) for field in fields):
            raise CatalogUnavailableError("Control 授权身份无效")
        if value["user_id"] != user_id:
            raise CatalogUnavailableError("Control 授权用户身份不匹配")
        return AuthorizationResolution(
            user_id=user_id,
            catalog_revision=value["catalog_revision"],
            catalog_checksum=value["catalog_checksum"],
            allowed_agent_ids=frozenset(allowed),
        )

    async def ack(self, snapshot: AgentCatalogSnapshotV1, *, accepted: bool, reason: str | None = None) -> None:
        try:
            async with self._client_factory(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/internal/v1/catalog/{snapshot.catalog_revision}/acks",
                    headers=self._headers(),
                    json={
                        "accepted": accepted,
                        "catalog_checksum": snapshot.catalog_checksum,
                        "reason": reason,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalogUnavailableError("无法提交 Main Catalog ACK") from exc

    async def record_citation(
        self,
        descriptor: Any,
        user_id: str,
        evidence: CitationEvidence,
    ) -> None:
        """回写由请求捕获 Catalog 和目标 SubAgent identity 共同证明的 citation。"""
        if not descriptor.catalog_revision or not descriptor.catalog_checksum:
            raise CatalogUnavailableError("子 Agent 缺少 citation 所需 Catalog identity")
        body = {
            "citation_id": evidence.citation_id,
            "user_id": user_id,
            "agent_id": descriptor.agent_id,
            "agent_version": descriptor.agent_version,
            "knowledge_version_id": evidence.knowledge_version_id,
            "catalog_revision": descriptor.catalog_revision,
            "catalog_checksum": descriptor.catalog_checksum,
            "locator": evidence.locator,
        }
        try:
            async with self._client_factory(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/internal/v1/citations",
                    headers=self._headers(),
                    json=body,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalogUnavailableError("无法记录可信 citation") from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get_secret_value().strip()}"}


class CatalogProvider:
    """以 ETag 拉取、完整校验并原子替换 Main 的 active Registry。"""

    def __init__(self, client: HttpControlPlaneClient | None, *, poll_seconds: float = 5.0) -> None:
        if poll_seconds <= 0 or poll_seconds > 300:
            raise ValueError("Catalog poll interval 必须为 0 至 300 秒")
        self._client = client
        self._poll_seconds = poll_seconds
        self._snapshot = build_catalog_snapshot([])
        self._registry = SubAgentRegistry([])
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None

    @property
    def snapshot(self) -> AgentCatalogSnapshotV1:
        return self._snapshot

    @property
    def registry(self) -> SubAgentRegistry:
        return self._registry

    async def start(self) -> None:
        """初次加载失败时保留可健康启动的空 Catalog，并继续后台重试。"""
        if self._client is None or self._poll_task is not None:
            return
        try:
            await self.refresh()
        except CatalogUnavailableError:
            logger.exception("初次加载 Agent Catalog 失败，Main 保持旧/空 Catalog")
        self._poll_task = asyncio.create_task(self._poll(), name="main-agent-catalog-poll")

    async def close(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def refresh(self) -> bool:
        """候选无效时 ACK reject 并保留旧 Registry；成功时一次赋值切换。"""
        if self._client is None:
            return False
        async with self._lock:
            candidate = await self._client.fetch_catalog(self._snapshot.catalog_checksum)
            if candidate is None:
                return False
            try:
                validate_catalog_snapshot_checksum(candidate)
                registry = SubAgentRegistry.from_snapshot(candidate)
            except ValueError as exc:
                try:
                    await self._client.ack(candidate, accepted=False, reason=str(exc)[:500])
                finally:
                    raise CatalogUnavailableError("Main 拒绝无效 Agent Catalog") from exc
            await self._client.ack(candidate, accepted=True)
            self._snapshot = candidate
            self._registry = registry
            return True

    async def authorized_view(self, user_id: str) -> AuthorizedCatalogView:
        """每次请求读取最新 grant，并要求授权投影与本地 Catalog 完全同 revision。"""
        if self._client is None:
            return AuthorizedCatalogView(
                user_id=user_id,
                catalog_revision=self._snapshot.catalog_revision,
                catalog_checksum=self._snapshot.catalog_checksum,
                allowed_agent_ids=frozenset(),
                registry=SubAgentRegistry([]),
            )
        await self.refresh()
        resolution = await self._client.resolve_authorization(user_id)
        if (
            resolution.catalog_revision != self._snapshot.catalog_revision
            or resolution.catalog_checksum != self._snapshot.catalog_checksum
        ):
            await self.refresh()
            resolution = await self._client.resolve_authorization(user_id)
        if (
            resolution.catalog_revision != self._snapshot.catalog_revision
            or resolution.catalog_checksum != self._snapshot.catalog_checksum
        ):
            raise CatalogUnavailableError("授权投影与 Main active Catalog 不一致")
        active_ids = {descriptor.agent_id for descriptor in self._registry.values()}
        allowed = frozenset(active_ids & resolution.allowed_agent_ids)
        return AuthorizedCatalogView(
            user_id=user_id,
            catalog_revision=self._snapshot.catalog_revision,
            catalog_checksum=self._snapshot.catalog_checksum,
            allowed_agent_ids=allowed,
            registry=self._registry.select(allowed),
        )

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                await self.refresh()
            except CatalogUnavailableError:
                logger.exception("Agent Catalog 轮询失败，继续使用上一份有效 Catalog")
