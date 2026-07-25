"""Travel 参考服务的无网络协议测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agents" / "agent-travel"))

from main import build_config
from muye_multi_agent_sdk import AgentConfig, ApiConfig, ReActAgent, create_app
from travel_agent import TravelAgent, sample_itinerary


def test_travel_agent_is_react_with_deterministic_tool() -> None:
    """Travel 示例使用 ReAct 基类，领域工具本身不依赖模型或网络。"""
    agent = TravelAgent(
        AgentConfig(
            api=ApiConfig(profiles={"internal", "public"}, public_path="/api/v1/travel"),
        )
    )
    result = sample_itinerary.invoke({"city": "成都", "days": 2})

    assert isinstance(agent, ReActAgent)
    assert result["json_data"]["days"] == 2
    assert "public" in agent.config.api.profiles
    assert "目的地" in agent.intent_guard_business_rules


def test_travel_service_defaults_to_documented_profiles(monkeypatch) -> None:
    """未显式配置时应同时提供 internal 与固定路径的 public profile。"""
    monkeypatch.delenv("MUYE_SDK_API_PROFILES", raising=False)
    monkeypatch.delenv("MUYE_SDK_PUBLIC_PATH", raising=False)

    config = build_config(env_file=None)
    app = create_app(TravelAgent(config))
    paths = {route.path for route in app.routes}

    assert config.api.profiles == {"internal", "public"}
    assert config.api.public_path == "/api/v1/travel"
    assert "/invoke" in paths
    assert "/api/v1/travel/invoke" in paths
    assert "/api/v1/travel/invoke/stream" in paths


def test_travel_service_respects_explicit_profile_override(monkeypatch) -> None:
    """部署环境应能将 Travel 收口为仅 internal 服务。"""
    monkeypatch.setenv("MUYE_SDK_API_PROFILES", "internal")

    config = build_config(env_file=None)
    app = create_app(TravelAgent(config))
    paths = {route.path for route in app.routes}

    assert config.api.profiles == {"internal"}
    assert "/invoke" in paths
    assert "/api/v1/travel/invoke" not in paths


def test_travel_capabilities_match_sdk_contract() -> None:
    """Travel 能力声明应由 SDK transport 生成，并包含 internal v3。"""
    config = AgentConfig(
        api=ApiConfig(profiles={"internal", "public"}, public_path="/api/v1/travel")
    )

    async def get_capabilities() -> httpx.Response:
        app = create_app(TravelAgent(config))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/capabilities")

    response = asyncio.run(get_capabilities())

    assert response.status_code == 200
    assert response.json()["internal_protocol_version"] == "muye-agent-internal/3.0"
    assert response.json()["api_profiles"] == ["internal", "public"]
