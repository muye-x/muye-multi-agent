"""数据库请求形状和 muye-llm 响应校验测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.backends.base import DenseBackendQuery, KeywordBackendQuery, RetrievalBackend
from src.backends.milvus import MilvusBackend
from src.backends.opensearch import OpenSearchBackend
from src.clients.llm import MuyeLLMClient
from src.contracts import FilterExpression
from src.errors import (
    BackendProtocolError,
    BackendUnavailableError,
    EmbeddingUnavailableError,
    RerankUnavailableError,
)


def _dense_query() -> DenseBackendQuery:
    return DenseBackendQuery(
        target="documents",
        id_field="document_id",
        content_field="body.text",
        vector_field="embedding",
        vector=(0.1, 0.2),
        top_k=3,
        returned_fields={"title": "metadata.title"},
        filterable_fields={"enabled": "metadata.enabled"},
        filter=FilterExpression(op="eq", field="enabled", value=True),
        metric_type="COSINE",
        timeout_seconds=2.0,
    )


class _FakeMilvusClient:
    def __init__(self) -> None:
        self.search_kwargs: dict[str, Any] = {}
        self.described = ""
        self.closed = False

    async def search(self, **kwargs: Any) -> list[list[dict[str, Any]]]:
        self.search_kwargs = kwargs
        return [
            [
                {
                    "id": 7,
                    "distance": 0.9,
                    "entity": {
                        "document_id": 7,
                        "body.text": "hello",
                        "metadata.title": "Title",
                    },
                }
            ]
        ]

    async def describe_collection(self, *, collection_name: str) -> dict[str, Any]:
        self.described = collection_name
        return {"collection_name": collection_name}

    async def close(self) -> None:
        self.closed = True


def test_milvus_adapter_uses_search_and_maps_logical_fields() -> None:
    async def run() -> None:
        client = _FakeMilvusClient()
        backend = MilvusBackend(uri="http://milvus.test", client=client)

        hits = await backend.search(_dense_query())

        assert hits[0].id == "7"
        assert hits[0].fields == {"title": "Title"}
        assert client.search_kwargs["collection_name"] == "documents"
        assert client.search_kwargs["filter"] == "metadata.enabled == true"
        assert await backend.health("documents", timeout_seconds=1)
        await backend.aclose()
        assert client.closed

    asyncio.run(run())


class _FailingMilvusClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def search(self, **_kwargs: Any) -> Any:
        raise self.error


def test_milvus_adapter_distinguishes_permanent_and_transient_errors() -> None:
    ParamError = type("ParamError", (Exception,), {})
    MilvusUnavailableException = type("MilvusUnavailableException", (Exception,), {})

    async def run() -> None:
        permanent = MilvusBackend(
            uri="http://milvus.test",
            client=_FailingMilvusClient(ParamError()),
        )
        with pytest.raises(BackendProtocolError):
            await permanent.search(_dense_query())

        transient = MilvusBackend(
            uri="http://milvus.test",
            client=_FailingMilvusClient(MilvusUnavailableException()),
        )
        with pytest.raises(BackendUnavailableError):
            await transient.search(_dense_query())

    asyncio.run(run())


class _FakeOpenSearchClient:
    def __init__(self) -> None:
        self.search_kwargs: dict[str, Any] = {}
        self.indices = SimpleNamespace(exists=self.exists)
        self.closed = False

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.search_kwargs = kwargs
        return {
            "hits": {
                "hits": [
                    {
                        "_id": "fallback",
                        "_score": 2.5,
                        "_source": {
                            "document_id": "doc-1",
                            "body": {"text": "hello"},
                            "metadata": {"title": "Nested"},
                        },
                    }
                ]
            }
        }

    async def exists(self, *, index: str) -> bool:
        return index == "documents"

    async def close(self) -> None:
        self.closed = True


def test_opensearch_adapter_builds_knn_dsl_and_reads_nested_source() -> None:
    async def run() -> None:
        client = _FakeOpenSearchClient()
        backend = OpenSearchBackend(hosts=["http://search.test"], client=client)

        hits = await backend.search(_dense_query())

        assert hits[0].id == "doc-1"
        assert hits[0].content == "hello"
        assert hits[0].fields == {"title": "Nested"}
        body = client.search_kwargs["body"]
        assert body["query"]["knn"]["embedding"]["filter"] == {
            "term": {"metadata.enabled": True}
        }
        assert await backend.health("documents", timeout_seconds=1)
        await backend.aclose()
        assert client.closed

    asyncio.run(run())


def test_opensearch_keyword_query_uses_match_and_filter() -> None:
    query = KeywordBackendQuery(
        target="documents",
        id_field="document_id",
        content_field="content",
        keyword_field="content",
        text="refund",
        top_k=5,
        returned_fields={},
        filterable_fields={"enabled": "enabled"},
        filter=FilterExpression(op="eq", field="enabled", value=True),
        timeout_seconds=2,
    )

    body = OpenSearchBackend._build_body(query)

    assert body["query"] == {
        "bool": {
            "must": [{"match": {"content": {"query": "refund"}}}],
            "filter": [{"term": {"enabled": True}}],
        }
    }


def test_readonly_protocol_has_no_mutation_methods() -> None:
    public_names = {name for name in RetrievalBackend.__dict__ if not name.startswith("_")}

    assert public_names == {"backend_type", "capabilities", "search", "health", "aclose"}


def test_llm_client_validates_embedding_dimensions_and_rerank_indexes() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if request.url.path.endswith("/embed"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"embeddings": [[0.1, 0.2]], "dimensions": 2, "model": "embed-v1"},
                },
            )
        return httpx.Response(
            200,
            json={"success": True, "data": {"results": [{"index": 1, "score": 0.8}]}},
        )

    async def run() -> None:
        http_client = httpx.AsyncClient(
            base_url="http://llm.test",
            transport=httpx.MockTransport(handler),
        )
        client = MuyeLLMClient(base_url="http://llm.test", timeout_seconds=1, client=http_client)
        vector = await client.embed(
            "query",
            model="embed-v1",
            expected_dimensions=2,
            trace_id="trace-1",
        )
        scores = await client.rerank(
            "query",
            ["a", "b"],
            top_n=1,
            model="rerank-v1",
            trace_id="trace-1",
        )
        assert vector == (0.1, 0.2)
        assert scores[0].index == 1
        await http_client.aclose()

    asyncio.run(run())
    assert requests[0]["model"] == "embed-v1"
    assert requests[1]["model"] == "rerank-v1"


def test_llm_client_rejects_invalid_model_payloads() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embed"):
            return httpx.Response(200, json={"success": True, "data": {"embeddings": [[0.1]]}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"results": [{"index": 0, "score": 1}, {"index": 0, "score": 0.5}]},
            },
        )

    async def run() -> None:
        http_client = httpx.AsyncClient(
            base_url="http://llm.test",
            transport=httpx.MockTransport(handler),
        )
        client = MuyeLLMClient(base_url="http://llm.test", timeout_seconds=1, client=http_client)
        with pytest.raises(EmbeddingUnavailableError):
            await client.embed("q", model="e", expected_dimensions=2, trace_id="t")
        with pytest.raises(RerankUnavailableError):
            await client.rerank("q", ["a", "b"], top_n=2, model="r", trace_id="t")
        await http_client.aclose()

    asyncio.run(run())


def test_llm_client_reads_actual_embedding_and_rerank_aliases() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "embedding_models": [{"id": "embed-v1"}],
                    "rerank_models": [{"id": "rerank-v1"}],
                },
            },
        )

    async def run() -> None:
        http_client = httpx.AsyncClient(
            base_url="http://llm.test",
            transport=httpx.MockTransport(handler),
        )
        client = MuyeLLMClient(
            base_url="http://llm.test",
            timeout_seconds=1,
            client=http_client,
        )

        capabilities = await client.model_capabilities()

        assert capabilities is not None
        assert capabilities.embedding_models == {"embed-v1": None}
        assert capabilities.rerank_models == {"rerank-v1"}
        await http_client.aclose()

    asyncio.run(run())
