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
    """管理子 Agent 的请求预算、并发队列和熔断状态。

    同一请求中的同一 Agent 只能调用一次。知识 Agent 会在自己的边界内完成检索和
    汇总，Main 不应把一个问题拆成多次并行调用；该限制同时阻止模型在失败后放大重试。
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        queue_wait_seconds: float = 10.0,
        max_calls_per_request: int = 1,
    ) -> None:
        if failure_threshold < 1 or failure_threshold > 20:
            raise ValueError("circuit failure threshold 必须为 1 至 20")
        if recovery_seconds <= 0 or recovery_seconds > 3600:
            raise ValueError("circuit recovery seconds 必须为 0 至 3600")
        if queue_wait_seconds <= 0 or queue_wait_seconds > 300:
            raise ValueError("sub agent queue wait seconds 必须为 0 至 300")
        if max_calls_per_request < 1 or max_calls_per_request > 20:
            raise ValueError("sub agent max calls per request 必须为 1 至 20")
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._queue_wait_seconds = queue_wait_seconds
        self._max_calls_per_request = max_calls_per_request
        self._circuits: dict[str, _CircuitState] = {}
        self._semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}
        self._request_call_counts: dict[tuple[str, str], int] = {}

    async def acquire(
        self,
        agent_id: str,
        max_concurrency: int,
        *,
        request_id: str = "",
    ) -> asyncio.Semaphore:
        """预留本次调用预算并在限定时间内等待子 Agent 的并发名额。"""
        if max_concurrency < 1:
            raise ValueError("sub agent max concurrency 必须至少为 1")
        state = self._circuits.setdefault(agent_id, _CircuitState())
        now = time.monotonic()
        if state.opened_at is not None:
            if now - state.opened_at < self._recovery_seconds:
                raise SubAgentRuntimeError("AGENT_NOT_READY", "子 Agent 熔断中，请稍后重试")
            state.opened_at = None
            state.failures = 0
        if request_id:
            request_key = (agent_id, request_id)
            calls = self._request_call_counts.get(request_key, 0)
            if calls >= self._max_calls_per_request:
                raise SubAgentRuntimeError(
                    "REQUEST_LIMIT_REACHED",
                    "本次请求已调用该子 Agent，请基于已有结果回答，不要重复查询",
                )
            self._request_call_counts[request_key] = calls + 1
        semaphore = self._semaphores.setdefault((agent_id, max_concurrency), asyncio.Semaphore(max_concurrency))
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=self._queue_wait_seconds)
        except TimeoutError as exc:
            raise SubAgentRuntimeError("AGENT_NOT_READY", "子 Agent 当前繁忙，请稍后重试") from exc
        return semaphore

    def succeeded(self, agent_id: str) -> None:
        self._circuits[agent_id] = _CircuitState()

    def failed(self, agent_id: str) -> None:
        state = self._circuits.setdefault(agent_id, _CircuitState())
        state.failures += 1
        if state.failures >= self._failure_threshold:
            state.opened_at = time.monotonic()

    def finish_request(self, request_id: str) -> None:
        """清理已结束请求的调用预算，保留运行中的请求限制。

        调用预算需要覆盖一次 Main 模型执行的全部工具轮次，因而不能在单次
        SubAgent 调用返回时清除；Main 的请求生命周期结束后必须主动释放，避免
        常驻进程随着历史 trace_id 无限增长。
        """
        if not request_id:
            return
        completed_keys = [key for key in self._request_call_counts if key[1] == request_id]
        for key in completed_keys:
            del self._request_call_counts[key]
