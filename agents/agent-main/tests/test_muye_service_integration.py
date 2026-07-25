"""主 Agent 到 Muye LLM 与子 Agent 的协议集成测试。"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(ROOT / "agents" / "agent-main"),
    str(ROOT / "agents" / "agent-travel"),
    str(ROOT / "agents" / "agent-order"),
]

from muye_multi_agent_sdk.integrations.muye_llm import MuyeLlmChatModel
from muye_multi_agent_sdk.runtime import ExecutionOptions
import config as main_config
import core.orchestrator as orchestrator_module
from core.prompts import get_system_prompt
from core.orchestrator import AgentManager
from api.stream_protocol import BlockType, EventNormalizer
from tools.sub_agent.caller import SubAgentCallError, SubAgentCaller
from tools.sub_agent.registry import SubAgentDescriptor, SubAgentRegistry
from tools.sub_agent.tools import _forward_child_event, build_sub_agent_tools
from utils.url_policy import UnsafeUrlError, validate_external_url


def _sse_envelope(frame: str) -> dict[str, object]:
    """提取 Block Stream V2 单帧中的 JSON 信封。"""
    return json.loads(frame.split("data: ", 1)[1])


class FakeStreamGraph:
    """以可控事件、异常或挂起状态替代 LangGraph 流运行时。"""

    def __init__(
        self,
        events: list[dict[str, object]] | None = None,
        *,
        error: Exception | None = None,
        block_forever: bool = False,
    ) -> None:
        self.events = events or []
        self.error = error
        self.block_forever = block_forever

    async def astream_events(self, *_args: object, **_kwargs: object):
        if self.block_forever:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        for event in self.events:
            yield event


def _stream_manager(monkeypatch: pytest.MonkeyPatch, graph: FakeStreamGraph) -> AgentManager:
    config = SimpleNamespace(
        profiling=SimpleNamespace(enabled=False, slow_threshold_ms=1000),
        llm=SimpleNamespace(model="test-model"),
    )
    monkeypatch.setattr(main_config, "get_config", lambda: config)
    manager = AgentManager()
    manager.initialized = True
    manager.agent = SimpleNamespace(agent=graph)  # type: ignore[assignment]
    return manager


async def _collect_main_stream(manager: AgentManager, session_id: str = "s1") -> list[dict[str, object]]:
    frames = [
        frame
        async for frame in manager.chat_stream(
            user_input="你好",
            user_id="u1",
            session_id=session_id,
        )
    ]
    return [_sse_envelope(frame) for frame in frames]


def test_main_block_stream_v2_has_stable_stream_and_single_terminal_sequence() -> None:
    """同一正文 block 的多次 delta 只能计为一个 block，并维持同一 stream ID。"""
    normalizer = EventNormalizer(session_id="s1", stream_id="stream-1", user_id="u1")
    frames = [
        normalizer.session_start(model="test-model").to_sse(),
        normalizer.block_delta("b1", BlockType.MARKDOWN, "你").to_sse(),
        normalizer.block_delta("b1", BlockType.MARKDOWN, "好").to_sse(),
        normalizer.done().to_sse(),
        normalizer.session_end().to_sse(),
    ]
    envelopes = [_sse_envelope(frame) for frame in frames]

    assert [item["event"] for item in envelopes] == ["session_start", "block", "block", "done", "session_end"]
    assert {item["streamId"] for item in envelopes} == {"stream-1"}
    assert [item["seq"] for item in envelopes] == [1, 2, 3, 4, 5]
    assert envelopes[-2]["data"]["totalBlocks"] == 1
    assert envelopes[-1]["data"]["totalBlocks"] == 1


def test_main_chat_stream_preserves_envelope_and_releases_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeStreamGraph(
        [
            {"event": "on_chat_model_stream", "data": {"chunk": SimpleNamespace(content="你")}},
            {"event": "on_chat_model_stream", "data": {"chunk": SimpleNamespace(content="好")}},
        ]
    )
    manager = _stream_manager(monkeypatch, graph)

    envelopes = asyncio.run(_collect_main_stream(manager))

    assert [item["event"] for item in envelopes] == ["session_start", "block", "block", "done", "session_end"]
    assert len({item["streamId"] for item in envelopes}) == 1
    assert [item["seq"] for item in envelopes] == [1, 2, 3, 4, 5]
    assert envelopes[-2]["data"]["totalBlocks"] == 1
    assert manager.execution_manager._locks == {}


def test_main_chat_stream_projects_one_sub_agent_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """子 Agent 的自动工具事件和 custom 事件不能形成两套对外生命周期。"""
    graph = FakeStreamGraph(
        [
            {
                "event": "on_chat_model_stream",
                "run_id": "model-before-tool",
                "data": {"chunk": SimpleNamespace(content="我来规划。")},
            },
            {
                "event": "on_tool_start",
                "name": "travel",
                "tags": ["muye:sub-agent"],
                "data": {"input": {"task": "西安三日游"}},
            },
            {
                "event": "on_chain_stream",
                "data": {
                    "chunk": (
                        "custom",
                        {
                            "tool_id": "sub_agent_fixed",
                            "tool_name": "travel",
                            "status": "start",
                            "input": {"task": "西安三日游"},
                        },
                    )
                },
            },
            {
                "event": "on_chain_stream",
                "data": {
                    "chunk": (
                        "custom",
                        {
                            "tool_id": "sub_agent_fixed",
                            "tool_name": "travel",
                            "status": "result",
                            "blocks": [{"id": "child-b1", "type": "markdown", "content": "行程"}],
                        },
                    )
                },
            },
            {
                "event": "on_chain_stream",
                "data": {
                    "chunk": (
                        "custom",
                        {
                            "tool_id": "sub_agent_fixed",
                            "tool_name": "travel",
                            "status": "complete",
                            "duration_ms": 20,
                        },
                    )
                },
            },
            {
                "event": "on_tool_end",
                "name": "travel",
                "tags": ["muye:sub-agent"],
                "data": {"output": "行程"},
            },
            {
                "event": "on_chat_model_stream",
                "run_id": "model-after-tool",
                "data": {"chunk": SimpleNamespace(content="完整行程如下。")},
            },
        ]
    )
    manager = _stream_manager(monkeypatch, graph)

    envelopes = asyncio.run(_collect_main_stream(manager))
    tool_events = [item for item in envelopes if item["event"] == "tool"]
    block_events = [item for item in envelopes if item["event"] == "block"]

    assert [(item["data"]["id"], item["data"]["status"]) for item in tool_events] == [
        ("sub_agent_fixed", "start"),
        ("sub_agent_fixed", "result"),
        ("sub_agent_fixed", "complete"),
    ]
    assert [item["data"]["id"] for item in block_events] == ["b1", "b2"]
    assert envelopes[-2]["data"]["totalBlocks"] == 2
    assert manager.execution_manager._locks == {}


def test_main_chat_stream_keeps_regular_tool_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """未标记为子 Agent 的普通工具继续使用 LangChain 自动生命周期。"""
    manager = _stream_manager(
        monkeypatch,
        FakeStreamGraph(
            [
                {
                    "event": "on_tool_start",
                    "name": "web_search",
                    "tags": [],
                    "data": {"input": {"query": "西安"}},
                },
                {
                    "event": "on_tool_end",
                    "name": "web_search",
                    "tags": [],
                    "data": {"output": "result"},
                },
            ]
        ),
    )

    envelopes = asyncio.run(_collect_main_stream(manager))
    tool_events = [item for item in envelopes if item["event"] == "tool"]

    assert [(item["data"]["id"], item["data"]["status"]) for item in tool_events] == [
        ("tool_1", "start"),
        ("tool_1", "complete"),
    ]


def test_child_tool_events_keep_distinct_progress_meaning() -> None:
    """child tool 的开始和完成不能折叠成两条相同的 running 日志。"""
    forwarded: list[dict[str, object]] = []

    _forward_child_event(
        forwarded.append,
        "sub_agent_fixed",
        "travel",
        {"event": "tool", "data": {"name": "sample_itinerary", "status": "start"}},
    )
    _forward_child_event(
        forwarded.append,
        "sub_agent_fixed",
        "travel",
        {"event": "tool", "data": {"name": "sample_itinerary", "status": "complete"}},
    )

    assert [item["status"] for item in forwarded] == ["running", "running"]
    assert [item["log"] for item in forwarded] == [
        "子 Agent 开始执行工具 sample_itinerary",
        "子 Agent 工具 sample_itinerary 执行完成",
    ]


def test_main_chat_stream_maps_runtime_error_to_single_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _stream_manager(monkeypatch, FakeStreamGraph(error=RuntimeError("stream failed")))

    envelopes = asyncio.run(_collect_main_stream(manager))

    assert [item["event"] for item in envelopes] == ["session_start", "error", "done", "session_end"]
    assert envelopes[1]["data"]["code"] == "STREAM_ERROR"
    assert len({item["streamId"] for item in envelopes}) == 1
    assert manager.execution_manager._locks == {}


def test_main_chat_stream_idle_watchdog_emits_busy_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator_module, "STREAM_IDLE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(orchestrator_module, "STREAM_MAX_HOLD_TIMEOUT_SECONDS", 1.0)
    manager = _stream_manager(monkeypatch, FakeStreamGraph(block_forever=True))

    envelopes = asyncio.run(_collect_main_stream(manager))

    assert [item["event"] for item in envelopes] == ["session_start", "error", "done", "session_end"]
    assert envelopes[1]["data"]["code"] == "BUSY"
    assert manager.execution_manager._locks == {}


def test_main_chat_stream_lock_timeout_keeps_original_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator_module, "STREAM_LOCK_WAIT_TIMEOUT_SECONDS", 0.01)
    manager = _stream_manager(monkeypatch, FakeStreamGraph())

    async def run() -> list[dict[str, object]]:
        request = manager._execution_request("holder", "s1")
        options = ExecutionOptions(execution_key="main:s1", wait_for_session=True)
        async with manager.execution_manager.acquire("agent-main", request, options):
            return await _collect_main_stream(manager)

    envelopes = asyncio.run(run())

    assert [item["event"] for item in envelopes] == ["session_start", "error", "done", "session_end"]
    assert envelopes[1]["data"]["code"] == "LOCK_TIMEOUT"
    assert envelopes[1]["data"]["details"]["timeout_seconds"] == 0.01
    assert len({item["streamId"] for item in envelopes}) == 1
    assert manager.execution_manager._locks == {}


def test_main_llm_adapter_calls_muye_llm() -> None:
    """主 Agent 模型适配器必须调用 muye-llm 的内部 chat 路由。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": {"content": "网关响应"}})

    async def run() -> None:
        model = MuyeLlmChatModel(base_url="http://muye-llm.test", model_name="test")
        model.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        assert await model.chat_text([{"role": "user", "content": "你好"}]) == "网关响应"
        await model._client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())
    assert captured["path"] == "/api/v2/chat"
    assert captured["body"] == {
        "messages": [{"role": "user", "content": "你好"}],
        "temperature": 0.1,
        "max_tokens": 4096,
        "trace_id": "",
        "model": "test",
    }


def test_main_sub_agent_caller_negotiates_then_invokes() -> None:
    """主 Agent 对可信 descriptor 先读取能力声明，再调用 internal invoke。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/capabilities":
            return httpx.Response(200, json={"agent_name": "travel-agent", "internal_protocol_version": "muye-agent-internal/3.0", "api_profiles": ["internal"], "supports_streaming": True})
        return httpx.Response(200, json={"status": "success", "payload": {"result_data": {"markdown": "ok"}}})

    async def run() -> None:
        caller = SubAgentCaller(lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        result = await caller.invoke(SubAgentDescriptor("travel", "http://travel.test", 2), task="成都三日游", user_id="u1", session_id="s1")
        assert result["status"] == "success"

    asyncio.run(run())
    assert paths == ["/capabilities", "/invoke"]


def test_main_sub_agent_stream_filters_child_session_envelope() -> None:
    """子 Agent 的 session envelope 不能嵌套到主 Agent 的 SSE 流。"""
    paths: list[str] = []
    stream_body = "".join(
        [
            "event: session_start\ndata: {\"event\": \"session_start\"}\n\n",
            "event: thinking\ndata: {\"event\": \"thinking\", \"data\": {\"content\": \"计划\"}}\n\n",
            "event: done\ndata: {\"event\": \"done\"}\n\n",
            "event: session_end\ndata: {\"event\": \"session_end\"}\n\n",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/capabilities":
            return httpx.Response(200, json={"agent_name": "travel-agent", "internal_protocol_version": "muye-agent-internal/3.0", "api_profiles": ["internal"], "supports_streaming": True})
        return httpx.Response(200, text=stream_body, headers={"content-type": "text/event-stream"})

    async def run() -> None:
        caller = SubAgentCaller(lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        events = [
            event
            async for event in caller.stream(
                SubAgentDescriptor("travel", "http://travel.test", 2),
                task="成都三日游",
                user_id="u1",
                session_id="s1",
            )
        ]
        assert events == [{"event": "thinking", "data": {"content": "计划"}}]

    asyncio.run(run())
    assert paths == ["/capabilities", "/invoke/stream"]


def test_sub_agent_tools_expose_travel_and_order_routing_rules() -> None:
    """模型可从工具定义得知旅行和不完整订单也必须路由到对应子 Agent。"""
    tools = build_sub_agent_tools(
        SubAgentRegistry(
            [
                SubAgentDescriptor("travel", "http://travel.test", 2),
                SubAgentDescriptor("order", "http://order.test", 2),
            ]
        )
    )
    descriptions = {tool.name: tool.description for tool in tools}

    assert set(descriptions) == {"travel", "order"}
    assert "旅行" in descriptions["travel"]
    assert "必须调用" in descriptions["travel"]
    assert "信息不完整" in descriptions["order"]
    assert "必须调用" in descriptions["order"]

    assert all("muye:sub-agent" in (tool.tags or []) for tool in tools)


def test_sub_agent_tool_forwards_runtime_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """主 Agent 调用子服务时必须透传真实会话身份，不能落入固定默认值。"""
    captured: dict[str, str] = {}

    async def fake_stream(self, _descriptor, *, task, user_id, session_id, trace_id):
        captured.update(task=task, user_id=user_id, session_id=session_id, trace_id=trace_id)
        yield {"event": "block", "data": {"content": "ok"}}

    monkeypatch.setattr(SubAgentCaller, "stream", fake_stream)
    tool = build_sub_agent_tools(SubAgentRegistry([SubAgentDescriptor("travel", "http://travel.test", 2)]))[0]

    async def run() -> str:
        return await tool.ainvoke(
            {"task": "西安三日游"},
            config={"configurable": {"user_id": "u1", "thread_id": "s1", "trace_id": "t1"}},
        )

    assert asyncio.run(run())
    assert captured == {"task": "西安三日游", "user_id": "u1", "session_id": "s1", "trace_id": "t1"}


def test_sub_agent_tool_tag_propagates_to_langchain_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """主图必须能通过真实 LangChain event tag 识别子 Agent 工具。"""

    async def fake_stream(self, _descriptor, *, task, user_id, session_id, trace_id):
        yield {"event": "block", "data": {"type": "markdown", "content": "ok"}}

    monkeypatch.setattr(SubAgentCaller, "stream", fake_stream)
    tool = build_sub_agent_tools(
        SubAgentRegistry([SubAgentDescriptor("travel", "http://travel.test", 2)])
    )[0]

    async def run() -> list[dict[str, object]]:
        return [
            event
            async for event in tool.astream_events(
                {"task": "西安三日游"},
                config={"configurable": {"user_id": "u1", "thread_id": "s1", "trace_id": "t1"}},
                version="v2",
            )
        ]

    events = asyncio.run(run())
    lifecycle_events = [
        event for event in events if event.get("event") in {"on_tool_start", "on_tool_end"}
    ]

    assert [event["event"] for event in lifecycle_events] == ["on_tool_start", "on_tool_end"]
    assert all("muye:sub-agent" in event.get("tags", []) for event in lifecycle_events)


def test_system_prompt_routes_travel_and_incomplete_order_to_sub_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提示词不得将旅行或不完整订单重新路由到网页搜索或主 Agent 澄清。"""
    monkeypatch.setattr(
        main_config,
        "get_config",
        lambda: SimpleNamespace(task_decomposition=SimpleNamespace(mode="none")),
    )
    prompt = get_system_prompt()

    assert "旅行、旅游、出游、行程、路线、景点、交通、攻略、票务查询|`travel`" in prompt
    assert "用户一旦表达订单意图即调用订单子 Agent" in prompt
    assert "信息不完整时也必须先调用" in prompt


def test_main_sub_agent_caller_forwards_cancel() -> None:
    """主 Agent 应将取消请求转发到对应子 Agent。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "cancelled", "message": "已取消"})

    async def run() -> None:
        caller = SubAgentCaller(lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        result = await caller.cancel(
            SubAgentDescriptor("travel", "http://travel.test", 2),
            user_id="u1",
            session_id="s1",
            trace_id="t1",
        )
        assert result["status"] == "cancelled"

    asyncio.run(run())
    assert captured == {
        "path": "/cancel",
        "body": {"user_id": "u1", "session_id": "s1", "trace_id": "t1"},
    }


def test_main_sub_agent_caller_rejects_legacy_capabilities() -> None:
    """主 Agent 不得调用未声明 v3 internal 协议的子服务。"""
    with pytest.raises(SubAgentCallError, match="internal v3 协议"):
        SubAgentCaller._validate_capabilities(
            {"agent_name": "legacy", "api_profiles": ["internal"], "supports_streaming": True},
            require_streaming=True,
        )


def test_web_fetch_url_policy_rejects_private_network(monkeypatch) -> None:
    """网页抓取不得解析到内网、环回或保留地址。"""
    with pytest.raises(UnsafeUrlError):
        validate_external_url("http://127.0.0.1:8080/admin")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    assert validate_external_url("https://example.test/article") == "https://example.test/article"
