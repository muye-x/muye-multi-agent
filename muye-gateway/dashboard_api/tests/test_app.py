"""dashboard-api 对受控服务状态的回归测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest

from dashboard_api.app import ServiceDefinition, _service_definitions, create_app

def _get(app: object, path: str) -> httpx.Response:
    """通过 ASGI transport 发起无网络请求，避免同步 TestClient 的版本耦合。"""

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_services_exposes_health_and_capability_profiles() -> None:
    """SDK Agent 的真实 profile 应覆盖静态展示默认值。"""

    async def fetch_json(url: str, timeout_seconds: float) -> Mapping[str, object]:
        assert timeout_seconds == 3.0
        if url.endswith("/health"):
            return {"status": "ok"}
        return {"api_profiles": ["internal", "public"]}

    app = create_app(
        (
            ServiceDefinition(
                "product-handbook",
                "Product handbook",
                "agent",
                "http://product.test",
                supports_capabilities=True,
                default_profiles=("internal",),
            ),
        ),
        fetch_json,
    )

    response = _get(app, "/services")

    assert response.status_code == 200
    service = response.json()["services"][0]
    assert service["online"] is True
    assert service["profiles"] == ["internal", "public"]
    assert service["capability_available"] is True


def test_services_marks_unreachable_service_offline() -> None:
    """下游异常应作为离线状态返回，不能导致整个控制台接口失败。"""

    async def fetch_json(url: str, timeout_seconds: float) -> Mapping[str, object]:
        raise RuntimeError("connection refused")

    app = create_app(
        (ServiceDefinition("llm", "LLM", "llm", "http://llm.test"),),
        fetch_json,
    )

    service = _get(app, "/services").json()["services"][0]
    assert service["online"] is False
    assert service["message"] == "健康检查不可用"


def test_data_service_is_listed_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUYE_DATA_ENABLED", raising=False)
    assert "muye-data" not in {item.service_id for item in _service_definitions()}

    monkeypatch.setenv("MUYE_DATA_ENABLED", "true")
    definitions = _service_definitions()

    data_service = next(item for item in definitions if item.service_id == "muye-data")
    assert data_service.kind == "data"
    assert data_service.default_profiles == ("internal", "read-only")
