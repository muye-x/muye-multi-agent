"""阶段 5 Control 内部 API；不包含阶段 6 的用户/Web 管理接口。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from secrets import compare_digest

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .catalog import CatalogProjection
from .health import CatalogCandidateError
from .models import (
    AuthorizationResolveRequest,
    AuthorizationResolveResponse,
    AgentObservationRequest,
    AgentObservationResponse,
    CatalogAckRequest,
    CatalogCandidateRequest,
    CatalogCandidateResponse,
    CitationRecordRequest,
    CitationResolveResponse,
)


logger = logging.getLogger(__name__)


def create_app(
    *,
    projection: CatalogProjection,
    operator_token: str,
    main_token: str,
    health_token: str,
    health_poll_seconds: float | None = 5.0,
) -> FastAPI:
    """构造只暴露受认证内部路由的 Control ASGI 应用。"""
    service_tokens = (operator_token.strip(), main_token.strip(), health_token.strip())
    if any(not token for token in service_tokens):
        raise ValueError("Control operator/main/health service token 不能为空")
    if len(set(service_tokens)) != len(service_tokens):
        raise ValueError("Control operator/main/health service token 必须互不相同")
    if health_poll_seconds is not None and not 0.1 <= health_poll_seconds <= 300:
        raise ValueError("Control health poll interval 必须为 0.1 至 300 秒")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = None
        if health_poll_seconds is not None:
            task = asyncio.create_task(
                _collect_health_forever(projection, health_poll_seconds),
                name="control-agent-health-collector",
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Muye Control Catalog", version="2.0.0", lifespan=lifespan)

    async def require_operator(authorization: str | None = Header(default=None)) -> None:
        _require_bearer(authorization, operator_token)

    async def require_main(authorization: str | None = Header(default=None)) -> None:
        _require_bearer(authorization, main_token)

    async def require_health(authorization: str | None = Header(default=None)) -> None:
        _require_bearer(authorization, health_token)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.post(
        "/internal/v1/catalog/candidates",
        response_model=CatalogCandidateResponse,
        dependencies=[Depends(require_operator)],
    )
    async def submit_candidate(request: CatalogCandidateRequest) -> CatalogCandidateResponse:
        try:
            return await projection.submit_candidate(request)
        except (CatalogCandidateError, ValueError) as exc:
            raise HTTPException(status_code=409, detail={"code": "CATALOG_REJECTED", "message": str(exc)}) from exc

    @app.get("/internal/v1/catalog/active", dependencies=[Depends(require_main)])
    async def active_catalog(request: Request) -> Response:
        snapshot = projection.catalog_for_main
        etag = f'"{snapshot.catalog_checksum}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(snapshot.model_dump(mode="json"), headers={"ETag": etag})

    @app.get("/internal/v1/deployment/catalog/active", dependencies=[Depends(require_operator)])
    async def deployment_active_catalog() -> Response:
        """允许部署身份读取并发前置 checksum，不授予用户或 Main 权限。"""
        snapshot = projection.active
        return JSONResponse(
            snapshot.model_dump(mode="json"),
            headers={"ETag": f'"{snapshot.catalog_checksum}"'},
        )

    @app.post("/internal/v1/catalog/{revision}/acks", dependencies=[Depends(require_main)])
    async def ack_catalog(revision: str, request: CatalogAckRequest) -> dict[str, object]:
        try:
            projection.record_ack(
                revision=revision,
                checksum=request.catalog_checksum,
                accepted=request.accepted,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "CATALOG_REJECTED", "message": str(exc)}) from exc
        return {"accepted": request.accepted, "catalog_revision": revision}

    @app.get("/internal/v1/catalog/{revision}/acks/main", dependencies=[Depends(require_operator)])
    async def main_ack(revision: str, checksum: str) -> dict[str, object]:
        return {
            "accepted": projection.is_acked(revision=revision, checksum=checksum),
            "catalog_revision": revision,
        }

    @app.post(
        "/internal/v1/agent-authorizations/resolve",
        response_model=AuthorizationResolveResponse,
        dependencies=[Depends(require_main)],
    )
    async def resolve_authorization(request: AuthorizationResolveRequest) -> AuthorizationResolveResponse:
        try:
            return projection.resolve_authorization(request.user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "AUTHORIZATION_ERROR", "message": "授权存储不可用"},
            ) from exc

    @app.post(
        "/internal/v1/agent-observations",
        response_model=AgentObservationResponse,
        dependencies=[Depends(require_health)],
    )
    async def record_observation(request: AgentObservationRequest) -> AgentObservationResponse:
        try:
            return await projection.record_observation(request)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "CATALOG_REJECTED", "message": str(exc)},
            ) from exc

    @app.post("/internal/v1/citations", dependencies=[Depends(require_main)])
    async def record_citation(request: CitationRecordRequest) -> dict[str, str]:
        try:
            record = projection.record_citation(request)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "AUTHORIZATION_ERROR", "message": str(exc)},
            ) from exc
        return {"citation_id": record.citation_id, "status": "recorded"}

    @app.get(
        "/internal/v1/citations/{citation_id}",
        response_model=CitationResolveResponse,
        dependencies=[Depends(require_main)],
    )
    async def resolve_citation(citation_id: str, user_id: str) -> CitationResolveResponse:
        try:
            record = projection.resolve_citation(citation_id=citation_id, user_id=user_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"code": "AUTHORIZATION_ERROR", "message": "引用不可访问"}) from exc
        return CitationResolveResponse(
            citation_id=record.citation_id,
            agent_id=record.agent_id,
            agent_version=record.agent_version,
            knowledge_version_id=record.knowledge_version_id,
            locator=record.locator,
        )

    return app


async def _collect_health_forever(projection: CatalogProjection, interval_seconds: float) -> None:
    """以固定静态间隔采集健康状态；单轮失败不终止后续采集。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await projection.collect_health_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Control Agent health collection round failed")


def _require_bearer(authorization: str | None, expected_token: str) -> None:
    """常量时间比较固定服务 token，不把 token 或 Header 写入错误信息。"""
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "服务认证失败"})
    actual = authorization[len(prefix) :].strip()
    if not actual or not compare_digest(actual, expected_token.strip()):
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "服务认证失败"})
