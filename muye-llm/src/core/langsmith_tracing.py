"""可选的 LangSmith 元数据 tracing，失败不会影响模型调用。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class LangSmithTracer:
    """仅异步上报脱敏的模型调用元数据，不发送 prompt、响应正文或业务 metadata。"""

    def __init__(
        self,
        *,
        enabled: bool,
        api_key: str,
        project: str,
        endpoint: str = "",
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._enabled = enabled and bool(api_key)
        self._project = project
        self._client: Any | None = None
        if not self._enabled:
            return
        try:
            if client_factory is None:
                from langsmith import Client

                client_factory = Client
            kwargs: dict[str, Any] = {"api_key": api_key}
            if endpoint:
                kwargs["api_url"] = endpoint
            self._client = client_factory(**kwargs)
        except Exception as exc:
            self._enabled = False
            logger.warning("LangSmith 初始化失败，已禁用 tracing: %s", type(exc).__name__)

    def record(self, metadata: dict[str, Any]) -> None:
        """后台上报一次脱敏调用记录；没有运行事件循环时静默跳过。"""
        if not self._enabled or self._client is None:
            return
        try:
            task = asyncio.create_task(self._create_run(metadata))
            task.add_done_callback(self._log_failure)
        except RuntimeError:
            logger.warning("LangSmith tracing 缺少运行事件循环，已跳过本次记录")

    async def _create_run(self, metadata: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self._client.create_run,
            name="muye-llm",
            run_type="llm",
            inputs={"redacted": True},
            outputs={"status": metadata.get("status", "unknown")},
            extra={"metadata": metadata},
            project_name=self._project,
        )

    @staticmethod
    def _log_failure(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except Exception as exc:
            logger.warning("LangSmith tracing 上报失败: %s", type(exc).__name__)
