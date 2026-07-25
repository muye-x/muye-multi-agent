"""Order Graph 参考服务的无网络协议测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agents" / "agent-order"))

from muye_multi_agent_sdk import AgentConfig, AgentContext, AgentRequest, ApiConfig, create_app
from order_agent import OrderGraphAgent


def test_order_graph_is_internal_only_and_normalizes_task() -> None:
    """Order 示例应以确定性 Graph 完成内部任务，不暴露 public profile。"""
    agent = OrderGraphAgent(
        AgentConfig(
            api=ApiConfig(profiles={"internal"}),
        )
    )
    request = AgentRequest(
        task="  演示订单流程  ",
        context=AgentContext(user_id="u1", session_id="s1"),
    )

    async def invoke() -> object:
        return await agent.invoke(request)

    response = asyncio.run(invoke())

    assert response.status == "success"
    assert response.result_data is not None
    assert response.result_data["json_data"]["task"] == "演示订单流程"
    assert "public" not in agent.config.api.profiles


def test_order_http_and_sse_follow_sdk_contract() -> None:
    """Order 的 internal HTTP 与 SSE 应由新版 SDK transport 完整提供。"""
    agent = OrderGraphAgent(AgentConfig(api=ApiConfig(profiles={"internal"})))
    app = create_app(agent)
    payload = {
        "task": "  演示订单流程  ",
        "context": {"user_id": "u1", "session_id": "s1"},
    }

    async def invoke() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/invoke", json=payload)
            stream_response = await client.post("/invoke/stream", json=payload)
            return response, stream_response

    response, stream_response = asyncio.run(invoke())

    assert response.status_code == 200
    assert response.json()["payload"]["result_data"]["json_data"]["task"] == "演示订单流程"
    assert stream_response.status_code == 200
    assert stream_response.text.count("event: session_start") == 1
    assert stream_response.text.count("event: done") == 1
    assert stream_response.text.count("event: session_end") == 1
