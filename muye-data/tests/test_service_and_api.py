"""召回编排、降级策略和只读 HTTP 表面的行为测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.backends.base import BackendCapabilities, BackendHit, DenseBackendQuery, KeywordBackendQuery
from src.clients.llm import LLMModelCapabilities, RerankScore
from src.config import DataConfig, ServiceSettings
from src.contracts import RetrieveRequest
from src.errors import (
    BackendUnavailableError,
    ConfigurationError,
    EmbeddingUnavailableError,
    RerankUnavailableError,
)
from src.retrieval.service import RetrievalService
from src.snapshots import canonical_checksum, load_resource_snapshot


_MAIN_SPEC = importlib.util.spec_from_file_location(
    "muye_data_service_main",
    Path(__file__).resolve().parents[1] / "main.py",
)
assert _MAIN_SPEC is not None and _MAIN_SPEC.loader is not None
_MAIN_MODULE = importlib.util.module_from_spec(_MAIN_SPEC)
_MAIN_SPEC.loader.exec_module(_MAIN_MODULE)
create_app = _MAIN_MODULE.create_app


def _config() -> DataConfig:
    return DataConfig.model_validate(
        {
            "version": 1,
            "connections": {"database": {"type": "milvus", "uri": "http://database.test"}},
            "resources": {
                "knowledge": {
                    "connection": "database",
                    "target": "documents",
                    "fields": {
                        "id": "document_id",
                        "content": "content",
                        "vector": "embedding",
                        "keyword": "sparse",
                        "exposed_fields": {"title": "title", "source": "source"},
                        "filterable_fields": {"enabled": "enabled"},
                    },
                    "embedding": {"model": "embed-v1", "dimensions": 2},
                    "pipelines": {
                        "dense": {"type": "dense", "candidate_k": 3},
                        "keyword": {"type": "keyword", "candidate_k": 3},
                        "keyword_rerank": {
                            "type": "keyword",
                            "candidate_k": 3,
                            "rerank": {"model": "rerank-v1", "required": False},
                        },
                        "keyword_rerank_required": {
                            "type": "keyword",
                            "candidate_k": 3,
                            "rerank": {"model": "rerank-v1", "required": True},
                        },
                        "hybrid": {
                            "type": "hybrid",
                            "dense_candidate_k": 3,
                            "keyword_candidate_k": 3,
                            "dense_weight": 1.0,
                            "keyword_weight": 1.0,
                        },
                    },
                    "default_pipeline": "hybrid",
                    "default_return_fields": ["title"],
                }
            },
        }
    )


def _settings(*, retries: int = 0) -> ServiceSettings:
    return ServiceSettings(
        host="127.0.0.1",
        port=9840,
        workers=1,
        log_level="INFO",
        config_path=Path("unused.yaml"),
        llm_base_url="http://llm.test",
        llm_timeout_seconds=1.0,
        backend_timeout_seconds=1.0,
        total_timeout_seconds=3.0,
        backend_max_retries=retries,
        rerank_max_documents=100,
    )


class _FakeBackend:
    backend_type = "fake"
    capabilities = BackendCapabilities(dense=True, keyword=True)

    def __init__(self) -> None:
        self.fail_dense = False
        self.fail_count = 0
        self.search_calls: list[Any] = []
        self.health_calls = 0
        self.ready = True
        self.closed = False

    async def search(self, query: DenseBackendQuery | KeywordBackendQuery) -> list[BackendHit]:
        self.search_calls.append(query)
        if self.fail_count:
            self.fail_count -= 1
            raise BackendUnavailableError()
        if isinstance(query, DenseBackendQuery):
            if self.fail_dense:
                raise BackendUnavailableError()
            return [
                BackendHit("a", "Dense A", 0.9, {"title": "A"}),
                BackendHit("b", "Dense B", 0.8, {"title": "B"}),
            ]
        return [
            BackendHit("b", "Keyword B", 5.0, {"title": "B"}),
            BackendHit("c", "Keyword C", 4.0, {"title": "C"}),
        ]

    async def health(self, target: str, *, timeout_seconds: float) -> bool:
        self.health_calls += 1
        return self.ready

    async def aclose(self) -> None:
        self.closed = True


class _FakeLLM:
    def __init__(self) -> None:
        self.embed_error = False
        self.rerank_error = False
        self.ready = True
        self.closed = False
        self.embedding_models = {"embed-v1"}
        self.embedding_dimensions: int | None = 2
        self.rerank_models = {"rerank-v1"}

    async def embed(
        self,
        text: str,
        *,
        model: str,
        expected_dimensions: int,
        trace_id: str,
    ) -> tuple[float, ...]:
        if self.embed_error:
            raise EmbeddingUnavailableError()
        assert model == "embed-v1"
        assert expected_dimensions == 2
        return (0.1, 0.2)

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str,
        trace_id: str,
    ) -> list[RerankScore]:
        if self.rerank_error:
            raise RerankUnavailableError()
        return [RerankScore(index=index, score=1.0 - index * 0.1) for index in range(top_n)]

    async def model_capabilities(self) -> LLMModelCapabilities | None:
        if not self.ready:
            return None
        return LLMModelCapabilities(
            embedding_models={
                model: self.embedding_dimensions for model in self.embedding_models
            },
            rerank_models=frozenset(self.rerank_models),
        )

    async def aclose(self) -> None:
        self.closed = True


def _service(
    *,
    backend: _FakeBackend | None = None,
    llm: _FakeLLM | None = None,
    retries: int = 0,
) -> tuple[RetrievalService, _FakeBackend, _FakeLLM]:
    selected_backend = backend or _FakeBackend()
    selected_llm = llm or _FakeLLM()
    service = RetrievalService(
        config=_config(),
        settings=_settings(retries=retries),
        backends={"database": selected_backend},
        llm_client=selected_llm,
    )
    return service, selected_backend, selected_llm


def test_service_rejects_pipeline_not_supported_by_backend() -> None:
    backend = _FakeBackend()
    backend.capabilities = BackendCapabilities(dense=True, keyword=False)

    with pytest.raises(ConfigurationError, match="适配器能力不匹配"):
        _service(backend=backend)


def test_hybrid_retrieval_fuses_channels_and_deduplicates() -> None:
    service, backend, _ = _service()

    result = asyncio.run(
        service.retrieve(
            RetrieveRequest(resource="knowledge", query="refund", top_k=3, trace_id="trace-1")
        )
    )

    assert result.trace_id == "trace-1"
    assert [hit.id for hit in result.hits] == ["b", "a", "c"]
    assert not result.partial
    assert len(backend.search_calls) == 2


def test_hybrid_degrades_when_optional_dense_channel_fails() -> None:
    llm = _FakeLLM()
    llm.embed_error = True
    service, backend, _ = _service(llm=llm)

    result = asyncio.run(
        service.retrieve(
            RetrieveRequest(resource="knowledge", query="refund", pipeline="hybrid", top_k=2)
        )
    )

    assert result.partial
    assert result.warnings == ["DENSE_RETRIEVAL_FAILED"]
    assert [hit.id for hit in result.hits] == ["b", "c"]
    assert all(isinstance(call, KeywordBackendQuery) for call in backend.search_calls)


def test_optional_rerank_failure_falls_back_to_database_order() -> None:
    llm = _FakeLLM()
    llm.rerank_error = True
    service, _, _ = _service(llm=llm)

    result = asyncio.run(
        service.retrieve(
            RetrieveRequest(
                resource="knowledge",
                query="refund",
                pipeline="keyword_rerank",
                top_k=2,
            )
        )
    )

    assert result.partial
    assert result.warnings == ["RERANK_FAILED"]
    assert [hit.id for hit in result.hits] == ["b", "c"]


def test_required_rerank_failure_is_returned_as_error() -> None:
    llm = _FakeLLM()
    llm.rerank_error = True
    service, _, _ = _service(llm=llm)

    with pytest.raises(RerankUnavailableError):
        asyncio.run(
            service.retrieve(
                RetrieveRequest(
                    resource="knowledge",
                    query="refund",
                    pipeline="keyword_rerank_required",
                )
            )
        )


def test_backend_transient_failure_is_retried_once() -> None:
    backend = _FakeBackend()
    backend.fail_count = 1
    service, _, _ = _service(backend=backend, retries=1)

    result = asyncio.run(
        service.retrieve(
            RetrieveRequest(resource="knowledge", query="refund", pipeline="keyword")
        )
    )

    assert result.hits
    assert len(backend.search_calls) == 2


def test_capabilities_expose_only_logical_names() -> None:
    service, _, _ = _service()

    capabilities = service.capabilities("knowledge")
    payload = capabilities.model_dump()

    assert payload["returnable_fields"] == ["source", "title"]
    assert payload["filterable_fields"] == ["enabled"]
    assert "documents" not in str(payload)
    assert "embedding" not in str(payload)


def test_readiness_reports_resource_degradation_without_physical_target() -> None:
    llm = _FakeLLM()
    llm.ready = False
    service, _, _ = _service(llm=llm)

    report, available = asyncio.run(service.readiness())

    assert available
    assert report.status == "degraded"
    assert report.resources["knowledge"].status == "degraded"
    assert "documents" not in str(report)


def test_readiness_checks_configured_model_aliases_not_only_process_health() -> None:
    llm = _FakeLLM()
    llm.embedding_models.clear()
    service, _, _ = _service(llm=llm)

    report, available = asyncio.run(service.readiness())

    assert available
    assert report.status == "degraded"
    assert report.resources["knowledge"].llm == "degraded"


def test_readiness_rejects_registered_embedding_with_wrong_dimensions() -> None:
    llm = _FakeLLM()
    llm.embedding_dimensions = 3
    service, _, _ = _service(llm=llm)

    report, available = asyncio.run(service.readiness())

    assert available
    assert report.status == "degraded"
    assert report.resources["knowledge"].llm == "degraded"


def test_readiness_degrades_when_embedding_dimension_is_not_declared() -> None:
    llm = _FakeLLM()
    llm.embedding_dimensions = None
    service, _, _ = _service(llm=llm)

    report, available = asyncio.run(service.readiness())

    assert available
    assert report.status == "degraded"
    assert report.resources["knowledge"].llm == "degraded"


def test_resource_snapshot_reload_is_atomic_and_rejects_invalid_candidate(tmp_path: Path) -> None:
    """候选 Snapshot 全量通过前不能替换服务中的旧逻辑 Resource。"""
    service, backend, _ = _service()
    snapshot_path = tmp_path / "resource-snapshot.json"
    _write_snapshot(snapshot_path, target="candidate_documents")
    service.configure_resource_snapshot(snapshot_path)

    _write_snapshot(snapshot_path, target="published_documents")
    os.utime(snapshot_path, ns=(1_000_000_000, 2_000_000_000))
    assert service.reload_resource_snapshot() is True
    asyncio.run(
        service.retrieve(
            RetrieveRequest(resource="knowledge", query="refund", pipeline="keyword", top_k=1)
        )
    )
    assert backend.search_calls[-1].target == "published_documents"

    invalid = json.loads(snapshot_path.read_text(encoding="utf-8"))
    invalid["resources"]["knowledge"]["target"] = "tampered_documents"
    snapshot_path.write_text(json.dumps(invalid), encoding="utf-8")
    os.utime(snapshot_path, ns=(1_000_000_000, 3_000_000_000))
    with pytest.raises(ConfigurationError, match="checksum"):
        service.reload_resource_snapshot()
    asyncio.run(
        service.retrieve(
            RetrieveRequest(resource="knowledge", query="refund", pipeline="keyword", top_k=1)
        )
    )
    assert backend.search_calls[-1].target == "published_documents"


def test_snapshot_identity_endpoint_reports_loaded_candidate_proof(tmp_path: Path) -> None:
    """隔离评测可读取服务已加载的 Snapshot 与 Manifest 身份，而不接触数据库写接口。"""
    service, _, _ = _service()
    snapshot_path = tmp_path / "resource-snapshot.json"
    _write_snapshot(snapshot_path, target="candidate_documents")
    service.replace_resource_snapshot(load_resource_snapshot(snapshot_path, known_connections={"database"}))
    app = create_app(service=service, settings_override=_settings())

    async def run() -> dict[str, object]:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/v1/snapshot-identity")
                assert response.status_code == 200
                return response.json()

    identity = asyncio.run(run())
    assert identity["snapshot_revision"] == "snapshot/kv_test"
    assert identity["resources"] == {
        "knowledge": {
            "resource_id": "knowledge",
            "resource_revision": "resource/kv_test",
            "resource_checksum": identity["resources"]["knowledge"]["resource_checksum"],
            "knowledge_version_id": "kv_test",
            "collection_plan_checksum": "a" * 64,
        }
    }


def test_snapshot_identity_endpoint_rejects_static_configuration() -> None:
    """未从版本化 Snapshot 装配的服务不能冒充 candidate 评测实例。"""
    service, _, _ = _service()
    app = create_app(service=service, settings_override=_settings())

    async def run() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                return await client.get("/api/v1/snapshot-identity")

    response = asyncio.run(run())
    assert response.status_code == 503
    assert response.json()["error_code"] == "SNAPSHOT_IDENTITY_UNAVAILABLE"


def test_snapshot_reload_stages_new_declared_connection_without_restart(tmp_path: Path) -> None:
    """候选资源首次使用配置中已有的 connection 时应惰性构造并原子切换。"""
    config = _config()
    database = config.connections["database"]
    config = config.model_copy(
        update={
            "connections": {
                "database": database,
                "archive": database.model_copy(update={"uri": "http://archive.test"}),
            }
        }
    )
    database_backend = _FakeBackend()
    archive_backend = _FakeBackend()
    factory_calls: list[set[str]] = []

    def factory(names: set[str]) -> dict[str, _FakeBackend]:
        factory_calls.append(set(names))
        assert names == {"archive"}
        return {"archive": archive_backend}

    service = RetrievalService(
        config=config,
        settings=_settings(),
        backends={"database": database_backend},
        llm_client=_FakeLLM(),
        backend_factory=factory,
    )
    snapshot_path = tmp_path / "resource-snapshot.json"
    _write_snapshot(snapshot_path, target="candidate_documents", connection="database")
    service.configure_resource_snapshot(snapshot_path)
    _write_snapshot(snapshot_path, target="archive_documents", connection="archive")
    os.utime(snapshot_path, ns=(1_000_000_000, 2_000_000_000))

    assert service.reload_resource_snapshot() is True
    asyncio.run(
        service.retrieve(
            RetrieveRequest(resource="knowledge", query="refund", pipeline="keyword", top_k=1)
        )
    )
    assert factory_calls == [{"archive"}]
    assert archive_backend.search_calls[-1].target == "archive_documents"


def test_snapshot_reload_keeps_old_state_when_connection_staging_fails(tmp_path: Path) -> None:
    """连接工厂未能完整提供候选适配器时，旧资源表和旧 adapter 必须继续服务。"""
    config = _config()
    database = config.connections["database"]
    config = config.model_copy(
        update={
            "connections": {
                "database": database,
                "archive": database.model_copy(update={"uri": "http://archive.test"}),
            }
        }
    )
    database_backend = _FakeBackend()
    service = RetrievalService(
        config=config,
        settings=_settings(),
        backends={"database": database_backend},
        llm_client=_FakeLLM(),
        backend_factory=lambda _names: {},
    )
    snapshot_path = tmp_path / "resource-snapshot.json"
    _write_snapshot(snapshot_path, target="candidate_documents", connection="database")
    service.configure_resource_snapshot(snapshot_path)
    _write_snapshot(snapshot_path, target="archive_documents", connection="archive")
    os.utime(snapshot_path, ns=(1_000_000_000, 2_000_000_000))

    with pytest.raises(ConfigurationError, match="完整且精确"):
        service.reload_resource_snapshot()
    asyncio.run(
        service.retrieve(
            RetrieveRequest(resource="knowledge", query="refund", pipeline="keyword", top_k=1)
        )
    )
    assert database_backend.search_calls[-1].target == "documents"


def test_snapshot_accepts_published_pipeline_null_fields(tmp_path: Path) -> None:
    """发布契约的非适用 pipeline 字段为 null 时仍可加载到严格运行时配置。"""
    snapshot_path = tmp_path / "resource-snapshot.json"
    _write_snapshot(snapshot_path, target="candidate_documents", include_null_pipeline_fields=True)

    loaded = load_resource_snapshot(snapshot_path, known_connections={"database"})

    assert loaded.resources["knowledge"].pipelines["dense"].type == "dense"
    assert loaded.resources["knowledge"].pipelines["keyword"].type == "keyword"
    assert loaded.resources["knowledge"].pipelines["hybrid"].type == "hybrid"


def _write_snapshot(
    path: Path,
    *,
    target: str,
    connection: str = "database",
    include_null_pipeline_fields: bool = False,
) -> None:
    """构造与阶段 4 Publisher 相同 checksum 规则的最小已发布 Resource Snapshot。"""
    manifest = {
        "schema_version": "muye.ai/knowledge-resource-manifest/v1",
        "resource_id": "knowledge",
        "resource_revision": "resource/kv_test",
        "knowledge_id": "kb.test",
        "knowledge_version_id": "kv_test",
        "collection_plan_checksum": "a" * 64,
        "connection": connection,
        "target": target,
        "fields": {
            "id": "document_id",
            "content": "content",
            "vector": "embedding",
            "keyword": "sparse",
            "exposed_fields": {"title": "title", "citation_id": "citation_id"},
            "filterable_fields": {"enabled": "enabled"},
        },
        "embedding_alias": "embed-v1",
        "embedding_dimensions": 2,
        "pipelines": {
            "dense": {"type": "dense", "candidate_k": 3},
            "keyword": {"type": "keyword", "candidate_k": 3},
            "hybrid": {
                "type": "hybrid",
                "dense_candidate_k": 3,
                "keyword_candidate_k": 3,
                "dense_weight": 1.0,
                "keyword_weight": 1.0,
                "rank_constant": 60,
            },
        },
        "default_pipeline": "hybrid",
        "default_return_fields": ["title", "citation_id"],
    }
    if include_null_pipeline_fields:
        for pipeline in manifest["pipelines"].values():
            pipeline.setdefault("candidate_k", None)
            pipeline.setdefault("dense_candidate_k", None)
            pipeline.setdefault("keyword_candidate_k", None)
            pipeline.setdefault("dense_weight", None)
            pipeline.setdefault("keyword_weight", None)
            pipeline.setdefault("rank_constant", None)
            pipeline.setdefault("rerank_model", None)
            pipeline.setdefault("rerank_required", False)
    manifest["resource_checksum"] = canonical_checksum(manifest)
    snapshot = {
        "schema_version": "muye.ai/resource-snapshot/v1",
        "snapshot_revision": "snapshot/kv_test",
        "resources": {"knowledge": manifest},
    }
    snapshot["snapshot_checksum"] = canonical_checksum(snapshot)
    path.write_text(json.dumps(snapshot), encoding="utf-8")


def test_http_surface_is_readonly_and_errors_are_stable() -> None:
    service, backend, _ = _service()
    app = create_app(service=service, settings_override=_settings())

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                health = await client.get("/health")
                assert health.status_code == 200
                assert backend.health_calls == 0

                readiness = await client.get("/ready")
                assert readiness.status_code == 200
                assert readiness.json()["resources"]["knowledge"]["status"] == "ready"

                response = await client.post(
                    "/api/v1/retrieve",
                    json={
                        "resource": "knowledge",
                        "query": "refund",
                        "pipeline": "keyword",
                        "trace_id": "api-trace",
                    },
                )
                assert response.status_code == 200
                assert response.headers["X-Trace-Id"] == "api-trace"

                capability_response = await client.get(
                    "/api/v1/resources/knowledge/capabilities",
                    headers={"X-Trace-Id": "invalid trace"},
                )
                generated_trace = capability_response.headers["X-Trace-Id"]
                assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", generated_trace)
                assert generated_trace != "invalid trace"

                validation = await client.post(
                    "/api/v1/retrieve",
                    json={"resource": "knowledge", "query": "", "trace_id": "bad-trace"},
                )
                assert validation.status_code == 422
                assert validation.json() == {
                    "error_code": "VALIDATION_ERROR",
                    "message": "请求参数校验失败",
                    "recoverable": False,
                    "trace_id": "bad-trace",
                }

                assert (await client.post("/api/v1/search", json={})).status_code == 404
                assert (await client.post("/api/v1/resources", json={})).status_code == 404

    asyncio.run(run())


def test_http_rejects_unknown_return_and_filter_fields() -> None:
    service, _, _ = _service()
    app = create_app(service=service, settings_override=_settings())

    async def run() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                return await client.post(
                    "/api/v1/retrieve",
                    json={
                        "resource": "knowledge",
                        "query": "refund",
                        "pipeline": "keyword",
                        "return_fields": ["secret"],
                    },
                )

    response = asyncio.run(run())
    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_REQUEST"
    assert "secret" not in response.text


def test_openapi_declares_stable_error_and_readiness_contracts() -> None:
    service, _, _ = _service()
    app = create_app(service=service, settings_override=_settings())

    schema = app.openapi()
    retrieve_responses = schema["paths"]["/api/v1/retrieve"]["post"]["responses"]

    for status_code in ("400", "404", "422", "502", "503", "504"):
        response_schema = retrieve_responses[status_code]["content"]["application/json"]["schema"]
        assert response_schema["$ref"].endswith("/ErrorResponse")
    ready_responses = schema["paths"]["/ready"]["get"]["responses"]
    assert ready_responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ReadinessResponse"
    )
    assert ready_responses["503"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ReadinessResponse"
    )
