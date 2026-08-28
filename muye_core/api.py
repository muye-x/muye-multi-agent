"""阶段 1 v3 Core FastAPI 应用。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from hashlib import sha256
import logging
from pathlib import Path
from secrets import token_hex
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from .models import (
    AccessTokenResponse,
    AgentCreateRequest,
    AgentDetail,
    AgentSummary,
    CursorPage,
    DraftPatchRequest,
    DraftResponse,
    GrantReplaceRequest,
    LoginRequest,
    MeResponse,
    SourceUploadResponse,
    UserCreateRequest,
)
from .service import DomainError, InMemoryCoreStore, Principal
from .storage import ArtifactStore, AssetValidationError


logger = logging.getLogger(__name__)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


def create_app(*, store: InMemoryCoreStore | None = None, artifact_root: Path | None = None) -> FastAPI:
    """创建 v3 API；测试可注入内存仓储，生产适配器以后续阶段替换。"""

    service = store or InMemoryCoreStore()
    artifacts = ArtifactStore(artifact_root or Path("/var/lib/muye/artifacts"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(title="Muye Core", version="3.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable):
        request_id = request.headers.get("X-Request-Id") or f"request_{token_hex(16)}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return _error(exc.code, str(exc), _request_id(request), exc.status_code)

    @app.exception_handler(AssetValidationError)
    async def asset_error(request: Request, exc: AssetValidationError) -> JSONResponse:
        return _error("VALIDATION_ERROR", str(exc), _request_id(request), 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.exception("core request failed", extra={"request_id": request_id})
        return _error("INTERNAL_ERROR", "服务内部错误", request_id, 500)

    async def current_principal(authorization: str | None = Header(default=None)) -> Principal:
        token = _bearer_token(authorization)
        principal = service.principal(token) if token else None
        if principal is None:
            raise DomainError("AUTHENTICATION_ERROR", "登录会话无效", status_code=401)
        return principal

    async def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.is_admin:
            raise DomainError("AUTHORIZATION_ERROR", "需要管理员权限", status_code=403)
        return principal

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        service.readiness()
        artifacts.readiness()
        return {"status": "ready"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        return "muye_core_up 1\n"

    @app.post("/api/v3/bootstrap/admin", response_model=MeResponse, status_code=201)
    async def bootstrap(request: UserCreateRequest) -> MeResponse:
        principal = service.bootstrap_admin(request.username, request.password)
        return _me(principal)

    @app.post("/api/v3/auth/login", response_model=AccessTokenResponse)
    async def login(request: LoginRequest, response: Response) -> AccessTokenResponse:
        tokens = service.login(request.username, request.password)
        response.set_cookie("muye_refresh", tokens.refresh_token, httponly=True, secure=True, samesite="strict", path="/api/v3/auth")
        return AccessTokenResponse(access_token=tokens.access_token, expires_at=tokens.expires_at.isoformat())

    @app.post("/api/v3/auth/refresh", response_model=AccessTokenResponse)
    async def refresh(request: Request, response: Response) -> AccessTokenResponse:
        tokens = service.refresh(request.cookies.get("muye_refresh", ""))
        response.set_cookie("muye_refresh", tokens.refresh_token, httponly=True, secure=True, samesite="strict", path="/api/v3/auth")
        return AccessTokenResponse(access_token=tokens.access_token, expires_at=tokens.expires_at.isoformat())

    @app.post("/api/v3/auth/logout", status_code=204)
    async def logout(response: Response, authorization: str | None = Header(default=None)) -> Response:
        service.logout(_bearer_token(authorization))
        completed = Response(status_code=204)
        completed.delete_cookie("muye_refresh", path="/api/v3/auth")
        return completed

    @app.get("/api/v3/me", response_model=MeResponse)
    async def me(principal: Principal = Depends(current_principal)) -> MeResponse:
        return _me(principal)

    @app.post("/api/v3/users", response_model=MeResponse, status_code=201)
    async def create_user(request: UserCreateRequest, _admin: Principal = Depends(require_admin)) -> MeResponse:
        return _me(service.create_user(request.username, request.password))

    @app.put("/api/v3/users/{user_id}/agent-grants")
    async def replace_grants(user_id: str, request: GrantReplaceRequest, admin: Principal = Depends(require_admin)) -> dict[str, object]:
        return {"user_id": user_id, "agent_ids": sorted(service.replace_grants(admin, user_id, request.agent_ids))}

    @app.post("/api/v3/agents", response_model=None, status_code=201)
    async def create_agent(request: AgentCreateRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        def create() -> tuple[int, dict[str, object]]:
            agent, draft = service.create_agent(admin, **body)
            return 201, _detail(agent, draft)
        result = service.idempotent(f"agent-create:{admin.user_id}", key, body, create)
        return JSONResponse(result.body, status_code=result.status_code)

    @app.get("/api/v3/agents", response_model=CursorPage)
    async def list_agents(cursor: str | None = None, limit: int = 50, _admin: Principal = Depends(require_admin)) -> CursorPage:
        if not 1 <= limit <= 200:
            raise DomainError("VALIDATION_ERROR", "limit 必须在 1 至 200", status_code=422)
        items, next_cursor = service.agents_page(cursor, limit)
        return CursorPage(items=[_summary(item) for item in items], next_cursor=next_cursor)

    @app.get("/api/v3/agents/{agent_id}", response_model=AgentDetail)
    async def agent_detail(agent_id: str, _admin: Principal = Depends(require_admin)) -> AgentDetail:
        return AgentDetail.model_validate(_detail(*service.agent_detail(agent_id)))

    @app.patch("/api/v3/agents/{agent_id}/draft", response_model=None)
    async def patch_draft(agent_id: str, request: DraftPatchRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        result = service.idempotent(
            f"draft-patch:{agent_id}", key, body,
            lambda: (200, _draft(service.patch_draft(admin, agent_id, request.version, request.config))),
        )
        return JSONResponse(result.body, status_code=result.status_code)

    @app.delete("/api/v3/agents/{agent_id}/draft", status_code=204)
    async def discard_draft(agent_id: str, admin: Principal = Depends(require_admin)) -> Response:
        service.discard_draft(admin, agent_id)
        return Response(status_code=204)

    @app.post("/api/v3/agents/{agent_id}/stop", response_model=None)
    async def stop_agent(agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        result = service.idempotent(f"agent-stop:{agent_id}", key, {}, lambda: (200, _summary(service.suspend(admin, agent_id)).model_dump(mode="json")))
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/agents/{agent_id}/archive", response_model=None)
    async def archive_agent(agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        result = service.idempotent(f"agent-archive:{agent_id}", key, {}, lambda: (200, _summary(service.archive(admin, agent_id)).model_dump(mode="json")))
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/agents/{agent_id}/restore", response_model=None)
    async def restore_agent(agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        result = service.idempotent(f"agent-restore:{agent_id}", key, {}, lambda: (200, _summary(service.restore(admin, agent_id)).model_dump(mode="json")))
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/agents/{agent_id}/sources", response_model=SourceUploadResponse, status_code=201)
    async def upload_source(agent_id: str, file: UploadFile = File(...), _admin: Principal = Depends(require_admin)) -> SourceUploadResponse:
        service.agent_detail(agent_id)
        if file.content_type not in {"text/plain", "text/markdown", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
            raise DomainError("VALIDATION_ERROR", "不支持的文件类型", status_code=422)
        stored = artifacts.store(file.file, filename=file.filename or "")
        return SourceUploadResponse(asset_id=f"asset_{stored.sha256[:16]}", sha256=stored.sha256, size_bytes=stored.size_bytes, media_type=file.content_type, display_name=file.filename or "", reused=stored.reused)

    return app


def _bearer_token(authorization: str | None) -> str:
    prefix = "Bearer "
    return authorization[len(prefix):].strip() if authorization and authorization.startswith(prefix) else ""


def _request_id(request: Request) -> str:
    """为错误响应保留调用方相关 ID，成功响应由后续持久化中间件统一覆盖。"""

    return request.headers.get("X-Request-Id") or f"request_{token_hex(16)}"


def _error(code: str, message: str, request_id: str, status_code: int) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "request_id": request_id}, status_code=status_code)


def _me(principal: Principal) -> MeResponse:
    return MeResponse(user_id=principal.user_id, username=principal.username, is_admin=principal.is_admin)


def _summary(agent: object) -> AgentSummary:
    value = agent
    return AgentSummary(agent_id=value.agent_id, slug=value.slug, display_name=value.display_name, description=value.description, archived_at=value.archived_at.isoformat() if value.archived_at else None, suspended_at=value.suspended_at.isoformat() if value.suspended_at else None)


def _draft(draft: object) -> dict[str, object]:
    return {"agent_id": draft.agent_id, "version": draft.version, "config": draft.config}


def _detail(agent: object, draft: object | None) -> dict[str, object]:
    payload = _summary(agent).model_dump(mode="json")
    payload["draft"] = _draft(draft) if draft else None
    return payload
