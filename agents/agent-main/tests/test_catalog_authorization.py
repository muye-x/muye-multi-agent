"""MainAgent 请求级 Catalog、服务身份和引用回写安全测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from contracts.catalog import build_catalog_snapshot
from contracts.local_dev import LocalDevAgentV1, build_local_dev_registration
from contracts.models import AgentCatalogEntryV1, ResourceBindingV1
from config.settings import CatalogConfig
from tools.sub_agent.catalog import (
    AuthorizationResolution,
    CatalogProvider,
    CatalogUnavailableError,
    CitationEvidence,
    LocalDevCatalogProvider,
)
from tools.sub_agent.registry import SubAgentDescriptor, SubAgentRegistry
from tools.sub_agent.runtime import SubAgentRuntimeError, SubAgentRuntimeGuard
from tools.sub_agent.tools import build_sub_agent_tools


def _descriptor(agent_id: str = "agent_product_handbook") -> SubAgentDescriptor:
    return SubAgentDescriptor(
        name="product_help",
        url="http://agent-product-handbook:8000",
        timeout_seconds=10,
        agent_id=agent_id,
        agent_version="1.0.0",
        display_name="产品手册",
        description="查询产品手册。",
        supported_intents=("产品咨询",),
        descriptor_checksum="a" * 64,
        source_tree_checksum="b" * 64,
        catalog_revision="catalog-test",
        catalog_checksum="c" * 64,
    )


class _Caller:
    def __init__(self, event: dict | None = None) -> None:
        self.called = False
        self.event = event or {"event": "block", "data": {"content": "ok"}}

    async def stream(self, *args, **kwargs):
        self.called = True
        yield self.event

    async def cancel(self, *args, **kwargs):
        return {"status": "cancelled"}


def _config(descriptor: SubAgentDescriptor) -> dict[str, object]:
    return {
        "configurable": {
            "user_id": "u1",
            "session_id": "s1",
            "trace_id": "t1",
            "catalog_revision": descriptor.catalog_revision,
            "allowed_agent_ids": [descriptor.agent_id],
        }
    }


def test_missing_target_token_fails_before_network_call() -> None:
    descriptor = _descriptor()
    caller = _Caller()
    tool = build_sub_agent_tools(
        SubAgentRegistry([descriptor]),
        caller=caller,
        token_provider=lambda _agent_id: None,
    )[0]

    result = asyncio.run(tool.ainvoke({"task": "查询"}, config=_config(descriptor)))

    assert "AUTHENTICATION_ERROR" in result
    assert caller.called is False


def test_main_rejects_reused_target_agent_tokens() -> None:
    config = CatalogConfig(
        agent_tokens_json=(
            '{"agent_product_handbook":"shared-token",'
            '"agent_finance_handbook":"shared-token"}'
        )
    )

    try:
        config.agent_token("agent_product_handbook")
    except ValueError as exc:
        assert "不同" in str(exc)
    else:
        raise AssertionError("Main 必须拒绝跨 Agent 复用的目标 token")


def test_authorization_and_catalog_revision_are_rechecked_before_call() -> None:
    descriptor = _descriptor()
    caller = _Caller()
    tool = build_sub_agent_tools(
        SubAgentRegistry([descriptor]),
        caller=caller,
        token_provider=lambda _agent_id: "target-token",
    )[0]
    config = _config(descriptor)
    config["configurable"]["allowed_agent_ids"] = []  # type: ignore[index]

    result = asyncio.run(tool.ainvoke({"task": "查询"}, config=config))

    assert "AUTHORIZATION_ERROR" in result
    assert caller.called is False


def test_trusted_citation_is_recorded_and_removed_from_model_result() -> None:
    descriptor = _descriptor()
    event = {
        "event": "block",
        "data": {
            "content": {
                "data": {
                    "result_data": {
                        "markdown": "answer",
                        "_muye_citations": [
                            {
                                "citation_id": "citation_0123456789abcdef",
                                "knowledge_version_id": "kv_0123456789abcdef",
                                "locator": {
                                    "source_path": "docs/handbook.md",
                                    "kind": "line",
                                    "start": 1,
                                    "end": 2,
                                },
                                "title": "手册",
                                "source": "file_0123456789abcdef",
                            }
                        ],
                    }
                }
            }
        },
    }
    recorded: list[tuple[str, str, CitationEvidence]] = []

    async def recorder(target, user_id, evidence):
        recorded.append((target.agent_id, user_id, evidence))

    tool = build_sub_agent_tools(
        SubAgentRegistry([descriptor]),
        caller=_Caller(event),
        token_provider=lambda _agent_id: "target-token",
        citation_recorder=recorder,
    )[0]

    result = asyncio.run(tool.ainvoke({"task": "查询"}, config=_config(descriptor)))

    assert recorded[0][0:2] == (descriptor.agent_id, "u1")
    assert recorded[0][2].knowledge_version_id == "kv_0123456789abcdef"
    assert "_muye_citations" not in result


def test_circuit_breaker_is_isolated_by_agent_id() -> None:
    guard = SubAgentRuntimeGuard(failure_threshold=1, recovery_seconds=60)
    guard.failed("agent_failed")

    async def run() -> None:
        try:
            await guard.acquire("agent_failed", 1)
        except SubAgentRuntimeError:
            pass
        else:
            raise AssertionError("失败 Agent 应处于熔断状态")
        semaphore = await guard.acquire("agent_healthy", 1)
        semaphore.release()

    asyncio.run(run())


def test_sub_agent_runtime_guard_queues_and_limits_each_request() -> None:
    """同请求重复调用被拒绝，请求结束后预算被释放且名额可供其他请求排队获得。"""
    guard = SubAgentRuntimeGuard(queue_wait_seconds=0.2)

    async def run() -> None:
        first = await guard.acquire("agent_product", 1, request_id="request-1")
        waiter = asyncio.create_task(guard.acquire("agent_product", 1, request_id="request-2"))
        await asyncio.sleep(0)
        first.release()
        second = await waiter
        second.release()
        try:
            await guard.acquire("agent_product", 1, request_id="request-1")
        except SubAgentRuntimeError as exc:
            assert exc.code == "REQUEST_LIMIT_REACHED"
        else:
            raise AssertionError("同一请求重复调用子 Agent 必须被拒绝")
        guard.finish_request("request-1")
        reused = await guard.acquire("agent_product", 1, request_id="request-1")
        reused.release()

    asyncio.run(run())


def test_empty_sub_agent_result_is_reported_as_dependency_failure() -> None:
    """空 SSE 不能伪装为成功，避免模型继续放大检索请求。"""
    descriptor = _descriptor()
    tool = build_sub_agent_tools(
        SubAgentRegistry([descriptor]),
        caller=_Caller({"event": "done", "data": {}}),
        token_provider=lambda _agent_id: "target-token",
    )[0]

    result = asyncio.run(tool.ainvoke({"task": "查询"}, config=_config(descriptor)))

    assert result == "[DEPENDENCY_UNAVAILABLE] 子 Agent 未返回可展示结果"


def _active_snapshot():
    return build_catalog_snapshot(
        [
            AgentCatalogEntryV1(
                agent_id="agent_product_handbook",
                agent_version="1.0.0",
                tool_name="product_help",
                display_name="产品手册",
                description="查询产品手册。",
                supported_intents=["产品咨询"],
                service_name="agent-product-handbook",
                base_url="http://agent-product-handbook:8000",
                timeout_seconds=10,
                internal_protocol_version="muye-agent-internal/3.0",
                api_profile="internal",
                descriptor_checksum="a" * 64,
                source_tree_checksum="b" * 64,
                image_digest=f"sha256:{'c' * 64}",
                resource_bindings=[ResourceBindingV1(resource_id="kb.product", skill_ref="skill_product@1")],
                capabilities_checksum="d" * 64,
                status="ACTIVE",
            )
        ]
    )


class _CatalogClient:
    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.etags: list[str | None] = []
        self.acks: list[tuple[str, bool]] = []

    async def fetch_catalog(self, etag):
        self.etags.append(etag)
        return self.candidates.pop(0)

    async def ack(self, snapshot, *, accepted, reason=None):
        self.acks.append((snapshot.catalog_checksum, accepted))


def test_catalog_provider_uses_etag_and_keeps_registry_on_rejected_candidate() -> None:
    active = _active_snapshot()
    invalid = active.model_copy(update={"catalog_checksum": "0" * 64})
    client = _CatalogClient([active, None, invalid])
    provider = CatalogProvider(client)  # type: ignore[arg-type]

    assert asyncio.run(provider.refresh()) is True
    assert provider.registry.get_by_agent_id("agent_product_handbook").name == "product_help"
    assert asyncio.run(provider.refresh()) is False
    try:
        asyncio.run(provider.refresh())
    except CatalogUnavailableError:
        pass
    else:
        raise AssertionError("无效 candidate 必须被 Main 拒绝")

    assert client.etags == [build_catalog_snapshot([]).catalog_checksum, active.catalog_checksum, active.catalog_checksum]
    assert client.acks == [(active.catalog_checksum, True), (invalid.catalog_checksum, False)]
    assert provider.snapshot == active
    assert provider.registry.get_by_agent_id("agent_product_handbook").name == "product_help"


def test_two_users_only_receive_their_authorized_agent_tools() -> None:
    product = _active_snapshot().agents[0]
    finance = product.model_copy(
        update={
            "agent_id": "agent_finance_handbook",
            "tool_name": "finance_help",
            "display_name": "财务手册",
            "description": "查询财务手册。",
            "supported_intents": ["财务咨询"],
            "service_name": "agent-finance-handbook",
            "base_url": "http://agent-finance-handbook:8000",
            "descriptor_checksum": "e" * 64,
            "source_tree_checksum": "f" * 64,
            "image_digest": f"sha256:{'1' * 64}",
            "capabilities_checksum": "2" * 64,
        }
    )
    active = build_catalog_snapshot([product, finance])

    class _AuthorizationClient:
        def __init__(self) -> None:
            self.first_fetch = True

        async def fetch_catalog(self, _etag):
            if self.first_fetch:
                self.first_fetch = False
                return active
            return None

        async def ack(self, _snapshot, *, accepted, reason=None):
            assert accepted is True

        async def resolve_authorization(self, user_id):
            allowed = {
                "product-user": {product.agent_id},
                "finance-user": {finance.agent_id},
            }[user_id]
            return AuthorizationResolution(
                user_id=user_id,
                catalog_revision=active.catalog_revision,
                catalog_checksum=active.catalog_checksum,
                allowed_agent_ids=frozenset(allowed),
            )

    provider = CatalogProvider(_AuthorizationClient())  # type: ignore[arg-type]
    assert asyncio.run(provider.refresh()) is True

    product_view = asyncio.run(provider.authorized_view("product-user"))
    finance_view = asyncio.run(provider.authorized_view("finance-user"))
    product_tools = build_sub_agent_tools(product_view.registry)
    finance_tools = build_sub_agent_tools(finance_view.registry)

    assert [(tool.name, tool.description) for tool in product_tools] == [
        ("product_help", "查询产品手册。 支持的意图：产品咨询。")
    ]
    assert [(tool.name, tool.description) for tool in finance_tools] == [
        ("finance_help", "查询财务手册。 支持的意图：财务咨询。")
    ]


def _local_registration_path(tmp_path: Path) -> Path:
    registration = build_local_dev_registration(
        user_id="local-dev-user",
        agent=LocalDevAgentV1(
            agent_id="agent_local_handbook",
            slug="local-handbook",
            agent_version="1.0.0",
            tool_name="local_help",
            display_name="本地手册",
            description="仅供本地开发联调的手册。",
            supported_intents=["本地咨询"],
            service_name="agent-local-handbook",
            base_url="http://127.0.0.1:8001",
            timeout_seconds=10,
            internal_protocol_version="muye-agent-internal/3.0",
            descriptor_checksum="a" * 64,
            source_tree_checksum="b" * 64,
            resource_bindings=[ResourceBindingV1(resource_id="kb.local", skill_ref="skill_local@1")],
        ),
    )
    path = tmp_path / "registration.json"
    path.write_text(registration.model_dump_json(), encoding="utf-8")
    return path


def test_local_dev_provider_exposes_only_registered_agent_to_local_user(tmp_path: Path) -> None:
    provider = LocalDevCatalogProvider(
        registration_path=_local_registration_path(tmp_path),
        user_id="local-dev-user",
    )

    local_view = asyncio.run(provider.authorized_view("local-dev-user"))
    other_view = asyncio.run(provider.authorized_view("other-user"))

    assert local_view.catalog_revision.startswith("local-dev-")
    assert [item.agent_id for item in local_view.registry.values()] == ["agent_local_handbook"]
    assert other_view.allowed_agent_ids == frozenset()
    assert other_view.registry.values() == ()


def test_local_dev_provider_fails_closed_for_tampered_registration(tmp_path: Path) -> None:
    path = _local_registration_path(tmp_path)
    path.write_text("{}", encoding="utf-8")
    provider = LocalDevCatalogProvider(registration_path=path, user_id="local-dev-user")

    try:
        asyncio.run(provider.authorized_view("local-dev-user"))
    except CatalogUnavailableError:
        pass
    else:
        raise AssertionError("损坏的 local-dev 注册文件必须拒绝授权")
