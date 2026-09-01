"""阶段 1 Core 的可注入领域服务与内存测试仓储。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from secrets import token_hex, token_urlsafe
from threading import RLock
from typing import Any, Callable

from contracts.v3 import AgentRevisionSpecV1

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


_PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)


class DomainError(Exception):
    """业务错误；路由层将其映射为稳定的公开错误码。"""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    username: str
    is_admin: bool


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(slots=True)
class _User:
    principal: Principal
    password_hash: str
    active: bool = True


@dataclass(slots=True)
class _Session:
    user_id: str
    access_hash: str
    refresh_hash: str
    expires_at: datetime


@dataclass(slots=True)
class AgentRecord:
    agent_id: str
    slug: str
    display_name: str
    description: str
    created_by: str
    archived_at: datetime | None = None
    suspended_at: datetime | None = None


@dataclass(slots=True)
class DraftRecord:
    agent_id: str
    version: int
    config: dict[str, object]
    updated_by: str


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """一个已冻结的 Agent 配置及其审批状态。

    ``spec`` 和 ``checksum`` 创建后不可改变；状态只能依次进入审阅、批准及后续的
    构建状态。阶段 2 的构建 Worker 只能接受 ``APPROVED`` Revision。
    """

    revision_id: str
    agent_id: str
    revision_number: int
    checksum: str
    spec: AgentRevisionSpecV1
    status: str
    approved_by: str | None = None


@dataclass(frozen=True, slots=True)
class IdempotentResponse:
    request_checksum: str
    status_code: int
    body: dict[str, object]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """不含凭据或资料正文的追加审计投影。"""

    actor_id: str | None
    action: str
    target_type: str
    target_id: str
    request_id: str
    details: dict[str, object]


class CoreStore:
    """阶段 1 服务所需的最小持久化接口。"""

    def readiness(self) -> None:
        raise NotImplementedError

    def bootstrap_admin(self, username: str, password: str) -> Principal:
        raise NotImplementedError


class InMemoryCoreStore(CoreStore):
    """锁保护的测试仓储，镜像阶段 1 API 的冲突和授权语义。"""

    def __init__(self, *, session_ttl_seconds: int = 900) -> None:
        self._ttl = timedelta(seconds=session_ttl_seconds)
        self._users: dict[str, _User] = {}
        self._by_username: dict[str, str] = {}
        self._sessions: dict[str, _Session] = {}
        self._agents: dict[str, AgentRecord] = {}
        self._agent_by_slug: dict[str, str] = {}
        self._drafts: dict[str, DraftRecord] = {}
        self._grants: dict[str, frozenset[str]] = {}
        self._idempotency: dict[tuple[str, str], IdempotentResponse] = {}
        self._assets: dict[str, dict[str, object]] = {}
        self._draft_sources: dict[str, list[dict[str, object]]] = {}
        self._revisions: dict[str, RevisionRecord] = {}
        self._jobs: dict[str, object] = {}
        self._jobs_by_key: dict[tuple[str, str, str], str] = {}
        self._job_events: dict[str, list[object]] = {}
        self._ready_revisions: dict[str, dict[str, object]] = {}
        self._audit_events: list[AuditEvent] = []
        self._lock = RLock()

    def readiness(self) -> None:
        return None

    def bootstrap_admin(self, username: str, password: str) -> Principal:
        with self._lock:
            if self._users:
                raise DomainError("CONFLICT", "Core 已初始化", status_code=409)
            return self._create_user(username, password, is_admin=True)

    def create_user(self, username: str, password: str) -> Principal:
        with self._lock:
            return self._create_user(username, password, is_admin=False)

    def _create_user(self, username: str, password: str, *, is_admin: bool) -> Principal:
        normalized = _username(username)
        _password(password)
        if normalized in self._by_username:
            raise DomainError("CONFLICT", "用户名已存在")
        principal = Principal(f"usr_{token_urlsafe(12)}", normalized, is_admin)
        self._users[principal.user_id] = _User(principal, _PASSWORD_HASHER.hash(password))
        self._by_username[normalized] = principal.user_id
        return principal

    def login(self, username: str, password: str) -> TokenPair:
        with self._lock:
            user = self._users.get(self._by_username.get(_username(username), ""))
            if user is None or not user.active or not _verify(user.password_hash, password):
                raise DomainError("AUTHENTICATION_ERROR", "用户名或密码错误", status_code=401)
            return self._issue_session(user.principal.user_id)

    def refresh(self, refresh_token: str) -> TokenPair:
        with self._lock:
            key = next((key for key, value in self._sessions.items() if value.refresh_hash == _hash(refresh_token)), None)
            if key is None:
                raise DomainError("AUTHENTICATION_ERROR", "refresh session 无效", status_code=401)
            session = self._sessions.pop(key)
            user = self._users.get(session.user_id)
            if user is None or not user.active or session.expires_at <= _now():
                raise DomainError("AUTHENTICATION_ERROR", "refresh session 无效", status_code=401)
            return self._issue_session(session.user_id)

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_hash(token), None)

    def principal(self, token: str) -> Principal | None:
        with self._lock:
            session = self._sessions.get(_hash(token))
            if session is None or session.expires_at <= _now():
                return None
            user = self._users.get(session.user_id)
            return user.principal if user is not None and user.active else None

    def replace_grants(self, actor: Principal, user_id: str, agent_ids: list[str]) -> frozenset[str]:
        with self._lock:
            if not actor.is_admin:
                raise DomainError("AUTHORIZATION_ERROR", "需要管理员权限", status_code=403)
            if user_id not in self._users:
                raise DomainError("NOT_FOUND", "用户不存在", status_code=404)
            if any(agent_id not in self._agents or self._agents[agent_id].archived_at for agent_id in agent_ids):
                raise DomainError("VALIDATION_ERROR", "包含未知或已归档 Agent", status_code=422)
            self._grants[user_id] = frozenset(agent_ids)
            return self._grants[user_id]

    def create_agent(self, actor: Principal, *, slug: str, display_name: str, description: str, config: dict[str, object]) -> tuple[AgentRecord, DraftRecord]:
        self._admin(actor)
        with self._lock:
            if slug in self._agent_by_slug:
                raise DomainError("CONFLICT", "slug 已永久保留")
            agent = AgentRecord(f"agent_{token_hex(16)}", slug, display_name.strip(), description.strip(), actor.user_id)
            draft = DraftRecord(agent.agent_id, 1, config, actor.user_id)
            self._agents[agent.agent_id] = agent
            self._agent_by_slug[slug] = agent.agent_id
            self._drafts[agent.agent_id] = draft
            return agent, draft

    def agents_page(self, cursor: str | None, limit: int) -> tuple[list[AgentRecord], str | None]:
        with self._lock:
            records = sorted(self._agents.values(), key=lambda value: value.agent_id)
            start = next((index + 1 for index, value in enumerate(records) if value.agent_id == cursor), 0) if cursor else 0
            page = records[start : start + limit]
            return page, page[-1].agent_id if len(records) > start + limit else None

    def agent_detail(self, agent_id: str) -> tuple[AgentRecord, DraftRecord | None]:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise DomainError("NOT_FOUND", "Agent 不存在", status_code=404)
            return agent, self._drafts.get(agent_id)

    def patch_draft(self, actor: Principal, agent_id: str, version: int, config: dict[str, object]) -> DraftRecord:
        self._admin(actor)
        with self._lock:
            agent, draft = self.agent_detail(agent_id)
            if agent.archived_at or agent.suspended_at:
                raise DomainError("CONFLICT", "Agent 当前不可编辑")
            if draft is None:
                raise DomainError("NOT_FOUND", "开放 Draft 不存在", status_code=404)
            if draft.version != version:
                raise DomainError("VERSION_CONFLICT", "Draft 已被其他请求更新")
            draft.version += 1
            draft.config = config
            draft.updated_by = actor.user_id
            return draft

    def discard_draft(self, actor: Principal, agent_id: str) -> None:
        self._admin(actor)
        with self._lock:
            self.agent_detail(agent_id)
            self._drafts.pop(agent_id, None)

    def suspend(self, actor: Principal, agent_id: str) -> AgentRecord:
        self._admin(actor)
        with self._lock:
            agent, _ = self.agent_detail(agent_id)
            if agent.archived_at:
                raise DomainError("CONFLICT", "已归档 Agent 不能停用")
            agent.suspended_at = agent.suspended_at or _now()
            return agent

    def archive(self, actor: Principal, agent_id: str) -> AgentRecord:
        self._admin(actor)
        with self._lock:
            agent, _ = self.agent_detail(agent_id)
            agent.archived_at = agent.archived_at or _now()
            self._drafts.pop(agent_id, None)
            for user_id, grants in tuple(self._grants.items()):
                self._grants[user_id] = grants - {agent_id}
            return agent

    def restore(self, actor: Principal, agent_id: str) -> AgentRecord:
        self._admin(actor)
        with self._lock:
            agent, _ = self.agent_detail(agent_id)
            if agent.archived_at is None:
                raise DomainError("CONFLICT", "Agent 尚未归档")
            agent.archived_at = None
            return agent

    def attach_asset(self, actor: Principal, agent_id: str, *, sha256: str, size_bytes: int, media_type: str, storage_key: str, display_name: str) -> tuple[str, bool]:
        """登记内容寻址 Asset，并将其绑定到当前开放 Draft。"""

        self._admin(actor)
        with self._lock:
            agent, draft = self.agent_detail(agent_id)
            if agent.archived_at or agent.suspended_at or draft is None:
                raise DomainError("CONFLICT", "Agent 当前不能接收资料")
            asset_id = f"asset_{sha256[:16]}"
            reused = asset_id in self._assets
            self._assets.setdefault(asset_id, {"sha256": sha256, "size_bytes": size_bytes, "media_type": media_type, "storage_key": storage_key})
            sources = self._draft_sources.setdefault(agent_id, [])
            if not any(source["asset_id"] == asset_id for source in sources):
                sources.append({"asset_id": asset_id, "display_name": display_name, "sort_order": len(sources)})
                draft.version += 1
            return asset_id, reused

    def freeze_revision(self, actor: Principal, agent_id: str, draft_version: int) -> RevisionRecord:
        """在 Draft 乐观锁与资料快照同时成立时创建待审 Revision。"""

        self._admin(actor)
        from .revisions import freeze_revision_spec

        with self._lock:
            agent, draft = self.agent_detail(agent_id)
            if agent.archived_at or agent.suspended_at or draft is None:
                raise DomainError("CONFLICT", "Agent 当前不能冻结 Revision")
            if draft.version != draft_version:
                raise DomainError("VERSION_CONFLICT", "Draft 已被其他请求更新")
            sources = self._draft_sources.get(agent_id, [])
            if not sources:
                raise DomainError("VALIDATION_ERROR", "Revision 至少需要一份资料", status_code=422)
            frozen_sources = []
            for source in sources:
                asset = self._assets[source["asset_id"]]
                frozen_sources.append(
                    {
                        "asset_id": source["asset_id"],
                        "sha256": asset["sha256"],
                        "display_name": source["display_name"],
                    }
                )
            revision_number = 1 + max(
                (record.revision_number for record in self._revisions.values() if record.agent_id == agent_id),
                default=0,
            )
            spec, checksum = freeze_revision_spec(
                agent_id=agent_id,
                revision_id=f"revision_{token_hex(16)}",
                revision_number=revision_number,
                draft_config=draft.config,
                sources=frozen_sources,
            )
            record = RevisionRecord(
                revision_id=spec.revision_id,
                agent_id=agent_id,
                revision_number=revision_number,
                checksum=checksum,
                spec=spec,
                status="REVIEW_REQUIRED",
            )
            self._revisions[record.revision_id] = record
            return record

    def approve_revision(self, actor: Principal, revision_id: str, checksum: str) -> RevisionRecord:
        """批准指定 checksum 的 Revision，防止审阅结果被资料或配置漂移复用。"""

        self._admin(actor)
        with self._lock:
            record = self.revision_detail(revision_id)
            if record.status != "REVIEW_REQUIRED":
                raise DomainError("CONFLICT", "Revision 当前不可审批")
            if record.checksum != checksum:
                raise DomainError("CONFLICT", "Revision checksum 与待审批版本不一致")
            approved = replace(record, status="APPROVED", approved_by=actor.user_id)
            self._revisions[revision_id] = approved
            return approved

    def revision_detail(self, revision_id: str) -> RevisionRecord:
        """读取已冻结 Revision；调用方不能通过此接口修改其内容。"""

        record = self._revisions.get(revision_id)
        if record is None:
            raise DomainError("NOT_FOUND", "Revision 不存在", status_code=404)
        return record

    def create_job(self, actor: Principal, *, revision_id: str, job_type: str, idempotency_key: str):
        """创建或返回同一幂等键的构建/评测 Job。"""

        self._admin(actor)
        from .jobs import JobRecord

        if job_type not in {"BUILD", "EVALUATE"}:
            raise DomainError("VALIDATION_ERROR", "Job 类型无效", status_code=422)
        if not idempotency_key or len(idempotency_key) > 128:
            raise DomainError("VALIDATION_ERROR", "Job 幂等键无效", status_code=422)
        with self._lock:
            revision = self.revision_detail(revision_id)
            if job_type == "BUILD" and revision.status != "APPROVED":
                raise DomainError("CONFLICT", "只有已审批 Revision 可以构建")
            key = (job_type, revision_id, idempotency_key)
            previous_id = self._jobs_by_key.get(key)
            if previous_id:
                return self._jobs[previous_id]
            job = JobRecord(f"job_{token_hex(16)}", job_type, revision_id, idempotency_key, "PENDING", 1)
            self._jobs[job.job_id] = job
            self._jobs_by_key[key] = job.job_id
            self._job_events[job.job_id] = []
            return job

    def job_detail(self, job_id: str):
        """读取 Job 当前状态，未知 ID 使用统一的 not-found 错误。"""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DomainError("NOT_FOUND", "Job 不存在", status_code=404)
            return job

    def claim_job(self, *, worker_id: str, lease_seconds: int = 60, job_types: frozenset[str] | None = None):
        """领取最早可执行 Job，并为 Worker 建立有限 lease。"""

        from .jobs import claim

        with self._lock:
            for job_id in sorted(self._jobs):
                if job_types is not None and self._jobs[job_id].job_type not in job_types:
                    continue
                try:
                    claimed = claim(self._jobs[job_id], worker_id=worker_id, now=_now(), lease_seconds=lease_seconds)
                except DomainError:
                    continue
                self._jobs[job_id] = claimed
                self._append_job_event_locked(job_id, event_type="started", stage="claimed")
                return claimed
            return None

    def request_job_cancel(self, actor: Principal, job_id: str):
        """请求协作式取消，实际清理由当前 Worker 在安全检查点完成。"""

        self._admin(actor)
        from .jobs import request_cancel

        with self._lock:
            updated = request_cancel(self.job_detail(job_id))
            self._jobs[job_id] = updated
            return updated

    def complete_job(self, *, worker_id: str, job_id: str, status: str, error_code: str | None = None):
        """收敛 Worker Job，并追加唯一的终态事件。"""

        from .jobs import complete

        with self._lock:
            updated = complete(self.job_detail(job_id), worker_id=worker_id, status=status, error_code=error_code)
            self._jobs[job_id] = updated
            terminal_event = {"SUCCEEDED": "completed", "FAILED": "failed", "CANCELLED": "cancelled"}[status]
            self._append_job_event_locked(job_id, event_type=terminal_event, stage="finished", error_code=error_code)
            return updated

    def job_events(self, job_id: str, after_sequence: int = -1) -> list[object]:
        """返回可由 Last-Event-ID 恢复的追加事件，不返回内部日志。"""

        with self._lock:
            self.job_detail(job_id)
            return [event for event in self._job_events[job_id] if event.sequence > after_sequence]

    def publish_revision_ready(self, *, worker_id: str, job_id: str, revision_id: str, build_id: str, bundle_checksum: str, report_ref: str, storage_key: str, collection_name: str, collection_checksum: str, pass_rate: float) -> RevisionRecord:
        """以当前 BUILD Job 的 lease 原子发布 Bundle、评测证据和 READY Revision。"""

        with self._lock:
            from .jobs import complete

            if not isfinite(pass_rate) or not 0 <= pass_rate <= 1:
                raise DomainError("VALIDATION_ERROR", "评测通过率必须是 0 到 1 之间的有限数值", status_code=422)
            job = self.job_detail(job_id)
            if job.revision_id != revision_id or job.job_type != "BUILD":
                raise DomainError("CONFLICT", "Job 与 Revision 不匹配")
            if job.status == "CANCEL_REQUESTED":
                raise DomainError("CONFLICT", "Job 已请求取消")
            if job.status != "RUNNING" or job.lease_owner != worker_id:
                raise DomainError("CONFLICT", "Worker 不持有 Job lease")
            record = self.revision_detail(revision_id)
            if record.status != "APPROVED":
                raise DomainError("CONFLICT", "Revision 当前不可标记为 READY")
            self._ready_revisions[revision_id] = {"build_id": build_id, "bundle_checksum": bundle_checksum, "report_ref": report_ref, "storage_key": storage_key, "collection_name": collection_name, "collection_checksum": collection_checksum, "pass_rate": pass_rate}
            ready = replace(record, status="READY")
            self._revisions[revision_id] = ready
            self._jobs[job_id] = complete(job, worker_id=worker_id, status="SUCCEEDED")
            self._append_job_event_locked(job_id, event_type="completed", stage="finished")
            return ready

    def _append_job_event_locked(self, job_id: str, *, event_type: str, stage: str, error_code: str | None = None) -> None:
        """在调用方持锁时按连续序号追加经契约校验的事件。"""

        from .jobs import new_event

        events = self._job_events[job_id]
        events.append(new_event(job_id=job_id, sequence=len(events), event_type=event_type, stage=stage, error_code=error_code))

    def audit(self, *, actor_id: str | None, action: str, target_type: str, target_id: str, request_id: str, details: dict[str, object] | None = None) -> None:
        """记录已经脱敏的领域写操作；调用方不得传入密码、Token 或资料正文。"""

        with self._lock:
            self._audit_events.append(AuditEvent(actor_id, action, target_type, target_id, request_id, details or {}))

    def idempotent(self, scope: str, key: str, request_body: dict[str, object], create: Callable[[], tuple[int, dict[str, object]]]) -> IdempotentResponse:
        checksum = _hash_json(request_body)
        with self._lock:
            previous = self._idempotency.get((scope, key))
            if previous:
                if previous.request_checksum != checksum:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "幂等键已绑定到不同请求")
                return previous
            status_code, body = create()
            response = IdempotentResponse(checksum, status_code, body)
            self._idempotency[(scope, key)] = response
            return response

    def _issue_session(self, user_id: str) -> TokenPair:
        access, refresh = token_urlsafe(32), token_urlsafe(48)
        pair = TokenPair(access, refresh, _now() + self._ttl)
        self._sessions[_hash(access)] = _Session(user_id, _hash(access), _hash(refresh), pair.expires_at)
        return pair

    @staticmethod
    def _admin(principal: Principal) -> None:
        if not principal.is_admin:
            raise DomainError("AUTHORIZATION_ERROR", "需要管理员权限", status_code=403)


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value: dict[str, object]) -> str:
    import json
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _username(value: str) -> str:
    normalized = value.strip()
    if not 3 <= len(normalized) <= 128:
        raise DomainError("VALIDATION_ERROR", "用户名长度无效", status_code=422)
    return normalized


def _password(value: str) -> None:
    if not 12 <= len(value) <= 1024:
        raise DomainError("VALIDATION_ERROR", "密码长度无效", status_code=422)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False
