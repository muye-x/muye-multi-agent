"""Rerank provider、可靠性策略与 API 契约测试。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from config.settings import RerankModelSettings
from src.api.chat import RerankRequest, rerank
from src.core.model_registry import RerankModelRegistry, RerankModelSelection
from src.core.rerank import (
    DashScopeRerankProvider,
    RerankClient,
    RerankItem,
    RerankProviderError,
    RerankResult,
)
from src.utils.exceptions import LLMCallException, ServiceUnavailableException


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeHttpClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


class _FakeProvider:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def rerank(self, **kwargs: Any) -> tuple[RerankItem, ...]:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


def _selection() -> RerankModelSelection:
    return RerankModelSelection(
        id="public-reranker",
        name="Public Reranker",
        provider_model="provider-secret-model",
        provider="dashscope",
    )


def _registry() -> RerankModelRegistry:
    return RerankModelRegistry(
        [
            RerankModelSettings(
                id="public-reranker",
                name="Public Reranker",
                provider_model="provider-secret-model",
                provider="dashscope",
            )
        ],
        default_model="public-reranker",
    )


def _client(provider: _FakeProvider, *, max_retries: int = 0) -> RerankClient:
    return RerankClient(
        enabled=True,
        model_registry=_registry(),
        providers={"dashscope": provider},
        max_retries=max_retries,
        max_documents=5,
        max_query_chars=100,
        max_document_chars=100,
        max_total_chars=300,
    )


def test_dashscope_adapter_maps_request_and_sorts_results() -> None:
    http_client = _FakeHttpClient(
        [
            _FakeResponse(
                200,
                {
                    "output": {
                        "results": [
                            {"index": 1, "relevance_score": 0.25},
                            {"index": 0, "relevance_score": 0.9},
                        ]
                    }
                },
            )
        ]
    )
    provider = DashScopeRerankProvider(
        api_url="https://dashscope.test/rerank",
        api_key="secret-key",
        timeout=1,
        http_client=http_client,
    )

    items = asyncio.run(
        provider.rerank(
            model=_selection(),
            query="query",
            documents=["first", "second"],
            top_n=2,
        )
    )

    assert items == (RerankItem(index=0, score=0.9), RerankItem(index=1, score=0.25))
    call = http_client.calls[0]
    assert call["url"] == "https://dashscope.test/rerank"
    assert call["json"] == {
        "model": "provider-secret-model",
        "input": {"query": "query", "documents": ["first", "second"]},
        "parameters": {"return_documents": False, "top_n": 2},
    }
    assert call["headers"]["Authorization"] == "Bearer secret-key"


@pytest.mark.parametrize(
    "raw_results,category",
    [
        ([], "invalid_response"),
        ([{"index": 2, "relevance_score": 0.1}], "invalid_index"),
        (
            [
                {"index": 0, "relevance_score": 0.2},
                {"index": 0, "relevance_score": 0.1},
            ],
            "invalid_index",
        ),
        ([{"index": 0, "relevance_score": float("nan")}], "invalid_score"),
        ([{"index": True, "relevance_score": 0.1}], "invalid_index"),
    ],
)
def test_dashscope_adapter_rejects_untrusted_results(
    raw_results: list[dict[str, Any]], category: str
) -> None:
    with pytest.raises(RerankProviderError) as exc_info:
        DashScopeRerankProvider._parse_results(
            {"output": {"results": raw_results}},
            document_count=2,
            top_n=2,
        )

    assert exc_info.value.category == category
    assert not exc_info.value.retryable


@pytest.mark.parametrize(
    "status_code,category,retryable",
    [
        (400, "upstream_request", False),
        (401, "authentication", False),
        (408, "timeout", True),
        (429, "rate_limited", True),
        (503, "upstream_server", True),
    ],
)
def test_dashscope_http_errors_are_classified_without_response_body(
    status_code: int, category: str, retryable: bool
) -> None:
    response = _FakeResponse(status_code, {"message": "sensitive upstream body"})

    with pytest.raises(RerankProviderError) as exc_info:
        DashScopeRerankProvider._raise_for_status(response)

    assert exc_info.value.category == category
    assert exc_info.value.retryable is retryable
    assert "sensitive upstream body" not in str(exc_info.value)


def test_retry_only_retries_recoverable_errors_and_redacts_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _FakeProvider(
        [
            RerankProviderError("rate_limited", retryable=True),
            (RerankItem(index=0, score=0.8),),
        ]
    )
    client = _client(provider, max_retries=1)

    async def call() -> RerankResult:
        client._sleep_before_retry = lambda _attempt: asyncio.sleep(0)
        return await client.rerank(
            query="sensitive-query",
            documents=["sensitive-document"],
            top_n=1,
            trace_id="trace-1",
        )

    with caplog.at_level(logging.INFO):
        result = asyncio.run(call())

    assert result.items == (RerankItem(index=0, score=0.8),)
    assert len(provider.calls) == 2
    assert "sensitive-query" not in caplog.text
    assert "sensitive-document" not in caplog.text
    assert "rate_limited" in caplog.text


def test_non_retryable_error_stops_after_first_attempt() -> None:
    provider = _FakeProvider(
        [RerankProviderError("authentication", retryable=False)]
    )
    client = _client(provider, max_retries=3)

    with pytest.raises(LLMCallException):
        asyncio.run(
            client.rerank(query="query", documents=["document"], top_n=1)
        )

    assert len(provider.calls) == 1


def test_cancellation_propagates_and_provider_is_closed() -> None:
    provider = _FakeProvider([asyncio.CancelledError()])
    client = _client(provider)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            client.rerank(query="query", documents=["document"], top_n=1)
        )

    asyncio.run(client.aclose())
    assert provider.closed


def test_disabled_rerank_fails_without_provider_call() -> None:
    client = RerankClient(
        enabled=False,
        model_registry=_registry(),
        providers={},
    )

    with pytest.raises(ServiceUnavailableException) as exc_info:
        asyncio.run(
            client.rerank(query="query", documents=["document"], top_n=1)
        )

    assert exc_info.value.code == 503


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "query", "documents": ["document"], "top_n": "1"},
        {"query": "query", "documents": ["document"], "top_n": 2},
        {"query": " ", "documents": ["document"], "top_n": 1},
        {"query": "query", "documents": [" "], "top_n": 1},
        {
            "query": "query",
            "documents": ["document"],
            "top_n": 1,
            "unknown": True,
        },
        {
            "query": "query",
            "documents": ["document"],
            "top_n": 1,
            "trace_id": "invalid trace",
        },
    ],
)
def test_rerank_request_is_strict(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RerankRequest.model_validate(payload)


def test_rerank_api_returns_only_indices_and_scores() -> None:
    class _ApiClient:
        async def rerank(self, **_kwargs: Any) -> RerankResult:
            return RerankResult(
                model_alias="public-reranker",
                items=(RerankItem(index=1, score=0.75),),
                latency_ms=5,
            )

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(llm_client=_ApiClient()))
    )
    response = asyncio.run(
        rerank(
            RerankRequest(
                query="sensitive-query",
                documents=["first", "sensitive-document"],
                top_n=1,
            ),
            request,
        )
    )
    body = response.model_dump()

    assert body["data"] == {
        "model": "public-reranker",
        "results": [{"index": 1, "score": 0.75}],
        "count": 1,
    }
    assert "sensitive-query" not in str(body)
    assert "sensitive-document" not in str(body)
