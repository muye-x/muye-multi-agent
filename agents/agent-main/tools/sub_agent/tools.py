"""把单次请求获授权的 Catalog 子集投影为 LangChain 工具。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import copy
import time
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import SecretStr

from .caller import SubAgentCallError, SubAgentCaller
from .catalog import CitationEvidence
from .registry import SubAgentDescriptor, SubAgentRegistry
from .runtime import SubAgentRuntimeError, SubAgentRuntimeGuard


SUB_AGENT_TOOL_TAG = "muye:sub-agent"
CitationRecorder = Callable[[SubAgentDescriptor, str, CitationEvidence], Awaitable[None]]


def _get_tool_description(descriptor: SubAgentDescriptor) -> str:
    """只使用当前用户已授权 entry 的公开描述和 intents 构建模型上下文。"""
    intents = "、".join(descriptor.supported_intents)
    suffix = f"支持的意图：{intents}。" if intents else ""
    return f"{descriptor.description.strip()} {suffix}".strip()


def _get_stream_writer(config: RunnableConfig | None) -> Any | None:
    if config is None:
        return None
    runtime = config.get("configurable", {}).get("__pregel_runtime")
    return getattr(runtime, "stream_writer", None)


def _request_identity(
    config: RunnableConfig | None,
    descriptor: SubAgentDescriptor,
) -> tuple[str, str, str]:
    """提取可信运行时身份，并在网络调用前再次核对授权集合和 Catalog revision。"""
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    user_id = str(configurable.get("user_id") or "").strip()
    session_id = str(configurable.get("session_id") or configurable.get("thread_id") or "").strip()
    trace_id = str(configurable.get("trace_id") or f"main_{uuid.uuid4().hex}").strip()
    if not user_id or not session_id:
        raise SubAgentCallError("AUTHORIZATION_ERROR", "缺少调用子 Agent 所需的可信用户或会话身份")
    if descriptor.catalog_revision:
        revision = str(configurable.get("catalog_revision") or "")
        allowed = configurable.get("allowed_agent_ids")
        if revision != descriptor.catalog_revision:
            raise SubAgentCallError("CATALOG_REJECTED", "请求 Catalog revision 已失效")
        if not isinstance(allowed, (list, tuple, set, frozenset)) or descriptor.agent_id not in allowed:
            raise SubAgentCallError("AUTHORIZATION_ERROR", "当前请求无权调用该子 Agent")
    return user_id, session_id, trace_id


def _forward_child_event(writer: Any, tool_id: str, tool_name: str, event: dict[str, Any]) -> None:
    """将子 Agent 中间事件映射为主 Agent 的单一工具生命周期。"""
    event_type = event.get("event")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event_type == "thinking":
        writer({"tool_id": tool_id, "tool_name": tool_name, "status": "running", "log": data.get("content") or "子 Agent 正在处理"})
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


def _citation_evidence(event: dict[str, Any]) -> tuple[CitationEvidence, ...]:
    """只读取模板放入受信任终态 result_data 的 citation 证明。"""
    data = event.get("data")
    if not isinstance(data, dict):
        return ()
    content = data.get("content")
    if not isinstance(content, dict):
        return ()
    payload = content.get("data")
    if not isinstance(payload, dict):
        return ()
    result_data = payload.get("result_data")
    if not isinstance(result_data, dict):
        return ()
    records = result_data.get("_muye_citations")
    if not isinstance(records, list):
        return ()
    evidence: list[CitationEvidence] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        citation_id = record.get("citation_id")
        knowledge_version_id = record.get("knowledge_version_id")
        locator = record.get("locator")
        title = record.get("title")
        source = record.get("source")
        if (
            not isinstance(citation_id, str)
            or citation_id in seen
            or not isinstance(knowledge_version_id, str)
            or not isinstance(locator, dict)
            or not isinstance(title, str)
            or not isinstance(source, str)
        ):
            continue
        evidence.append(
            CitationEvidence(
                citation_id=citation_id,
                knowledge_version_id=knowledge_version_id,
                locator=locator,
                title=title,
                source=source,
            )
        )
        seen.add(citation_id)
    return tuple(evidence)


def _model_visible_event(event: dict[str, Any]) -> dict[str, Any]:
    """移除仅供 Main/Control 授权记录使用的字段，避免内部身份进入模型上下文。"""
    projected = copy.deepcopy(event)
    data = projected.get("data")
    content = data.get("content") if isinstance(data, dict) else None
    payload = content.get("data") if isinstance(content, dict) else None
    result_data = payload.get("result_data") if isinstance(payload, dict) else None
    if isinstance(result_data, dict):
        result_data.pop("_muye_citations", None)
    return projected


def build_sub_agent_tools(
    registry: SubAgentRegistry,
    *,
    caller: SubAgentCaller | None = None,
    runtime_guard: SubAgentRuntimeGuard | None = None,
    token_provider: Callable[[str], str | SecretStr | None] | None = None,
    citation_recorder: CitationRecorder | None = None,
) -> list[BaseTool]:
    """为 AuthorizedCatalogView 创建工具；registry 外的 Agent 不会进入模型上下文。"""
    active_caller = caller or SubAgentCaller()
    guard = runtime_guard or SubAgentRuntimeGuard()
    tools: list[BaseTool] = []
    for descriptor in registry.values():
        @tool(descriptor.name, description=_get_tool_description(descriptor))
        async def call(task: str, config: RunnableConfig, _descriptor: SubAgentDescriptor = descriptor) -> str:
            tool_name = _descriptor.name
            tool_id = f"sub_agent_{uuid.uuid4().hex[:8]}"
            writer = _get_stream_writer(config)
            started_at = time.monotonic()
            semaphore = None
            token: str | None = None
            try:
                user_id, session_id, trace_id = _request_identity(config, _descriptor)
                if token_provider is not None:
                    raw_token = token_provider(_descriptor.agent_id)
                    token = raw_token.get_secret_value() if isinstance(raw_token, SecretStr) else raw_token
                if _descriptor.catalog_revision and (not isinstance(token, str) or not token.strip()):
                    raise SubAgentCallError("AUTHENTICATION_ERROR", "子 Agent 服务凭据不可用")
                semaphore = await guard.acquire(
                    _descriptor.agent_id,
                    _descriptor.max_concurrency,
                    request_id=trace_id,
                )
                if writer is not None:
                    writer({"tool_id": tool_id, "tool_name": tool_name, "status": "start", "input": {"task": task}, "log": "正在调用子 Agent", "progress": 0})
                result_parts: list[str] = []
                citations: dict[str, CitationEvidence] = {}
                async for event in active_caller.stream(
                    _descriptor,
                    task=task,
                    user_id=user_id,
                    session_id=session_id,
                    trace_id=trace_id,
                    service_token=token,
                ):
                    if writer is not None:
                        _forward_child_event(writer, tool_id, tool_name, event)
                    for evidence in _citation_evidence(event):
                        citations[evidence.citation_id] = evidence
                    if event.get("event") == "block":
                        result_parts.append(str(_model_visible_event(event).get("data", {})))
                if not result_parts:
                    raise SubAgentCallError("DEPENDENCY_UNAVAILABLE", "子 Agent 未返回可展示结果")
                if citations and citation_recorder is None:
                    raise SubAgentCallError("DEPENDENCY_UNAVAILABLE", "可信引用记录服务不可用")
                for evidence in citations.values():
                    try:
                        # 为每个用户生成唯一的 citation_id，避免跨用户冲突
                        from tools.sub_agent.catalog import CitationEvidence
                        unique_evidence = CitationEvidence(
                            citation_id=f"{evidence.citation_id}_{user_id}",
                            knowledge_version_id=evidence.knowledge_version_id,
                            locator=evidence.locator,
                            title=evidence.title,
                            source=evidence.source,
                        )
                        await citation_recorder(_descriptor, user_id, unique_evidence)
                    except Exception as exc:
                        raise SubAgentCallError("DEPENDENCY_UNAVAILABLE", "可信引用记录失败") from exc
                    if writer is not None:
                        writer(
                            {
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "status": "result",
                                "blocks": [
                                    {
                                        "type": "citation",
                                        "citation_id": unique_evidence.citation_id,
                                        "title": evidence.title,
                                        "source": evidence.source,
                                    }
                                ],
                            }
                        )
                guard.succeeded(_descriptor.agent_id)
                if writer is not None:
                    writer({"tool_id": tool_id, "tool_name": tool_name, "status": "complete", "duration_ms": int((time.monotonic() - started_at) * 1000)})
                return "\n".join(result_parts)
            except asyncio.CancelledError:
                if "user_id" in locals() and "session_id" in locals() and "trace_id" in locals():
                    try:
                        await active_caller.cancel(
                            _descriptor,
                            user_id=user_id,
                            session_id=session_id,
                            trace_id=trace_id,
                            service_token=token,
                        )
                    except SubAgentCallError:
                        pass
                raise
            except SubAgentRuntimeError as exc:
                if writer is not None:
                    writer({"tool_id": tool_id, "tool_name": tool_name, "status": "error", "log": str(exc)})
                return f"[{exc.code}] {exc}"
            except SubAgentCallError as exc:
                if exc.code in {"AGENT_NOT_READY", "CAPABILITIES_MISMATCH", "DEPENDENCY_UNAVAILABLE"}:
                    guard.failed(_descriptor.agent_id)
                if writer is not None:
                    writer({"tool_id": tool_id, "tool_name": tool_name, "status": "error", "log": str(exc)})
                return f"[{exc.code}] {exc}"
            finally:
                if semaphore is not None:
                    semaphore.release()

        call.tags = [*(call.tags or []), SUB_AGENT_TOOL_TAG]
        tools.append(call)
    return tools
