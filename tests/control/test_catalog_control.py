"""Control candidate、状态阈值、grant 与 citation 授权测试。"""

from __future__ import annotations

import asyncio

from contracts.catalog import build_catalog_snapshot
from contracts.models import AgentCatalogEntryV1, ResourceBindingV1
from control_server.catalog import CatalogProjection
from control_server.health import AgentHealthCollector, CatalogCandidateError
from control_server.main import _agent_token
from control_server.models import AgentObservationRequest, CatalogCandidateRequest, CitationRecordRequest
from control_server.api import create_app
import httpx


def _entry(*, status: str = "STARTING") -> AgentCatalogEntryV1:
    return AgentCatalogEntryV1(
        agent_id="agent_product_handbook",
        agent_version="1.0.0",
        tool_name="product_help",
        display_name="产品手册",
        description="查询产品手册。",
        supported_intents=["产品咨询"],
        service_name="agent-product-handbook",
        base_url="http://agent-product-handbook:8000",
        timeout_seconds=30,
        internal_protocol_version="muye-agent-internal/3.0",
        api_profile="internal",
        descriptor_checksum="a" * 64,
        source_tree_checksum="b" * 64,
        image_digest=f"sha256:{'c' * 64}",
        resource_bindings=[ResourceBindingV1(resource_id="kb.product", skill_ref="skill_product@1")],
        capabilities_checksum="d" * 64,
        status=status,
    )


class _Grants:
    def __init__(self) -> None:
        self.values = {"u1": frozenset({"agent_product_handbook"})}

    def allowed_agent_ids(self, user_id: str) -> frozenset[str]:
        return self.values.get(user_id, frozenset())


class _Health:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.machine = AgentHealthCollector(
            token_provider=lambda _agent_id: "token",
            failure_threshold=2,
            success_threshold=2,
        )

    async def validate_candidate(self, candidate):
        if self.reject:
            raise CatalogCandidateError("probe failed")
        return build_catalog_snapshot([entry.model_copy(update={"status": "ACTIVE"}) for entry in candidate.agents])

    def observe(self, **kwargs):
        return self.machine.observe(**kwargs)

    async def probe(self, entry):
        if self.reject:
            raise CatalogCandidateError("probe failed")


def _projection(*, reject: bool = False) -> tuple[CatalogProjection, _Grants]:
    grants = _Grants()
    return CatalogProjection(health_collector=_Health(reject=reject), grant_store=grants), grants


def _submit_and_ack(projection: CatalogProjection, *, key: str):
    response = asyncio.run(
        projection.submit_candidate(
            CatalogCandidateRequest(
                idempotency_key=key,
                expected_active_checksum=projection.active.catalog_checksum,
                snapshot=build_catalog_snapshot([_entry()]),
            )
        )
    )
    projection.record_ack(
        revision=response.catalog_revision,
        checksum=response.catalog_checksum,
        accepted=True,
    )
    return response


def test_candidate_idempotency_key_is_bound_to_request_content() -> None:
    projection, _ = _projection()
    candidate = build_catalog_snapshot([_entry()])
    request = CatalogCandidateRequest(
        idempotency_key="deploy:test-1",
        expected_active_checksum=projection.active.catalog_checksum,
        snapshot=candidate,
    )

    first = asyncio.run(projection.submit_candidate(request))
    second = asyncio.run(projection.submit_candidate(request))

    assert first == second
    assert projection.active.agents == []
    projection.record_ack(
        revision=first.catalog_revision,
        checksum=first.catalog_checksum,
        accepted=True,
    )
    different = CatalogCandidateRequest(
        idempotency_key=request.idempotency_key,
        expected_active_checksum=first.catalog_checksum,
        snapshot=build_catalog_snapshot([]),
    )
    try:
        asyncio.run(projection.submit_candidate(different))
    except ValueError as exc:
        assert "不同 Catalog candidate" in str(exc)
    else:
        raise AssertionError("相同幂等键不能提交不同 candidate")


def test_rejected_candidate_keeps_previous_active_catalog() -> None:
    projection, _ = _projection(reject=True)
    previous = projection.active
    request = CatalogCandidateRequest(
        idempotency_key="deploy:test-2",
        expected_active_checksum=previous.catalog_checksum,
        snapshot=build_catalog_snapshot([_entry()]),
    )

    try:
        asyncio.run(projection.submit_candidate(request))
    except CatalogCandidateError:
        pass
    else:
        raise AssertionError("失败健康探测必须拒绝 candidate")

    assert projection.active == previous


def test_main_rejected_pending_candidate_keeps_previous_active_catalog() -> None:
    projection, _ = _projection()
    previous = projection.active
    response = asyncio.run(
        projection.submit_candidate(
            CatalogCandidateRequest(
                idempotency_key="deploy:test-reject",
                expected_active_checksum=previous.catalog_checksum,
                snapshot=build_catalog_snapshot([_entry()]),
            )
        )
    )

    assert projection.catalog_for_main.catalog_checksum == response.catalog_checksum
    assert projection.active == previous
    projection.record_ack(
        revision=response.catalog_revision,
        checksum=response.catalog_checksum,
        accepted=False,
    )
    assert projection.catalog_for_main == previous
    assert projection.active == previous


def test_health_threshold_transitions_active_and_degraded() -> None:
    projection, _ = _projection()
    active = _submit_and_ack(projection, key="deploy:test-3")
    request = lambda checksum, healthy: AgentObservationRequest(
        catalog_checksum=checksum,
        agent_id="agent_product_handbook",
        agent_version="1.0.0",
        healthy=healthy,
        error_code=None if healthy else "DEPENDENCY_UNAVAILABLE",
    )

    first_failure = asyncio.run(projection.record_observation(request(active.catalog_checksum, False)))
    degraded = asyncio.run(projection.record_observation(request(first_failure.catalog_checksum, False)))
    first_success = asyncio.run(projection.record_observation(request(degraded.catalog_checksum, True)))
    recovered = asyncio.run(projection.record_observation(request(first_success.catalog_checksum, True)))

    assert (first_failure.status, first_failure.changed) == ("ACTIVE", False)
    assert (degraded.status, degraded.changed) == ("DEGRADED", True)
    assert (first_success.status, first_success.changed) == ("DEGRADED", False)
    assert (recovered.status, recovered.changed) == ("ACTIVE", True)


def test_citation_is_bound_to_user_agent_version_and_current_grant() -> None:
    projection, grants = _projection()
    response = _submit_and_ack(projection, key="deploy:test-4")
    projection.record_citation(
        CitationRecordRequest(
            citation_id="citation_0123456789abcdef",
            user_id="u1",
            agent_id="agent_product_handbook",
            agent_version="1.0.0",
            knowledge_version_id="kv_0123456789abcdef",
            catalog_revision=response.catalog_revision,
            catalog_checksum=response.catalog_checksum,
            locator={"source_path": "docs/handbook.md", "kind": "line", "start": 3, "end": 5},
        )
    )

    assert projection.resolve_citation(citation_id="citation_0123456789abcdef", user_id="u1").agent_id == (
        "agent_product_handbook"
    )
    grants.values["u1"] = frozenset()
    try:
        projection.resolve_citation(citation_id="citation_0123456789abcdef", user_id="u1")
    except ValueError as exc:
        assert "不可访问" in str(exc)
    else:
        raise AssertionError("撤销 grant 后 citation 必须立即失效")


def test_control_internal_routes_require_distinct_service_identities() -> None:
    projection, _ = _projection()
    app = create_app(
        projection=projection,
        operator_token="operator-secret",
        main_token="main-secret",
        health_token="health-secret",
    )

    async def run() -> list[int]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://control.test",
        ) as client:
            responses = [
                await client.get(
                    "/internal/v1/deployment/catalog/active",
                    headers={"Authorization": "Bearer main-secret"},
                ),
                await client.get(
                    "/internal/v1/catalog/active",
                    headers={"Authorization": "Bearer operator-secret"},
                ),
                await client.post(
                    "/internal/v1/agent-observations",
                    headers={"Authorization": "Bearer operator-secret"},
                    json={},
                ),
                await client.get(
                    "/internal/v1/deployment/catalog/active",
                    headers={"Authorization": "Bearer operator-secret"},
                ),
            ]
        return [response.status_code for response in responses]

    assert asyncio.run(run()) == [401, 401, 401, 200]


def test_control_rejects_reused_service_tokens() -> None:
    projection, _ = _projection()

    try:
        create_app(
            projection=projection,
            operator_token="shared-secret",
            main_token="shared-secret",
            health_token="health-secret",
        )
    except ValueError as exc:
        assert "互不相同" in str(exc)
    else:
        raise AssertionError("Control 必须拒绝复用的服务身份 token")


def test_health_collector_rejects_missing_token_before_network() -> None:
    constructed = False

    def client_factory(**kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("缺少 token 时不能构造 HTTP client")

    collector = AgentHealthCollector(
        token_provider=lambda _agent_id: None,  # type: ignore[return-value]
        client_factory=client_factory,
    )
    try:
        asyncio.run(collector.probe(_entry()))
    except CatalogCandidateError as exc:
        assert "服务凭据" in str(exc)
    else:
        raise AssertionError("缺少目标 token 必须拒绝探测")
    assert constructed is False


def test_control_rejects_reused_target_agent_tokens(monkeypatch) -> None:
    monkeypatch.setenv(
        "MUYE_CONTROL_AGENT_TOKENS_JSON",
        (
            '{"agent_product_handbook":"shared-token",'
            '"agent_finance_handbook":"shared-token"}'
        ),
    )

    try:
        _agent_token("agent_product_handbook")
    except ValueError as exc:
        assert "不同" in str(exc)
    else:
        raise AssertionError("Control 必须拒绝跨 Agent 复用的目标 token")


def test_health_collector_rejects_capabilities_identity_mismatch() -> None:
    entry = _entry()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def get(self, url, headers=None):
            request = httpx.Request("GET", url)
            if url.endswith("/health"):
                return httpx.Response(200, json={"status": "healthy"}, request=request)
            if url.endswith("/ready"):
                return httpx.Response(200, json={"status": "ready"}, request=request)
            return httpx.Response(
                200,
                json={
                    "agent_name": "product-handbook",
                    "version": "9.9.9",
                    "api_profiles": ["internal"],
                    "identity": {
                        "agent_id": entry.agent_id,
                        "agent_version": entry.agent_version,
                        "descriptor_checksum": entry.descriptor_checksum,
                        "source_tree_checksum": entry.source_tree_checksum,
                    },
                    "internal_protocol_version": entry.internal_protocol_version,
                    "supports_streaming": True,
                },
                request=request,
            )

    collector = AgentHealthCollector(
        token_provider=lambda _agent_id: "control-token",
        client_factory=lambda **_kwargs: _Client(),
    )

    try:
        asyncio.run(collector.probe(entry))
    except CatalogCandidateError as exc:
        assert "版本不匹配" in str(exc)
    else:
        raise AssertionError("capabilities identity 漂移必须拒绝 candidate")


def test_active_health_collection_uses_probe_results_and_thresholds() -> None:
    health = _Health()
    projection = CatalogProjection(health_collector=health, grant_store=_Grants())
    _submit_and_ack(projection, key="deploy:health-loop")
    health.reject = True

    first = asyncio.run(projection.collect_health_once())
    second = asyncio.run(projection.collect_health_once())

    assert first[0].status == "ACTIVE"
    assert second[0].status == "DEGRADED"
    health.reject = False
    third = asyncio.run(projection.collect_health_once())
    fourth = asyncio.run(projection.collect_health_once())
    assert third[0].status == "DEGRADED"
    assert fourth[0].status == "ACTIVE"
