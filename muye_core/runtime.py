"""阶段 3 Core 到固定 Runtime 的受控调用编排。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Protocol

import httpx

from contracts.v3 import RuntimeCapabilitiesV1, RuntimeInvokeRequestV1, RuntimeInvokeResponseV1

from .service import DomainError, Principal, RevisionRecord


@dataclass(frozen=True, slots=True)
class RuntimeRoute:
    """Core 已验证的 Active Runtime 投影；地址不来自客户端或 Revision。"""

    agent_id: str
    revision: RevisionRecord
    base_url: str
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        if self.agent_id != self.revision.agent_id:
            raise ValueError("Runtime Route 的 Agent 与 Revision 不一致")
        if not self.base_url.startswith("http://") or self.max_concurrency < 1:
            raise ValueError("Runtime Route 配置非法")


class RuntimeClient(Protocol):
    """Core 到 Runtime 的最小私有网络客户端。"""

    async def capabilities(self, route: RuntimeRoute) -> RuntimeCapabilitiesV1: ...

    async def invoke(self, route: RuntimeRoute, request: RuntimeInvokeRequestV1) -> RuntimeInvokeResponseV1: ...


class HttpRuntimeClient:
    """只调用已投影 Route 的固定 Runtime HTTP 接口，不附带旧服务 Token。"""

    async def capabilities(self, route: RuntimeRoute) -> RuntimeCapabilitiesV1:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{route.base_url}/capabilities")
            response.raise_for_status()
        return RuntimeCapabilitiesV1.model_validate(response.json())

    async def invoke(self, route: RuntimeRoute, request: RuntimeInvokeRequestV1) -> RuntimeInvokeResponseV1:
        async with httpx.AsyncClient(timeout=route.revision.spec.budgets.timeout_seconds) as client:
            response = await client.post(f"{route.base_url}/invoke", json=request.model_dump(mode="json"))
            response.raise_for_status()
        return RuntimeInvokeResponseV1.model_validate(response.json())


@dataclass(slots=True)
class _Circuit:
    failures: int = 0
    opened_at: float | None = None


class RuntimeInvoker:
    """对已授权 Runtime 调用实施实时 grant、并发、超时、熔断及 citation 复核。"""

    def __init__(self, store: object, client: RuntimeClient, *, failure_threshold: int = 3, recovery_seconds: float = 30) -> None:
        if failure_threshold < 1 or recovery_seconds <= 0:
            raise ValueError("熔断配置非法")
        self._store = store
        self._client = client
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._routes: dict[str, RuntimeRoute] = {}
        self._circuits: dict[str, _Circuit] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def set_route(self, route: RuntimeRoute) -> None:
        """由部署投影更新 Active Route；阶段 4 会以数据库事务驱动此操作。"""

        self._routes[route.agent_id] = route
        self._semaphores[route.agent_id] = asyncio.Semaphore(route.max_concurrency)

    def remove_route(self, agent_id: str) -> None:
        self._routes.pop(agent_id, None)
        self._semaphores.pop(agent_id, None)

    async def invoke(self, principal: Principal, *, agent_id: str, request_id: str, session_id: str, task: str) -> RuntimeInvokeResponseV1:
        """调用前实时复核 grant，任何 Runtime 协议或 citation 越界均 fail-closed。"""

        if not self._store.has_grant(principal.user_id, agent_id):
            raise DomainError("AUTHORIZATION_ERROR", "当前用户没有该 Agent 的调用权限", status_code=403)
        route = self._routes.get(agent_id)
        if route is None:
            raise DomainError("AGENT_INACTIVE", "Agent 当前未上线", status_code=409)
        self._ensure_circuit_closed(agent_id)
        semaphore = self._semaphores[agent_id]
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=5)
        except TimeoutError as exc:
            raise DomainError("AGENT_BUSY", "Agent 当前繁忙", status_code=503) from exc
        try:
            request = RuntimeInvokeRequestV1(
                schema_version="muye.ai/runtime-invoke-request/v1",
                request_id=request_id,
                session_id=session_id,
                user_id=principal.user_id,
                task=task,
            )
            capabilities = await self._client.capabilities(route)
            self._verify_capabilities(route, capabilities)
            result = await self._client.invoke(route, request)
            self._verify_result(route, request, result)
            if self._is_dependency_failure(result):
                self._record_failure(agent_id)
                raise DomainError("RUNTIME_UNAVAILABLE", "Agent Runtime 暂时不可用", status_code=503)
            self._circuits[agent_id] = _Circuit()
            return result
        except DomainError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            self._record_failure(agent_id)
            raise DomainError("RUNTIME_UNAVAILABLE", "Agent Runtime 暂时不可用", status_code=503) from exc
        finally:
            semaphore.release()

    def _ensure_circuit_closed(self, agent_id: str) -> None:
        circuit = self._circuits.setdefault(agent_id, _Circuit())
        if circuit.opened_at is not None and time.monotonic() - circuit.opened_at < self._recovery_seconds:
            raise DomainError("AGENT_NOT_READY", "Agent 暂时熔断", status_code=503)
        if circuit.opened_at is not None:
            self._circuits[agent_id] = _Circuit()

    def _record_failure(self, agent_id: str) -> None:
        circuit = self._circuits.setdefault(agent_id, _Circuit())
        circuit.failures += 1
        if circuit.failures >= self._failure_threshold:
            circuit.opened_at = time.monotonic()

    @staticmethod
    def _verify_capabilities(route: RuntimeRoute, capabilities: RuntimeCapabilitiesV1) -> None:
        revision = route.revision
        if (capabilities.agent_id, capabilities.revision_id, capabilities.revision_checksum) != (revision.agent_id, revision.revision_id, revision.checksum):
            raise ValueError("Runtime capabilities identity 不匹配")

    @staticmethod
    def _verify_result(route: RuntimeRoute, request: RuntimeInvokeRequestV1, result: RuntimeInvokeResponseV1) -> None:
        if result.request_id != request.request_id:
            raise ValueError("Runtime 响应 request_id 不匹配")
        allowed_assets = {asset.asset_id for asset in route.revision.spec.source_assets}
        if any(citation.source_asset_id not in allowed_assets for citation in result.citations):
            raise ValueError("Runtime citation 不属于当前 Agent Revision")

    @staticmethod
    def _is_dependency_failure(result: RuntimeInvokeResponseV1) -> bool:
        """业务拒答不影响熔断；基础设施、模型和超时错误必须计入失败。"""

        return result.status == "error" or result.error_code in {
            "DEPENDENCY_UNAVAILABLE",
            "RUNTIME_TIMEOUT",
            "MODEL_EMPTY_RESPONSE",
        }
