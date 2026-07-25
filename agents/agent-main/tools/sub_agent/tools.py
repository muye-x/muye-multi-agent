"""将注册的子服务投影为 LangChain 工具。"""
from __future__ import annotations
import time
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from .caller import SubAgentCallError, SubAgentCaller
from .registry import SubAgentRegistry


SUB_AGENT_TOOL_TAG = "muye:sub-agent"


SUB_AGENT_DESCRIPTIONS = {
    "travel": (
        "处理旅行、旅游、出游、行程规划、路线、景点、交通、攻略与票务查询。"
        "只要用户存在旅行意图就必须调用此工具；将完整需求传入 task。"
    ),
    "order": (
        "处理酒店、门票、餐饮等预订、购买、下单、订单查询或取消。"
        "只要用户存在订单意图就必须调用此工具，即使日期、人数、房型等信息不完整，"
        "也必须由此工具返回澄清结果，不能在主 Agent 直接询问。"
    ),
}


def _get_tool_description(agent_name: str) -> str:
    """返回用于模型路由的子 Agent 能力描述。"""
    return SUB_AGENT_DESCRIPTIONS.get(
        agent_name,
        f"调用 Muye {agent_name} 子 Agent 完成相关任务。",
    )

def _get_stream_writer(config: RunnableConfig | None) -> Any | None:
    """返回当前 LangGraph 执行可用的 custom stream writer。"""
    if config is None:
        return None
    runtime = config.get("configurable", {}).get("__pregel_runtime")
    return getattr(runtime, "stream_writer", None)


def _request_identity(config: RunnableConfig | None) -> tuple[str, str, str]:
    """从主 Agent 的可信运行时配置提取子 Agent 会话身份。"""
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    user_id = str(configurable.get("user_id") or "").strip()
    session_id = str(configurable.get("session_id") or configurable.get("thread_id") or "").strip()
    trace_id = str(configurable.get("trace_id") or f"main_{uuid.uuid4().hex}").strip()
    if not user_id or not session_id:
        raise SubAgentCallError("缺少调用子 Agent 所需的 user_id 或 session_id")
    return user_id, session_id, trace_id


def _forward_child_event(writer: Any, tool_id: str, tool_name: str, event: dict[str, Any]) -> None:
    """将子 Agent 中间事件映射为主 Agent 的工具事件。"""
    event_type = event.get("event")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event_type == "thinking":
        writer(
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "status": "running",
                "log": data.get("content") or "子 Agent 正在处理",
            }
        )
    elif event_type == "tool":
        child_tool_name = str(data.get("name") or "tool")
        child_status = str(data.get("status") or "running")
        if child_status == "start":
            log = f"子 Agent 开始执行工具 {child_tool_name}"
        elif child_status == "complete":
            log = f"子 Agent 工具 {child_tool_name} 执行完成"
        else:
            log = f"子 Agent 工具 {child_tool_name} 状态：{child_status}"
        writer({"tool_id": tool_id, "tool_name": tool_name, "status": "running", "log": log})
    elif event_type == "block":
        writer({"tool_id": tool_id, "tool_name": tool_name, "status": "result", "blocks": [data]})
    elif event_type in {"error", "interrupted", "clarification_needed"}:
        writer({"tool_id": tool_id, "tool_name": tool_name, "status": "error", "log": data.get("message") or "子 Agent 未完成任务"})


def build_sub_agent_tools(registry: SubAgentRegistry) -> list[BaseTool]:
    """为每个 descriptor 创建名称稳定、无任意网络权限的异步工具。"""
    caller = SubAgentCaller()
    tools: list[BaseTool] = []
    for descriptor in registry.values():
        @tool(descriptor.name, description=_get_tool_description(descriptor.name))
        async def call(
            task: str,
            config: RunnableConfig,
            _descriptor=descriptor,
        ) -> str:
            tool_name = _descriptor.name
            tool_id = f"sub_agent_{uuid.uuid4().hex[:8]}"
            writer = _get_stream_writer(config)
            started_at = time.monotonic()
            if writer is not None:
                writer({"tool_id": tool_id, "tool_name": tool_name, "status": "start", "input": {"task": task}, "log": "正在调用子 Agent", "progress": 0})
            try:
                user_id, session_id, trace_id = _request_identity(config)
                result_parts: list[str] = []
                async for event in caller.stream(
                    _descriptor,
                    task=task,
                    user_id=user_id,
                    session_id=session_id,
                    trace_id=trace_id,
                ):
                    if writer is not None:
                        _forward_child_event(writer, tool_id, tool_name, event)
                    if event.get("event") == "block":
                        result_parts.append(str(event.get("data", {})))
            except SubAgentCallError as exc:
                if writer is not None:
                    writer({"tool_id": tool_id, "tool_name": tool_name, "status": "error", "log": str(exc)})
                return f"子 Agent 暂不可用：{exc}"
            if writer is not None:
                writer({"tool_id": tool_id, "tool_name": tool_name, "status": "complete", "duration_ms": int((time.monotonic() - started_at) * 1000)})
            return "\n".join(result_parts) or "子 Agent 已完成，但未返回可展示结果。"
        call.tags = [*(call.tags or []), SUB_AGENT_TOOL_TAG]
        tools.append(call)
    return tools
