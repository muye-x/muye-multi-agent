"""Embedding alias、响应维度和模型能力 API 测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from config.settings import EmbeddingModelSettings, RerankModelSettings
from src.api.chat import EmbedRequest, embed, list_models
from src.core.llm_client import LLMNodeConfig, MultiLLMClient
from src.core.model_registry import (
    EmbeddingModelRegistry,
    RerankModelRegistry,
)
from src.core.rerank import RerankClient
from src.utils.exceptions import InvalidRequestException


class _CloseableFake:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeEmbeddings:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class _FakeEmbeddingClient(_CloseableFake):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.embeddings = _FakeEmbeddings(response)


def _embedding_registry(*, dimensions: int | None = 2) -> EmbeddingModelRegistry:
    return EmbeddingModelRegistry(
        [
            EmbeddingModelSettings(
                id="public-embedding",
                name="Public Embedding",
                provider_model="provider-secret-embedding",
                dimensions=dimensions,
            )
        ],
        default_model="public-embedding",
    )


def _rerank_client(*, enabled: bool = False) -> RerankClient:
    registry = RerankModelRegistry(
        [
            RerankModelSettings(
                id="public-reranker",
                name="Public Reranker",
                provider_model="provider-secret-reranker",
            )
        ],
        default_model="public-reranker",
    )
    return RerankClient(
        enabled=enabled,
        model_registry=registry,
        providers={},
    )


def _client(
    response: Any,
    *,
    dimensions: int | None = 2,
    rerank_enabled: bool = False,
) -> tuple[MultiLLMClient, _FakeEmbeddingClient]:
    embedding_client = _FakeEmbeddingClient(response)
    client = MultiLLMClient(
        api_config=LLMNodeConfig(
            base_url="http://llm.test/v1",
            api_key="test",
            extra_body={},
        ),
        client=_CloseableFake(),
        embedding_client=embedding_client,
        embedding_model_registry=_embedding_registry(dimensions=dimensions),
        rerank_client=_rerank_client(enabled=rerank_enabled),
    )
    return client, embedding_client


def _response(*vectors: list[float]) -> Any:
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=vector) for vector in vectors]
    )


def _request(client: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(llm_client=client)))


def test_embed_alias_maps_to_provider_and_returns_dimensions() -> None:
    client, embedding_client = _client(_response([0.1, 0.2], [0.3, 0.4]))

    result = asyncio.run(
        client.embed_result(
            ["first", "second"],
            trace_id="trace-1",
            model="public-embedding",
        )
    )

    assert result is not None
    assert result.model_alias == "public-embedding"
    assert result.dimensions == 2
    assert embedding_client.embeddings.calls == [
        {"model": "provider-secret-embedding", "input": ["first", "second"]}
    ]


def test_legacy_embed_method_keeps_list_return_type() -> None:
    client, _ = _client(_response([0.1, 0.2]))

    embeddings = asyncio.run(client.embed(["text"]))

    assert embeddings == [[0.1, 0.2]]


def test_embed_rejects_unknown_alias_and_dimension_mismatch() -> None:
    client, _ = _client(_response([0.1, 0.2, 0.3]))

    with pytest.raises(InvalidRequestException):
        asyncio.run(client.embed_result(["text"], model="provider-secret-embedding"))
    assert asyncio.run(client.embed_result(["text"])) is None


def test_parse_embeddings_rejects_empty_response_without_assert() -> None:
    """空响应必须在优化模式下仍然以明确错误拒绝。"""
    selection = _embedding_registry().resolve(None)

    with pytest.raises(ValueError, match="未返回任何向量"):
        MultiLLMClient._parse_embeddings(
            _response(),
            input_count=0,
            selection=selection,
        )


def test_embed_api_response_adds_model_and_dimensions() -> None:
    client, _ = _client(_response([0.1, 0.2]))

    response = asyncio.run(
        embed(
            EmbedRequest(texts=["text"], model="public-embedding"),
            _request(client),
        )
    )

    assert response.model_dump()["data"] == {
        "embeddings": [[0.1, 0.2]],
        "count": 1,
        "model": "public-embedding",
        "dimensions": 2,
    }


def test_models_api_lists_aliases_without_provider_details() -> None:
    client, _ = _client(
        _response([0.1, 0.2]),
        rerank_enabled=True,
    )

    response = asyncio.run(list_models(_request(client)))
    body = response.model_dump()["data"]

    assert body["default_embedding_model"] == "public-embedding"
    assert body["embedding_models"] == [
        {
            "id": "public-embedding",
            "name": "Public Embedding",
            "dimensions": 2,
            "is_default": True,
        }
    ]
    assert body["default_rerank_model"] == "public-reranker"
    assert body["rerank_models"] == [
        {
            "id": "public-reranker",
            "name": "Public Reranker",
            "is_default": True,
        }
    ]
    serialized = str(body)
    assert "provider-secret-embedding" not in serialized
    assert "provider-secret-reranker" not in serialized


def test_client_lifecycle_closes_embedding_and_rerank_resources() -> None:
    client, embedding_client = _client(_response([0.1, 0.2]))
    rerank_client = client.rerank_client

    asyncio.run(client.aclose())

    assert embedding_client.closed
    # Closing an empty provider map is still a valid deterministic lifecycle operation.
    asyncio.run(rerank_client.aclose())
