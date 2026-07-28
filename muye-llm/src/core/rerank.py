"""Rerank 模型调用、DashScope 协议适配和可靠性策略。

公共服务只接受模型 alias；本模块负责将 alias 映射为供应商模型、执行有限重试、
校验不可信的上游 JSON，并确保日志不包含查询或候选文档正文。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from config.settings import settings
from src.core.model_registry import (
    ModelSelectionError,
    RerankModelRegistry,
    RerankModelSelection,
)
from src.utils.exceptions import (
    InvalidRequestException,
    LLMCallException,
    ServiceUnavailableException,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RerankItem:
    """供应商返回并经校验的一项排序结果。"""

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    """一次 Rerank 调用结果，仅包含候选索引、分数和脱敏元数据。"""

    model_alias: str
    items: tuple[RerankItem, ...]
    latency_ms: int


class RerankProviderError(Exception):
    """供应商调用失败，携带是否可重试及脱敏错误类别。"""

    def __init__(self, category: str, *, retryable: bool) -> None:
        super().__init__(category)
        self.category = category
        self.retryable = retryable


class RerankProvider(Protocol):
    """供应商适配器必须实现的最小只读协议。"""

    async def rerank(
        self,
        *,
        model: RerankModelSelection,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> tuple[RerankItem, ...]:
        """执行一次供应商请求；失败时抛出 ``RerankProviderError``。"""

    async def aclose(self) -> None:
        """关闭底层连接池。"""


class DashScopeRerankProvider:
    """DashScope text-rerank HTTP 协议适配器。

    API URL 必须是完整服务地址。适配器不负责业务重试，仅分类 HTTP/传输错误并
    严格验证响应中每个 ``index`` 和 ``relevance_score``。
    """

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        timeout: float,
        http_client: Any | None = None,
    ) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._timeout = timeout
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        """关闭 HTTP 客户端；兼容测试 fake 的同步或异步 close。"""
        for method_name in ("aclose", "close"):
            method = getattr(self._http_client, method_name, None)
            if method is None:
                continue
            result = method()
            if inspect.isawaitable(result):
                await result
            return

    async def rerank(
        self,
        *,
        model: RerankModelSelection,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> tuple[RerankItem, ...]:
        """发起一次 DashScope 调用并返回按分数降序排列的结果。"""
        payload = {
            "model": model.provider_model,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False, "top_n": top_n},
        }
        try:
            response = await asyncio.wait_for(
                self._http_client.post(
                    self._api_url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise RerankProviderError("timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            raise RerankProviderError("connection", retryable=True) from exc
        except Exception as exc:
            raise RerankProviderError("transport", retryable=False) from exc

        self._raise_for_status(response)
        try:
            payload_data = response.json()
        except Exception as exc:
            raise RerankProviderError("invalid_json", retryable=False) from exc
        return self._parse_results(payload_data, document_count=len(documents), top_n=top_n)

    @staticmethod
    def _raise_for_status(response: Any) -> None:
        """将供应商 HTTP 状态映射为稳定、脱敏的错误类别。"""
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            raise RerankProviderError("invalid_http_response", retryable=False)
        if 200 <= status_code < 300:
            return
        if status_code == 408:
            raise RerankProviderError("timeout", retryable=True)
        if status_code == 429:
            raise RerankProviderError("rate_limited", retryable=True)
        if status_code >= 500:
            raise RerankProviderError("upstream_server", retryable=True)
        if status_code in {401, 403}:
            raise RerankProviderError("authentication", retryable=False)
        raise RerankProviderError("upstream_request", retryable=False)

    @staticmethod
    def _parse_results(
        payload: Any,
        *,
        document_count: int,
        top_n: int,
    ) -> tuple[RerankItem, ...]:
        """校验 DashScope 结果结构、索引唯一性、范围和有限分数。"""
        if not isinstance(payload, dict):
            raise RerankProviderError("invalid_response", retryable=False)
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RerankProviderError("invalid_response", retryable=False)
        raw_results = output.get("results")
        if (
            not isinstance(raw_results, list)
            or not raw_results
            or len(raw_results) > top_n
        ):
            raise RerankProviderError("invalid_response", retryable=False)

        seen_indices: set[int] = set()
        items: list[RerankItem] = []
        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                raise RerankProviderError("invalid_response", retryable=False)
            index = raw_result.get("index")
            score = raw_result.get("relevance_score")
            if isinstance(index, bool) or not isinstance(index, int):
                raise RerankProviderError("invalid_index", retryable=False)
            if index < 0 or index >= document_count or index in seen_indices:
                raise RerankProviderError("invalid_index", retryable=False)
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise RerankProviderError("invalid_score", retryable=False)
            normalized_score = float(score)
            if not math.isfinite(normalized_score):
                raise RerankProviderError("invalid_score", retryable=False)
            seen_indices.add(index)
            items.append(RerankItem(index=index, score=normalized_score))

        return tuple(sorted(items, key=lambda item: (-item.score, item.index)))


class RerankClient:
    """Rerank 业务客户端，负责 alias、输入预算、重试和生命周期。"""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        model_registry: RerankModelRegistry | None = None,
        providers: dict[str, RerankProvider] | None = None,
        max_retries: int | None = None,
        max_documents: int | None = None,
        max_query_chars: int | None = None,
        max_document_chars: int | None = None,
        max_total_chars: int | None = None,
    ) -> None:
        self.enabled = settings.rerank_enabled if enabled is None else enabled
        self.model_registry = model_registry or RerankModelRegistry(
            settings.rerank_models,
            default_model=settings.rerank_default_model,
        )
        self._providers = (
            providers
            if providers is not None
            else {
                "dashscope": DashScopeRerankProvider(
                    api_url=settings.rerank_api_url,
                    api_key=settings.rerank_api_key,
                    timeout=settings.rerank_timeout,
                )
            }
        )
        self._max_retries = (
            settings.rerank_max_retries if max_retries is None else max_retries
        )
        self._max_documents = (
            settings.rerank_max_documents if max_documents is None else max_documents
        )
        self._max_query_chars = (
            settings.rerank_max_query_chars if max_query_chars is None else max_query_chars
        )
        self._max_document_chars = (
            settings.rerank_max_document_chars
            if max_document_chars is None
            else max_document_chars
        )
        self._max_total_chars = (
            settings.rerank_max_total_chars if max_total_chars is None else max_total_chars
        )

    async def aclose(self) -> None:
        """并发关闭所有 provider 连接，单个关闭错误不阻断其他资源释放。"""
        await asyncio.gather(
            *(provider.aclose() for provider in self._providers.values()),
            return_exceptions=True,
        )

    async def _sleep_before_retry(self, attempt: int) -> None:
        """有限指数退避并加入小幅抖动。"""
        delay = min(0.25 * (2**attempt), 2.0) + random.uniform(0, 0.1)
        await asyncio.sleep(delay)

    def _validate_request(
        self,
        query: str,
        documents: list[str],
        top_n: int,
        trace_id: str,
    ) -> str:
        """保护直接 Python 调用路径，与 HTTP 请求模型执行相同的文本预算校验。"""
        if not isinstance(query, str) or not query.strip():
            raise InvalidRequestException("query 不能为空")
        if len(query) > self._max_query_chars:
            raise InvalidRequestException("query 超过长度限制")
        if not isinstance(documents, list) or not documents or len(documents) > self._max_documents:
            raise InvalidRequestException("documents 数量超出限制")
        if isinstance(top_n, bool) or not isinstance(top_n, int):
            raise InvalidRequestException("top_n 必须是整数")
        if top_n < 1 or top_n > len(documents):
            raise InvalidRequestException("top_n 必须在 1 和 documents 数量之间")
        for document in documents:
            if not isinstance(document, str) or not document.strip():
                raise InvalidRequestException("documents 不能包含空文本")
            if len(document) > self._max_document_chars:
                raise InvalidRequestException("document 超过长度限制")
        if len(query) + sum(len(document) for document in documents) > self._max_total_chars:
            raise InvalidRequestException("Rerank 请求文本总量超过限制")
        if not isinstance(trace_id, str):
            raise InvalidRequestException("trace_id 必须是字符串")
        normalized_trace_id = trace_id.strip()
        if normalized_trace_id and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
            normalized_trace_id,
        ) is None:
            raise InvalidRequestException("trace_id 格式无效")
        return normalized_trace_id

    def resolve_model(self, model: str | None) -> RerankModelSelection:
        """解析公开 alias，并将选择错误转换为 HTTP 400 领域异常。"""
        try:
            return self.model_registry.resolve(model)
        except ModelSelectionError as exc:
            raise InvalidRequestException(str(exc)) from exc

    async def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
        model: str | None = None,
        trace_id: str = "",
    ) -> RerankResult:
        """执行有限重试的只读 Rerank；取消始终原样向上传播。"""
        if not self.enabled:
            raise ServiceUnavailableException("Rerank 功能未启用")
        trace_id = self._validate_request(query, documents, top_n, trace_id)
        selection = self.resolve_model(model)
        provider = self._providers.get(selection.provider)
        if provider is None:
            raise LLMCallException("Rerank 服务配置错误")

        started_at = time.monotonic()
        last_error: RerankProviderError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                items = await provider.rerank(
                    model=selection,
                    query=query,
                    documents=documents,
                    top_n=top_n,
                )
                latency_ms = int((time.monotonic() - started_at) * 1000)
                logger.info(
                    "[LLM.rerank] success trace_id=%s model=%s documents=%s "
                    "results=%s attempt=%s latency_ms=%s",
                    trace_id,
                    selection.id,
                    len(documents),
                    len(items),
                    attempt + 1,
                    latency_ms,
                )
                return RerankResult(
                    model_alias=selection.id,
                    items=items,
                    latency_ms=latency_ms,
                )
            except asyncio.CancelledError:
                logger.info(
                    "[LLM.rerank] cancelled trace_id=%s model=%s documents=%s",
                    trace_id,
                    selection.id,
                    len(documents),
                )
                raise
            except RerankProviderError as exc:
                last_error = exc
                logger.warning(
                    "[LLM.rerank] failure trace_id=%s model=%s documents=%s "
                    "attempt=%s category=%s retryable=%s",
                    trace_id,
                    selection.id,
                    len(documents),
                    attempt + 1,
                    exc.category,
                    exc.retryable,
                )
                if not exc.retryable or attempt >= self._max_retries:
                    break
                await self._sleep_before_retry(attempt)

        logger.error(
            "[LLM.rerank] exhausted trace_id=%s model=%s documents=%s category=%s",
            trace_id,
            selection.id,
            len(documents),
            last_error.category if last_error else "unknown",
        )
        raise LLMCallException("Rerank 服务调用失败") from last_error
