"""muye-data 的只读 FastAPI 路由与稳定错误投影。"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from fastapi.responses import JSONResponse

from src.contracts import (
    RESOURCE_NAME_PATTERN,
    TRACE_ID_PATTERN,
    ErrorResponse,
    HealthResponse,
    ReadinessResponse,
    ResourceCapabilities,
    RetrievalResponse,
    RetrieveRequest,
)
from src.retrieval.service import RetrievalService


logger = logging.getLogger(__name__)
router = APIRouter()

RETRIEVE_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "资源级请求参数无效"},
    404: {"model": ErrorResponse, "description": "资源或 pipeline 不存在"},
    422: {"model": ErrorResponse, "description": "请求结构校验失败"},
    502: {"model": ErrorResponse, "description": "下游响应协议无效"},
    503: {"model": ErrorResponse, "description": "下游依赖暂时不可用"},
    504: {"model": ErrorResponse, "description": "召回请求超时"},
}


def get_service(request: Request) -> RetrievalService:
    """从应用生命周期状态获取已初始化服务。"""
    service = getattr(request.app.state, "retrieval_service", None)
    if service is None:
        raise RuntimeError("retrieval service is not initialized")
    return service


@router.post(
    "/api/v1/retrieve",
    response_model=RetrievalResponse,
    responses=RETRIEVE_ERROR_RESPONSES,
    tags=["retrieval"],
)
async def retrieve(
    request_data: RetrieveRequest,
    request: Request,
    response: Response,
) -> RetrievalResponse:
    """执行配置指定的完整召回 pipeline。"""
    result = await get_service(request).retrieve(request_data)
    response.headers["X-Trace-Id"] = result.trace_id
    return result


@router.get(
    "/api/v1/resources/{resource}/capabilities",
    response_model=ResourceCapabilities,
    responses={
        404: {"model": ErrorResponse, "description": "资源不存在"},
        422: {"model": ErrorResponse, "description": "资源 alias 格式无效"},
    },
    tags=["retrieval"],
)
async def capabilities(
    resource: Annotated[str, Path(pattern=RESOURCE_NAME_PATTERN)],
    request: Request,
    response: Response,
) -> ResourceCapabilities:
    """返回资源公开能力；不会连接数据库。"""
    candidate = request.headers.get("X-Trace-Id", "").strip()
    trace_id = (
        candidate
        if candidate and re.fullmatch(TRACE_ID_PATTERN, candidate) is not None
        else uuid.uuid4().hex
    )
    result = get_service(request).capabilities(resource, trace_id=trace_id)
    response.headers["X-Trace-Id"] = trace_id
    return result


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(request: Request) -> HealthResponse:
    """仅报告进程存活，不探测任何远端依赖。"""
    started_at = getattr(request.app.state, "started_at", time.monotonic())
    return HealthResponse(
        status="ok",
        service="muye-data",
        uptime=round(time.monotonic() - started_at, 1),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "没有可服务资源"}},
    tags=["health"],
)
async def ready(request: Request) -> Response:
    """返回脱敏依赖状态；至少一个资源可服务时保持 HTTP 200。"""
    report, available = await get_service(request).readiness()
    return JSONResponse(
        status_code=200 if available else 503,
        content=report.model_dump(mode="json"),
    )
