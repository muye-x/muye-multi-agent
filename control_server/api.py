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
from .identity import IdentityStore, InMemoryIdentityStore, Principal
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
    AccessTokenResponse,
    GrantReplaceRequest,
    LoginRequest,
    MeResponse,
    SessionIntrospectionResponse,
    UserCreateRequest,
)


logger = logging.getLogger(__name__)


def create_app(
    *,
    projection: CatalogProjection,
    operator_token: str,
    main_token: str,
    health_token: str,
    gateway_token: str | None = None,
    identity_store: IdentityStore | None = None,
    cookie_secure: bool = True,
    health_poll_seconds: float | None = 5.0,
) -> FastAPI:
    """构造只暴露受认证内部路由的 Control ASGI 应用。"""
    service_tokens = tuple(token for token in (operator_token.strip(), main_token.strip(), health_token.strip(), (gateway_token or "").strip()) if token)
    if any(not token for token in service_tokens):
        raise ValueError("Control operator/main/health service token 不能为空")
    if len(set(service_tokens)) != len(service_tokens):
        raise ValueError("Control service token 必须互不相同")
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

    identities = identity_store or InMemoryIdentityStore()
    app = FastAPI(title="Muye Control Catalog", version="2.0.0", lifespan=lifespan)

    async def require_operator(authorization: str | None = Header(default=None)) -> None:
        _require_bearer(authorization, operator_token)

    async def require_main(authorization: str | None = Header(default=None)) -> None:
        _require_bearer(authorization, main_token)

    async def require_health(authorization: str | None = Header(default=None)) -> None:
        _require_bearer(authorization, health_token)

    async def require_gateway(authorization: str | None = Header(default=None)) -> None:
        if not gateway_token:
            raise HTTPException(status_code=404, detail="Gateway introspection 未启用")
        _require_bearer(authorization, gateway_token)

    async def current_principal(authorization: str | None = Header(default=None)) -> Principal:
        """在 ASGI 请求协程内校验 access token，失败时统一拒绝会话。"""
        token = _bearer_token(authorization)
        principal = identities.introspect(token) if token else None
        if principal is None:
            raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "登录会话无效"})
        return principal

    async def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
        """仅允许唯一内置 Admin 使用管理投影和 grant 写接口。"""
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail={"code": "AUTHORIZATION_ERROR", "message": "需要管理员权限"})
        return principal

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.post("/api/v2/auth/login", response_model=AccessTokenResponse)
    async def login(request: LoginRequest, response: Response) -> AccessTokenResponse:
        try:
            tokens = identities.login(username=request.username, password=request.password)
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "用户名或密码错误"}) from exc
        response.set_cookie("muye_refresh", tokens.refresh_token, httponly=True, secure=cookie_secure, samesite="strict", path="/api/v2/auth")
        return AccessTokenResponse(access_token=tokens.access_token, expires_at=tokens.expires_at.isoformat())

    @app.post("/api/v2/auth/refresh", response_model=AccessTokenResponse)
    async def refresh(request: Request, response: Response) -> AccessTokenResponse:
        try:
            tokens = identities.refresh(request.cookies.get("muye_refresh", ""))
        except PermissionError as exc:
            response.delete_cookie("muye_refresh", path="/api/v2/auth")
            raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "refresh session 无效"}) from exc
        response.set_cookie("muye_refresh", tokens.refresh_token, httponly=True, secure=cookie_secure, samesite="strict", path="/api/v2/auth")
        return AccessTokenResponse(access_token=tokens.access_token, expires_at=tokens.expires_at.isoformat())

    @app.post("/api/v2/auth/logout", status_code=204)
    async def logout(authorization: str | None = Header(default=None)) -> Response:
        """撤销当前 access session，并明确返回可完成的 204 ASGI 响应。"""
        identities.logout(_bearer_token(authorization))
        response = Response(status_code=204)
        response.delete_cookie("muye_refresh", path="/api/v2/auth")
        return response

    @app.get("/api/v2/me", response_model=MeResponse)
    async def me(principal: Principal = Depends(current_principal)) -> MeResponse:
        return MeResponse(user_id=principal.user_id, username=principal.username, is_admin=principal.is_admin)

    @app.get("/api/v2/me/agents")
    async def my_agents(principal: Principal = Depends(current_principal)) -> dict[str, object]:
        allowed = identities.allowed_agent_ids(principal.user_id)
        agents = [item for item in projection.active.agents if item.status == "ACTIVE" and item.agent_id in allowed]
        return {"agents": [item.model_dump(mode="json") for item in agents]}

    @app.get("/api/v2/topology")
    async def topology(_admin: Principal = Depends(require_admin)) -> dict[str, object]:
        """返回管理台所需的脱敏 Catalog 状态，不泄露内部 URL 或服务凭据。"""
        return {"catalog_revision": projection.active.catalog_revision, "agents": [_agent_summary(item) for item in projection.active.agents]}

    @app.get("/api/v2/agents")
    async def list_agents(_admin: Principal = Depends(require_admin)) -> dict[str, object]:
        return {"agents": [_agent_summary(item) for item in projection.active.agents]}

    @app.get("/api/v2/agents/{agent_id}")
    async def agent_detail(agent_id: str, _admin: Principal = Depends(require_admin)) -> dict[str, object]:
        agent = next((item for item in projection.active.agents if item.agent_id == agent_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Agent 不存在"})
        return _agent_summary(agent, include_bindings=True)

    @app.get("/api/v2/citations/{citation_id}", response_model=CitationResolveResponse)
    async def public_citation(citation_id: str, principal: Principal = Depends(current_principal)) -> CitationResolveResponse:
        try:
            record = projection.resolve_citation(citation_id=citation_id, user_id=principal.user_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "引用不可访问"}) from exc
        return CitationResolveResponse(
            citation_id=record.citation_id, agent_id=record.agent_id, agent_version=record.agent_version,
            knowledge_version_id=record.knowledge_version_id, locator=record.locator,
        )

    @app.get("/api/v2/users")
    async def list_users(_admin: Principal = Depends(require_admin)) -> dict[str, object]:
        return {"users": [
            {"user_id": item.user_id, "username": item.username, "is_admin": item.is_admin}
            for item in identities.list_users()
        ]}

    @app.post("/api/v2/users", status_code=201)
    async def create_user(request: UserCreateRequest, _admin: Principal = Depends(require_admin)) -> dict[str, object]:
        try:
            user = identities.create_user(username=request.username, password=request.password)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "CONFLICT", "message": str(exc)}) from exc
        return {"user_id": user.user_id, "username": user.username, "is_admin": user.is_admin}

    @app.get("/api/v2/users/{user_id}/agent-grants")
    async def get_grants(user_id: str, _admin: Principal = Depends(require_admin)) -> dict[str, object]:
        return {"user_id": user_id, "agent_ids": sorted(identities.allowed_agent_ids(user_id))}

    @app.put("/api/v2/users/{user_id}/agent-grants")
    async def replace_grants(user_id: str, request: GrantReplaceRequest, admin: Principal = Depends(require_admin)) -> dict[str, object]:
        active_agent_ids = {item.agent_id for item in projection.active.agents}
        requested_agent_ids = frozenset(request.agent_ids)
        if not requested_agent_ids <= active_agent_ids:
            raise HTTPException(status_code=422, detail={"code": "VALIDATION_ERROR", "message": "包含未知或未激活的 Agent"})
        try:
            agent_ids = identities.replace_grants(actor_id=admin.user_id, user_id=user_id, agent_ids=requested_agent_ids)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "用户不存在"}) from exc
        return {"user_id": user_id, "agent_ids": sorted(agent_ids)}

    @app.post("/internal/v1/auth/session-introspect", response_model=SessionIntrospectionResponse, dependencies=[Depends(require_gateway)])
    async def session_introspect(authorization: str | None = Header(default=None, alias="X-Muye-Session-Authorization")) -> SessionIntrospectionResponse:
        principal = identities.introspect(_bearer_token(authorization))
        if principal is None:
            raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "登录会话无效"})
        return SessionIntrospectionResponse(user_id=principal.user_id, is_admin=principal.is_admin)

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
    actual = _bearer_token(authorization)
    if not actual:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "服务认证失败"})
    if not compare_digest(actual, expected_token.strip()):
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "服务认证失败"})


def _bearer_token(authorization: str | None) -> str:
    """从单一 Authorization header 读取 bearer token，格式错误视为匿名。"""
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        return ""
    return authorization[len(prefix) :].strip()


def _agent_summary(agent: object, *, include_bindings: bool = False) -> dict[str, object]:
    """将 Catalog entry 转为 Web 可展示投影，排除容器网络地址与 checksum。"""
    # Catalog 的 Pydantic 模型由 Control 自身构造；这里仍只白名单公开字段。
    result = {
        "agent_id": agent.agent_id,
        "agent_version": agent.agent_version,
        "display_name": agent.display_name,
        "description": agent.description,
        "supported_intents": agent.supported_intents,
        "status": agent.status,
    }
    if include_bindings:
        result["resource_bindings"] = [
            {"resource_id": binding.resource_id, "skill_ref": binding.skill_ref}
            for binding in agent.resource_bindings
        ]
    return result
