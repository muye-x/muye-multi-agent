"""每个 SubAgent 独立的并发上限与 circuit breaker。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time


class SubAgentRuntimeError(RuntimeError):
    """Main 在发起网络调用前拒绝过载或熔断的目标 Agent。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


class SubAgentRuntimeGuard:
    """状态按稳定 agent_id 隔离，一个 Agent 故障不会影响其他 Agent。"""

    def __init__(self, *, failure_threshold: int = 3, recovery_seconds: float = 30.0) -> None:
        if failure_threshold < 1 or failure_threshold > 20:
            raise ValueError("circuit failure threshold 必须为 1 至 20")
        if recovery_seconds <= 0 or recovery_seconds > 3600:
            raise ValueError("circuit recovery seconds 必须为 0 至 3600")
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._circuits: dict[str, _CircuitState] = {}
        self._semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}

    async def acquire(self, agent_id: str, max_concurrency: int) -> asyncio.Semaphore:
        state = self._circuits.setdefault(agent_id, _CircuitState())
        now = time.monotonic()
        if state.opened_at is not None:
            if now - state.opened_at < self._recovery_seconds:
                raise SubAgentRuntimeError("AGENT_NOT_READY", "子 Agent 熔断中，请稍后重试")
            state.opened_at = None
            state.failures = 0
        semaphore = self._semaphores.setdefault((agent_id, max_concurrency), asyncio.Semaphore(max_concurrency))
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise SubAgentRuntimeError("AGENT_NOT_READY", "子 Agent 当前并发已满") from exc
        return semaphore

    def succeeded(self, agent_id: str) -> None:
        self._circuits[agent_id] = _CircuitState()

    def failed(self, agent_id: str) -> None:
        state = self._circuits.setdefault(agent_id, _CircuitState())
        state.failures += 1
        if state.failures >= self._failure_threshold:
            state.opened_at = time.monotonic()
