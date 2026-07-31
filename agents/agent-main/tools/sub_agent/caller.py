"""主服务对子 Agent 调用的业务适配层。"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
import time
from typing import Any

import httpx
from muye_multi_agent_sdk import AgentContext, AgentIdentity, AgentRequest
from muye_multi_agent_sdk.integrations import InternalAgentClient, InternalAgentClientError

from .registry import SubAgentDescriptor


class SubAgentCallError(RuntimeError):
    """将 SDK transport 失败转换为主服务可展示的调用错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SubAgentCaller:
    """将已注册的业务子 Agent 描述符适配为 SDK internal client 调用。"""

    def __init__(self, client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient) -> None:
        self._client = InternalAgentClient(client_factory)

    async def invoke(
        self,
        descriptor: SubAgentDescriptor,
        *,
        task: str,
        user_id: str,
        session_id: str,
        trace_id: str = "",
        service_token: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._client.invoke(
                base_url=descriptor.url,
                timeout_seconds=descriptor.timeout_seconds,
                request=self._request(task, user_id, session_id, trace_id),
                service_name=f"子 Agent {descriptor.name}",
                expected_identity=self._expected_identity(descriptor),
                expected_protocol_version=descriptor.protocol_version,
                expected_profile=descriptor.profile,
                deadline_monotonic=time.monotonic() + descriptor.timeout_seconds,
                service_token=service_token,
            )
        except InternalAgentClientError as exc:
            raise SubAgentCallError(self._error_code(exc), str(exc)) from exc

    async def stream(
        self,
        descriptor: SubAgentDescriptor,
        *,
        task: str,
        user_id: str,
        session_id: str,
        trace_id: str = "",
        service_token: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in self._client.stream(
                base_url=descriptor.url,
                timeout_seconds=descriptor.timeout_seconds,
                request=self._request(task, user_id, session_id, trace_id),
                service_name=f"子 Agent {descriptor.name}",
                expected_identity=self._expected_identity(descriptor),
                expected_protocol_version=descriptor.protocol_version,
                expected_profile=descriptor.profile,
                deadline_monotonic=time.monotonic() + descriptor.timeout_seconds,
                service_token=service_token,
            ):
                yield event
        except InternalAgentClientError as exc:
            raise SubAgentCallError(self._error_code(exc), str(exc)) from exc

    async def cancel(
        self,
        descriptor: SubAgentDescriptor,
        *,
        user_id: str,
        session_id: str,
        trace_id: str = "",
        service_token: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._client.cancel(
                base_url=descriptor.url,
                timeout_seconds=descriptor.timeout_seconds,
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
                service_name=f"子 Agent {descriptor.name}",
                deadline_monotonic=time.monotonic() + descriptor.timeout_seconds,
                service_token=service_token,
            )
        except InternalAgentClientError as exc:
            raise SubAgentCallError(self._error_code(exc), str(exc)) from exc

    @staticmethod
    def _request(task: str, user_id: str, session_id: str, trace_id: str) -> AgentRequest:
        return AgentRequest(
            task=task,
            context=AgentContext(user_id=user_id, session_id=session_id, trace_id=trace_id or AgentContext().trace_id),
        )

    @staticmethod
    def _expected_identity(descriptor: SubAgentDescriptor) -> AgentIdentity | None:
        """Catalog descriptor 必须协商部署 identity；旧测试 descriptor 保持兼容。"""
        if not descriptor.descriptor_checksum or not descriptor.source_tree_checksum:
            return None
        return AgentIdentity(
            agent_id=descriptor.agent_id,
            agent_version=descriptor.agent_version,
            descriptor_checksum=descriptor.descriptor_checksum,
            source_tree_checksum=descriptor.source_tree_checksum,
        )

    @staticmethod
    def _error_code(exc: InternalAgentClientError) -> str:
        message = str(exc)
        if "身份" in message or "源码校验和" in message or "能力" in message or "协议版本" in message:
            return "CAPABILITIES_MISMATCH"
        if "超时" in message or "deadline" in message:
            return "DEPENDENCY_UNAVAILABLE"
        return "AGENT_NOT_READY"

    @staticmethod
    def _validate_capabilities(capabilities: object, *, require_streaming: bool) -> None:
        """保留旧测试与调用方的兼容入口。"""
        try:
            InternalAgentClient.validate_capabilities(capabilities, require_streaming=require_streaming)
        except InternalAgentClientError as exc:
            raise SubAgentCallError("CAPABILITIES_MISMATCH", str(exc)) from exc
