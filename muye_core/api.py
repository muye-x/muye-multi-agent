"""阶段 1 v3 Core FastAPI 应用。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
from secrets import token_hex
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from contracts.v3 import ChatStreamEventV1, RuntimeCitationV1

from .models import (
    AccessTokenResponse,
    AgentCreateRequest,
    AgentDetail,
    AgentSummary,
    ChatStreamRequest,
    CursorPage,
    DraftPatchRequest,
    DraftImpactResponse,
    DraftResponse,
    GrantReplaceRequest,
    LoginRequest,
    JobCreateRequest,
    JobResponse,
    ProfileProposalResponse,
    MeResponse,
    RevisionApprovalRequest,
    RevisionFreezeRequest,
    RevisionResponse,
    RuntimeInvokeRequest,
    SourceUploadResponse,
    UserCreateRequest,
)
from .service import CoreStore, DomainError, Principal
from .storage import ArtifactStore, AssetValidationError
from .runtime import RuntimeInvoker
from .knowledge_runtime import CoreEvidence, CoreKnowledgeBackend


logger = logging.getLogger(__name__)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


def create_app(*, store: CoreStore, artifact_root: Path, runtime_invoker: RuntimeInvoker | None = None, runtime_backend: CoreKnowledgeBackend | None = None) -> FastAPI:
    """创建 v3 API；生产调用方必须显式注入持久化仓储与 Artifact 根目录。"""

    service = store
    artifacts = ArtifactStore(artifact_root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(title="Muye Core", version="3.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable):
        request_id = _valid_request_id(request.headers.get("X-Request-Id")) or f"request_{token_hex(16)}"
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

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error("VALIDATION_ERROR", "请求参数无效", _request_id(request), 422)

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

    @app.post("/api/v3/agents/{agent_id}/invoke")
    async def invoke_agent(agent_id: str, request: RuntimeInvokeRequest, http_request: Request, principal: Principal = Depends(current_principal)) -> JSONResponse:
        if runtime_invoker is None:
            raise DomainError("AGENT_INACTIVE", "Agent Runtime 当前未配置", status_code=409)
        result = await runtime_invoker.invoke(principal, agent_id=agent_id, request_id=_request_id(http_request), session_id=request.session_id, task=request.task)
        return JSONResponse(result.model_dump(mode="json"))

    @app.post("/api/v3/chat/stream")
    async def chat_stream(request: ChatStreamRequest, http_request: Request, principal: Principal = Depends(current_principal)) -> StreamingResponse:
        """以稳定 Chat SSE 契约代理一次受控 Runtime 调用。"""

        if runtime_invoker is None:
            raise DomainError("AGENT_INACTIVE", "Agent Runtime 当前未配置", status_code=409)
        events = runtime_invoker.stream(
            principal,
            agent_id=request.agent_id,
            request_id=_request_id(http_request),
            session_id=request.session_id,
            task=request.task,
        )
        return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/internal/v1/runtime/retrieve")
    async def runtime_retrieve(payload: dict[str, object]) -> dict[str, object]:
        if runtime_backend is None:
            raise DomainError("RUNTIME_UNAVAILABLE", "Core 知识后端当前未配置", status_code=503)
        resource_id, query, top_k, pipeline = payload.get("resource_id"), payload.get("query"), payload.get("top_k"), payload.get("pipeline")
        if not isinstance(resource_id, str) or not isinstance(query, str) or not isinstance(top_k, int) or not isinstance(pipeline, str) or not 1 <= top_k <= 100:
            raise DomainError("VALIDATION_ERROR", "Runtime 检索请求无效", status_code=422)
        evidence = await runtime_backend.retrieve(resource_id=resource_id, query=query, top_k=top_k, pipeline=pipeline)
        return {"evidence": [{"citation": item.citation.model_dump(mode="json"), "content": item.content, "score": item.score} for item in evidence]}

    @app.post("/internal/v1/runtime/answer")
    async def runtime_answer(payload: dict[str, object]) -> dict[str, str]:
        if runtime_backend is None:
            raise DomainError("RUNTIME_UNAVAILABLE", "Core 知识后端当前未配置", status_code=503)
        system_instruction, task, raw_evidence, max_tokens = payload.get("system_instruction"), payload.get("task"), payload.get("evidence"), payload.get("max_tokens")
        if not isinstance(system_instruction, str) or not isinstance(task, str) or not isinstance(raw_evidence, list) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 65_536:
            raise DomainError("VALIDATION_ERROR", "Runtime 生成请求无效", status_code=422)
        try:
            evidence = [CoreEvidence(RuntimeCitationV1.model_validate(item["citation"]), item["content"], float(item["score"])) for item in raw_evidence if isinstance(item, dict) and isinstance(item.get("content"), str) and isinstance(item.get("score"), (int, float))]
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("VALIDATION_ERROR", "Runtime 证据格式无效", status_code=422) from exc
        if len(evidence) != len(raw_evidence):
            raise DomainError("VALIDATION_ERROR", "Runtime 证据格式无效", status_code=422)
        return {"content": await runtime_backend.answer(system_instruction=system_instruction, task=task, evidence=evidence, max_tokens=max_tokens)}

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
    async def create_user(http_request: Request, request: UserCreateRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        result = service.idempotent(
            f"user-create:{admin.user_id}", key, body,
            lambda: _audited_create_user(service, admin, body, _request_id(http_request)),
        )
        return JSONResponse(result.body, status_code=result.status_code)

    @app.put("/api/v3/users/{user_id}/agent-grants")
    async def replace_grants(http_request: Request, user_id: str, request: GrantReplaceRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        result = service.idempotent(f"grant-replace:{admin.user_id}:{user_id}", key, body, lambda: _audited_grants(service, admin, user_id, request.agent_ids, _request_id(http_request)))
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/agents", response_model=None, status_code=201)
    async def create_agent(http_request: Request, request: AgentCreateRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        def create() -> tuple[int, dict[str, object]]:
            agent, draft = service.create_agent(admin, **body)
            service.audit(actor_id=admin.user_id, action="create_agent", target_type="agent", target_id=agent.agent_id, request_id=_request_id(http_request))
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
    async def patch_draft(http_request: Request, agent_id: str, request: DraftPatchRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        result = service.idempotent(
            f"draft-patch:{agent_id}", key, body,
            lambda: _patch_draft(service, admin, agent_id, request, _request_id(http_request)),
        )
        return JSONResponse(result.body, status_code=result.status_code)

    @app.get("/api/v3/agents/{agent_id}/draft/impact", response_model=DraftImpactResponse)
    async def draft_impact(agent_id: str, admin: Principal = Depends(require_admin)) -> DraftImpactResponse:
        impact = service.draft_impact(admin, agent_id)
        return DraftImpactResponse(
            mode=impact.mode,
            base_revision_id=impact.base_revision_id,
            added_asset_ids=list(impact.added_asset_ids),
            removed_asset_ids=list(impact.removed_asset_ids),
            reusable_asset_ids=list(impact.reusable_asset_ids),
            evaluation_required=impact.evaluation_required,
            reasons=list(impact.reasons),
        )

    @app.post("/api/v3/agents/{agent_id}/profile-proposals", response_model=JobResponse, status_code=202)
    async def create_profile_proposal(http_request: Request, agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JobResponse:
        result = service.idempotent(
            f"profile-proposal:{agent_id}",
            key,
            {},
            lambda: _create_profile_proposal_job(service, admin, agent_id, key, _request_id(http_request)),
        )
        return JobResponse.model_validate(result.body)

    @app.delete("/api/v3/agents/{agent_id}/draft", status_code=204)
    async def discard_draft(http_request: Request, agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> Response:
        service.idempotent(f"draft-discard:{admin.user_id}:{agent_id}", key, {}, lambda: _discard_draft(service, admin, agent_id, _request_id(http_request)))
        return Response(status_code=204)

    @app.post("/api/v3/agents/{agent_id}/stop", response_model=None)
    async def stop_agent(http_request: Request, agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        result = service.idempotent(f"agent-stop:{admin.user_id}:{agent_id}", key, {}, lambda: _agent_action(service, admin, agent_id, "stop_agent", service.suspend, _request_id(http_request)))
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/agents/{agent_id}/archive", response_model=None)
    async def archive_agent(http_request: Request, agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        result = service.idempotent(f"agent-archive:{admin.user_id}:{agent_id}", key, {}, lambda: _agent_action(service, admin, agent_id, "archive_agent", service.archive, _request_id(http_request)))
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/agents/{agent_id}/restore", response_model=None)
    async def restore_agent(http_request: Request, agent_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        result = service.idempotent(f"agent-restore:{admin.user_id}:{agent_id}", key, {}, lambda: _agent_action(service, admin, agent_id, "restore_agent", service.restore, _request_id(http_request)))
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/agents/{agent_id}/sources", response_model=SourceUploadResponse, status_code=201)
    async def upload_source(http_request: Request, agent_id: str, file: UploadFile = File(...), key: IdempotencyKey = None, admin: Principal = Depends(require_admin)) -> SourceUploadResponse:
        if key is None:
            raise DomainError("VALIDATION_ERROR", "缺少 Idempotency-Key", status_code=422)
        service.agent_detail(agent_id)
        if file.content_type not in {"text/plain", "text/markdown", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
            raise DomainError("VALIDATION_ERROR", "不支持的文件类型", status_code=422)
        # UploadFile 已由 Starlette 落入受限临时文件；ArtifactStore 仍按固定块读取。
        stored = artifacts.store(file.file, filename=file.filename or "")
        body = {"sha256": stored.sha256, "size_bytes": stored.size_bytes, "media_type": file.content_type, "storage_key": stored.storage_key, "display_name": file.filename or ""}
        result = service.idempotent(f"asset-upload:{admin.user_id}:{agent_id}", key, body, lambda: _attach_asset(service, admin, agent_id, body, _request_id(http_request), stored.reused))
        return SourceUploadResponse.model_validate(result.body)

    @app.delete("/api/v3/agents/{agent_id}/sources/{asset_id}", status_code=204)
    async def remove_source(http_request: Request, agent_id: str, asset_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> Response:
        service.idempotent(
            f"asset-remove:{agent_id}:{asset_id}",
            key,
            {},
            lambda: _remove_source(service, admin, agent_id, asset_id, _request_id(http_request)),
        )
        return Response(status_code=204)

    @app.post("/api/v3/agents/{agent_id}/revisions", response_model=None, status_code=201)
    async def freeze_revision(http_request: Request, agent_id: str, request: RevisionFreezeRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        result = service.idempotent(
            f"revision-freeze:{agent_id}", key, body,
            lambda: _freeze_revision(service, admin, agent_id, request.draft_version, _request_id(http_request)),
        )
        return JSONResponse(result.body, status_code=result.status_code)

    @app.get("/api/v3/revisions/{revision_id}", response_model=RevisionResponse)
    async def revision_detail(revision_id: str, _admin: Principal = Depends(require_admin)) -> RevisionResponse:
        return RevisionResponse.model_validate(_revision(service.revision_detail(revision_id)))

    @app.post("/api/v3/revisions/{revision_id}/approve", response_model=None)
    async def approve_revision(http_request: Request, revision_id: str, request: RevisionApprovalRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        result = service.idempotent(
            f"revision-approve:{revision_id}", key, body,
            lambda: _approve_revision(service, admin, revision_id, request.checksum, _request_id(http_request)),
        )
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/revisions/{revision_id}/jobs", response_model=None, status_code=201)
    async def create_job(http_request: Request, revision_id: str, request: JobCreateRequest, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JSONResponse:
        body = request.model_dump(mode="json")
        result = service.idempotent(
            f"job-create:{revision_id}", key, body,
            lambda: _create_job(service, admin, revision_id, request.job_type, key, _request_id(http_request)),
        )
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/api/v3/revisions/{revision_id}/build", response_model=JobResponse, status_code=202)
    async def build_revision(http_request: Request, revision_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JobResponse:
        result = service.idempotent(
            f"revision-build:{revision_id}",
            key,
            {},
            lambda: _create_job(service, admin, revision_id, "BUILD", key, _request_id(http_request), status_code=202),
        )
        return JobResponse.model_validate(result.body)

    @app.get("/api/v3/revisions/{revision_id}/evaluation")
    async def revision_evaluation(revision_id: str, _admin: Principal = Depends(require_admin)) -> dict[str, object]:
        return service.revision_evaluation(revision_id)

    @app.get("/api/v3/jobs/{job_id}", response_model=JobResponse)
    async def job_detail(job_id: str, _admin: Principal = Depends(require_admin)) -> JobResponse:
        return JobResponse.model_validate(_job(service.job_detail(job_id)))

    @app.get("/api/v3/profile-proposals/{job_id}", response_model=ProfileProposalResponse)
    async def profile_proposal(job_id: str, _admin: Principal = Depends(require_admin)) -> ProfileProposalResponse:
        job = service.job_detail(job_id)
        if job.job_type != "PROFILE_PROPOSAL":
            raise DomainError("NOT_FOUND", "Profile Proposal Job 不存在", status_code=404)
        proposal = service.profile_proposal(job_id)
        return ProfileProposalResponse(
            job_id=job_id,
            status=job.status,
            proposal=proposal.model_dump(mode="json") if proposal is not None else None,
        )

    @app.post("/api/v3/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(http_request: Request, job_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JobResponse:
        result = service.idempotent(
            f"job-cancel:{job_id}", key, {},
            lambda: _cancel_job(service, admin, job_id, _request_id(http_request)),
        )
        return JobResponse.model_validate(result.body)

    @app.post("/api/v3/jobs/{job_id}/retry", response_model=JobResponse, status_code=201)
    async def retry_job(http_request: Request, job_id: str, key: IdempotencyKey, admin: Principal = Depends(require_admin)) -> JobResponse:
        result = service.idempotent(
            f"job-retry:{job_id}",
            key,
            {},
            lambda: _retry_job(service, admin, job_id, key, _request_id(http_request)),
        )
        return JobResponse.model_validate(result.body)

    @app.get("/api/v3/jobs/{job_id}/events")
    async def job_events(job_id: str, last_event_id: int = Header(default=-1, alias="Last-Event-ID"), _admin: Principal = Depends(require_admin)) -> Response:
        if last_event_id < -1:
            raise DomainError("VALIDATION_ERROR", "Last-Event-ID 无效", status_code=422)
        events = service.job_events(job_id, after_sequence=last_event_id)

        payload = "".join(
            f"id: {event.sequence}\nevent: {event.event_type}\ndata: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            for event in events
        )
        return Response(content=payload, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def _bearer_token(authorization: str | None) -> str:
    prefix = "Bearer "
    return authorization[len(prefix):].strip() if authorization and authorization.startswith(prefix) else ""


def _request_id(request: Request) -> str:
    """为错误响应保留调用方相关 ID，成功响应由后续持久化中间件统一覆盖。"""

    return _valid_request_id(request.headers.get("X-Request-Id")) or f"request_{token_hex(16)}"


def _valid_request_id(value: str | None) -> str | None:
    if value and value.startswith("request_") and len(value) == 40 and all(character in "0123456789abcdef" for character in value[8:]):
        return value
    return None


def _error(code: str, message: str, request_id: str, status_code: int) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "request_id": request_id}, status_code=status_code)


def _chat_stream_events(session_id: str, result: object):
    """将 Runtime 响应投影为 v3 Chat SSE，避免公开内部响应字段。"""

    sequence = 0
    yield _encode_chat_event(ChatStreamEventV1(schema_version="muye.ai/chat-stream-event/v1", event_type="session_start", sequence=sequence, session_id=session_id))
    sequence += 1
    if result.status == "success":
        if result.content:
            yield _encode_chat_event(ChatStreamEventV1(schema_version="muye.ai/chat-stream-event/v1", event_type="block_delta", sequence=sequence, session_id=session_id, block_id="block_0000000000000001", delta=result.content))
            sequence += 1
        yield _encode_chat_event(ChatStreamEventV1(schema_version="muye.ai/chat-stream-event/v1", event_type="done", sequence=sequence, session_id=session_id, citations=result.citations or [], total_tokens=0))
    else:
        yield _encode_chat_event(ChatStreamEventV1(schema_version="muye.ai/chat-stream-event/v1", event_type="error", sequence=sequence, session_id=session_id, error_code=result.error_code or "RUNTIME_ERROR", message=result.error_message or "请求失败。"))
    sequence += 1
    yield _encode_chat_event(ChatStreamEventV1(schema_version="muye.ai/chat-stream-event/v1", event_type="session_end", sequence=sequence, session_id=session_id))


def _encode_chat_event(event: ChatStreamEventV1) -> str:
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event_type}\ndata: {payload}\n\n"


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


def _audited_create_user(service: CoreStore, admin: Principal, body: dict[str, object], request_id: str) -> tuple[int, dict[str, object]]:
    principal = service.create_user(str(body["username"]), str(body["password"]))
    service.audit(actor_id=admin.user_id, action="create_user", target_type="user", target_id=principal.user_id, request_id=request_id)
    return 201, _me(principal).model_dump(mode="json")


def _audited_grants(service: CoreStore, admin: Principal, user_id: str, agent_ids: list[str], request_id: str) -> tuple[int, dict[str, object]]:
    grants = service.replace_grants(admin, user_id, agent_ids)
    service.audit(actor_id=admin.user_id, action="replace_agent_grants", target_type="user", target_id=user_id, request_id=request_id, details={"agent_count": len(grants)})
    return 200, {"user_id": user_id, "agent_ids": sorted(grants)}


def _patch_draft(service: CoreStore, admin: Principal, agent_id: str, request: DraftPatchRequest, request_id: str) -> tuple[int, dict[str, object]]:
    result = _draft(service.patch_draft(admin, agent_id, request.version, request.config))
    service.audit(actor_id=admin.user_id, action="update_draft", target_type="agent", target_id=agent_id, request_id=request_id)
    return 200, result


def _discard_draft(service: CoreStore, admin: Principal, agent_id: str, request_id: str) -> tuple[int, dict[str, object]]:
    service.discard_draft(admin, agent_id)
    service.audit(actor_id=admin.user_id, action="discard_draft", target_type="agent", target_id=agent_id, request_id=request_id)
    return 204, {}


def _agent_action(service: CoreStore, admin: Principal, agent_id: str, action: str, operation: Callable[[Principal, str], object], request_id: str) -> tuple[int, dict[str, object]]:
    agent = operation(admin, agent_id)
    service.audit(actor_id=admin.user_id, action=action, target_type="agent", target_id=agent_id, request_id=request_id)
    return 200, _summary(agent).model_dump(mode="json")


def _attach_asset(service: CoreStore, admin: Principal, agent_id: str, body: dict[str, object], request_id: str, reused: bool) -> tuple[int, dict[str, object]]:
    asset_id, already_registered = service.attach_asset(admin, agent_id, **body)
    service.audit(actor_id=admin.user_id, action="upload_source", target_type="asset", target_id=asset_id, request_id=request_id, details={"agent_id": agent_id, "sha256": body["sha256"], "reused": reused or already_registered})
    return 201, {"asset_id": asset_id, "sha256": body["sha256"], "size_bytes": body["size_bytes"], "media_type": body["media_type"], "display_name": body["display_name"], "reused": reused or already_registered}


def _remove_source(service: CoreStore, admin: Principal, agent_id: str, asset_id: str, request_id: str) -> tuple[int, dict[str, object]]:
    service.remove_draft_asset(admin, agent_id, asset_id)
    service.audit(actor_id=admin.user_id, action="remove_source", target_type="asset", target_id=asset_id, request_id=request_id, details={"agent_id": agent_id})
    return 204, {}


def _revision(record: object) -> dict[str, object]:
    """将不可变领域 Revision 映射为公开 API 响应。"""

    return {
        "revision_id": record.revision_id,
        "agent_id": record.agent_id,
        "revision_number": record.revision_number,
        "checksum": record.checksum,
        "status": record.status,
        "spec": record.spec.model_dump(mode="json"),
    }


def _freeze_revision(service: CoreStore, admin: Principal, agent_id: str, draft_version: int, request_id: str) -> tuple[int, dict[str, object]]:
    record = service.freeze_revision(admin, agent_id, draft_version)
    service.audit(actor_id=admin.user_id, action="freeze_revision", target_type="revision", target_id=record.revision_id, request_id=request_id, details={"agent_id": agent_id, "checksum": record.checksum})
    return 201, _revision(record)


def _approve_revision(service: CoreStore, admin: Principal, revision_id: str, checksum: str, request_id: str) -> tuple[int, dict[str, object]]:
    record = service.approve_revision(admin, revision_id, checksum)
    service.audit(actor_id=admin.user_id, action="approve_revision", target_type="revision", target_id=record.revision_id, request_id=request_id, details={"checksum": record.checksum})
    return 200, _revision(record)


def _job(record: object) -> dict[str, object]:
    """映射 Job 公开状态，刻意不暴露 Worker lease owner。"""

    return {
        "job_id": record.job_id,
        "job_type": record.job_type,
        "revision_id": record.revision_id,
        "status": record.status,
        "attempt": record.attempt,
        "error_code": record.error_code,
    }


def _create_job(service: CoreStore, admin: Principal, revision_id: str, job_type: str, idempotency_key: str, request_id: str, *, status_code: int = 201) -> tuple[int, dict[str, object]]:
    record = service.create_job(admin, revision_id=revision_id, job_type=job_type, idempotency_key=idempotency_key)
    service.audit(actor_id=admin.user_id, action="create_job", target_type="job", target_id=record.job_id, request_id=request_id, details={"revision_id": revision_id, "job_type": job_type})
    return status_code, _job(record)


def _cancel_job(service: CoreStore, admin: Principal, job_id: str, request_id: str) -> tuple[int, dict[str, object]]:
    record = service.request_job_cancel(admin, job_id)
    service.audit(actor_id=admin.user_id, action="cancel_job", target_type="job", target_id=job_id, request_id=request_id)
    return 200, _job(record)


def _retry_job(service: CoreStore, admin: Principal, job_id: str, idempotency_key: str, request_id: str) -> tuple[int, dict[str, object]]:
    record = service.retry_job(admin, job_id, idempotency_key=idempotency_key)
    service.audit(
        actor_id=admin.user_id,
        action="retry_job",
        target_type="job",
        target_id=record.job_id,
        request_id=request_id,
        details={"source_job_id": job_id, "attempt": record.attempt},
    )
    return 201, _job(record)


def _create_profile_proposal_job(service: CoreStore, admin: Principal, agent_id: str, idempotency_key: str, request_id: str) -> tuple[int, dict[str, object]]:
    record = service.create_profile_proposal_job(admin, agent_id, idempotency_key=idempotency_key)
    service.audit(actor_id=admin.user_id, action="create_profile_proposal", target_type="job", target_id=record.job_id, request_id=request_id, details={"agent_id": agent_id})
    return 202, _job(record)
