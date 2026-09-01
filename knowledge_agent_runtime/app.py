"""固定 Runtime 的私有 HTTP 接口。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse

from contracts.v3 import (
    ChatStreamEventV1,
    RuntimeCancelRequestV1,
    RuntimeCapabilitiesV1,
    RuntimeInvokeRequestV1,
    RuntimeInvokeResponseV1,
)

from .service import RuntimeService


def create_app(service: RuntimeService) -> FastAPI:
    """创建不含内部 Token 认证的 Runtime 私有网络应用。"""

    app = FastAPI(title="knowledge-agent-runtime", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, str]:
        response.status_code = 200
        return {"status": "ready", "revision_id": service.bundle.revision.revision_id}

    @app.get("/capabilities", response_model=RuntimeCapabilitiesV1)
    async def capabilities() -> RuntimeCapabilitiesV1:
        manifest = service.bundle.manifest
        return RuntimeCapabilitiesV1(
            schema_version="muye.ai/runtime-capabilities/v1",
            agent_id=manifest.agent_id,
            revision_id=manifest.revision_id,
            revision_checksum=manifest.revision_checksum,
            runtime_contract_version=manifest.runtime_contract_version,
            supports_streaming=True,
            supports_cancel=True,
        )

    @app.post("/invoke", response_model=RuntimeInvokeResponseV1)
    async def invoke(request: RuntimeInvokeRequestV1) -> RuntimeInvokeResponseV1:
        return await _invoke_with_timeout(service, request)

    @app.post("/invoke/stream")
    async def invoke_stream(request: RuntimeInvokeRequestV1) -> StreamingResponse:
        return StreamingResponse(_stream_response(service, request), media_type="text/event-stream")

    @app.post("/cancel", status_code=202)
    async def cancel(request: RuntimeCancelRequestV1) -> dict[str, str]:
        service.cancel(request.request_id)
        return {"status": "accepted"}

    return app


async def _invoke_with_timeout(service: RuntimeService, request: RuntimeInvokeRequestV1) -> RuntimeInvokeResponseV1:
    """应用 Revision 的单请求总超时，并投影为稳定 Runtime 错误。"""

    try:
        async with asyncio.timeout(service.bundle.revision.budgets.timeout_seconds):
            return await service.invoke(request)
    except TimeoutError:
        service.cancel(request.request_id)
        return RuntimeInvokeResponseV1(
            schema_version="muye.ai/runtime-invoke-response/v1",
            request_id=request.request_id,
            status="refused",
            error_code="RUNTIME_TIMEOUT",
            error_message="请求处理超时。",
        )


async def _stream_response(service: RuntimeService, request: RuntimeInvokeRequestV1) -> AsyncIterator[str]:
    """以冻结 Chat SSE 契约传输终态 Runtime 响应，不泄漏后端细节。"""

    sequence = 0
    yield _sse(
        ChatStreamEventV1(
            schema_version="muye.ai/chat-stream-event/v1",
            event_type="session_start",
            sequence=sequence,
            session_id=request.session_id,
        )
    )
    sequence += 1
    result = await _invoke_with_timeout(service, request)
    if result.status == "success":
        yield _sse(
            ChatStreamEventV1(
                schema_version="muye.ai/chat-stream-event/v1",
                event_type="block_delta",
                sequence=sequence,
                session_id=request.session_id,
                block_id="block_0000000000000001",
                delta=result.content or "",
            )
        )
        sequence += 1
        yield _sse(
            ChatStreamEventV1(
                schema_version="muye.ai/chat-stream-event/v1",
                event_type="done",
                sequence=sequence,
                session_id=request.session_id,
                citations=result.citations,
                total_tokens=0,
            )
        )
    else:
        yield _sse(
            ChatStreamEventV1(
                schema_version="muye.ai/chat-stream-event/v1",
                event_type="error",
                sequence=sequence,
                session_id=request.session_id,
                error_code=result.error_code or "RUNTIME_ERROR",
                message=result.error_message or "请求失败。",
            )
        )
    sequence += 1
    yield _sse(
        ChatStreamEventV1(
            schema_version="muye.ai/chat-stream-event/v1",
            event_type="session_end",
            sequence=sequence,
            session_id=request.session_id,
        )
    )


def _sse(event: ChatStreamEventV1) -> str:
    """编码一个无内部字段的 SSE 事件。"""

    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event_type}\ndata: {payload}\n\n"
