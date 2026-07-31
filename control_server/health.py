"""Control 对 candidate Agent 执行固定 URL 的健康与身份校验。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any

import httpx
from pydantic import SecretStr

from contracts.models import AgentCatalogEntryV1, AgentCatalogSnapshotV1
from contracts.catalog import build_catalog_snapshot, capabilities_checksum_from_response


class CatalogCandidateError(RuntimeError):
    """candidate 的进程状态、协议或部署身份无法通过 fail-closed 校验。"""


@dataclass(frozen=True, slots=True)
class HealthObservation:
    """一次探测在连续阈值状态机中的结果。"""

    status: str
    changed: bool
    consecutive_successes: int
    consecutive_failures: int


@dataclass(slots=True)
class _HealthState:
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    last_observed_at: float | None = None


class AgentHealthCollector:
    """验证 candidate，并以固定时间窗口维护连续健康/失败阈值。"""

    def __init__(
        self,
        *,
        token_provider: Callable[[str], str | SecretStr],
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        failure_threshold: int = 3,
        success_threshold: int = 2,
        observation_window_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= failure_threshold <= 20:
            raise ValueError("health failure threshold 必须为 1 至 20")
        if not 1 <= success_threshold <= 20:
            raise ValueError("health success threshold 必须为 1 至 20")
        if not 1 <= observation_window_seconds <= 3600:
            raise ValueError("health observation window 必须为 1 至 3600 秒")
        self._token_provider = token_provider
        self._client_factory = client_factory
        self._failure_threshold = failure_threshold
        self._success_threshold = success_threshold
        self._observation_window_seconds = observation_window_seconds
        self._monotonic = monotonic
        self._states: dict[str, _HealthState] = {}

    async def validate_candidate(self, candidate: AgentCatalogSnapshotV1) -> AgentCatalogSnapshotV1:
        """探测全部 candidate entry，并返回状态全部为 ACTIVE 的新快照。"""
        active_entries: list[AgentCatalogEntryV1] = []
        for entry in candidate.agents:
            if entry.status not in {"STARTING", "ACTIVE"}:
                raise CatalogCandidateError(f"candidate Agent 状态不可激活：{entry.agent_id}")
            await self.probe(entry)
            self._states[entry.agent_id] = _HealthState(consecutive_successes=self._success_threshold)
            active_entries.append(entry.model_copy(update={"status": "ACTIVE"}))
        return build_catalog_snapshot(active_entries)

    async def probe(self, entry: AgentCatalogEntryV1) -> None:
        """探测固定 health/readiness/capabilities URL 并验证部署身份。"""
        try:
            token = self._token_provider(entry.agent_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogCandidateError(f"Agent 缺少目标绑定服务凭据：{entry.agent_id}") from exc
        token_value = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not isinstance(token_value, str) or not token_value.strip():
            raise CatalogCandidateError(f"Agent 缺少目标绑定服务凭据：{entry.agent_id}")
        headers = {"Authorization": f"Bearer {token_value.strip()}"}
        try:
            async with self._client_factory(timeout=entry.timeout_seconds) as client:
                health = await client.get(f"{entry.base_url.rstrip('/')}/health")
                health.raise_for_status()
                ready = await client.get(f"{entry.base_url.rstrip('/')}/ready")
                ready.raise_for_status()
                capabilities_response = await client.get(
                    f"{entry.base_url.rstrip('/')}/capabilities",
                    headers=headers,
                )
                capabilities_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalogCandidateError(f"Agent 健康或能力探测失败：{entry.agent_id}") from exc
        if self._object(health, "health").get("status") not in {"ok", "healthy"}:
            raise CatalogCandidateError(f"Agent health 状态无效：{entry.agent_id}")
        if self._object(ready, "ready").get("status") != "ready":
            raise CatalogCandidateError(f"Agent readiness 状态无效：{entry.agent_id}")
        capabilities = self._object(capabilities_response, "capabilities")
        if capabilities.get("agent_name") != entry.service_name.removeprefix("agent-"):
            raise CatalogCandidateError(f"Agent capabilities 名称不匹配：{entry.agent_id}")
        if capabilities.get("version") != entry.agent_version:
            raise CatalogCandidateError(f"Agent capabilities 版本不匹配：{entry.agent_id}")
        try:
            actual_checksum = capabilities_checksum_from_response(capabilities)
        except ValueError as exc:
            raise CatalogCandidateError(f"Agent capabilities 身份无效：{entry.agent_id}") from exc
        if actual_checksum != entry.capabilities_checksum:
            raise CatalogCandidateError(f"Agent capabilities checksum 不匹配：{entry.agent_id}")

    def observe(self, *, agent_id: str, current_status: str, healthy: bool) -> HealthObservation:
        """应用连续阈值；窗口中断会清空旧计数，单次抖动不改变 Catalog。"""
        if current_status not in {"ACTIVE", "DEGRADED"}:
            raise ValueError("只有 ACTIVE/DEGRADED Agent 可以写入运行时健康观察")
        now = self._monotonic()
        state = self._states.setdefault(agent_id, _HealthState())
        if (
            state.last_observed_at is not None
            and now - state.last_observed_at > self._observation_window_seconds
        ):
            state.consecutive_successes = 0
            state.consecutive_failures = 0
        state.last_observed_at = now
        if healthy:
            state.consecutive_successes += 1
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1
            state.consecutive_successes = 0

        status = current_status
        if current_status == "ACTIVE" and state.consecutive_failures >= self._failure_threshold:
            status = "DEGRADED"
        elif current_status == "DEGRADED" and state.consecutive_successes >= self._success_threshold:
            status = "ACTIVE"
        return HealthObservation(
            status=status,
            changed=status != current_status,
            consecutive_successes=state.consecutive_successes,
            consecutive_failures=state.consecutive_failures,
        )

    @staticmethod
    def _object(response: httpx.Response, label: str) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise CatalogCandidateError(f"Agent {label} 返回非 JSON") from exc
        if not isinstance(value, dict):
            raise CatalogCandidateError(f"Agent {label} 返回必须是对象")
        return value
