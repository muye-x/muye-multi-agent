"""阶段 1 Core 的 PostgreSQL 事实源适配器。"""

from __future__ import annotations

import json
from datetime import timedelta
from math import isfinite
from secrets import token_hex, token_urlsafe
from typing import Any, Callable

from contracts.v3 import AgentRevisionSpecV1

from .service import (
    AgentRecord,
    CoreStore,
    DomainError,
    DraftRecord,
    IdempotentResponse,
    Principal,
    RevisionAssetRecord,
    RevisionRecord,
    TokenPair,
    _PASSWORD_HASHER,
    _hash,
    _hash_json,
    _now,
    _password,
    _username,
    _verify,
)


class PostgresCoreStore(CoreStore):
    """每次操作使用短事务的 v3 Core PostgreSQL 仓储。"""

    def __init__(self, database_url: str, *, session_ttl_seconds: int = 900) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("MUYE_CORE_DATABASE_URL 必须是 PostgreSQL URL")
        self._database_url = database_url
        self._ttl_seconds = session_ttl_seconds

    def readiness(self) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM muye_schema_migrations WHERE version = 1")
            if cursor.fetchone() is None:
                raise RuntimeError("v3 Core 数据库迁移尚未应用")

    def bootstrap_admin(self, username: str, password: str) -> Principal:
        normalized = _username(username)
        _password(password)
        principal = Principal(f"usr_{token_urlsafe(12)}", normalized, True)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM core_users")
            if cursor.fetchone()[0]:
                raise DomainError("CONFLICT", "Core 已初始化")
            cursor.execute("INSERT INTO core_users (user_id, username, password_hash, is_admin) VALUES (%s, %s, %s, TRUE)", (principal.user_id, principal.username, _PASSWORD_HASHER.hash(password)))
        return principal

    def create_user(self, username: str, password: str) -> Principal:
        normalized = _username(username)
        _password(password)
        principal = Principal(f"usr_{token_urlsafe(12)}", normalized, False)
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO core_users (user_id, username, password_hash, is_admin) VALUES (%s, %s, %s, FALSE)", (principal.user_id, normalized, _PASSWORD_HASHER.hash(password)))
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise DomainError("CONFLICT", "用户名已存在") from exc
            raise
        return principal

    def login(self, username: str, password: str) -> TokenPair:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT user_id, password_hash, is_admin, active FROM core_users WHERE username = %s", (_username(username),))
            row = cursor.fetchone()
            if row is None or not row[3] or not _verify(row[1], password):
                raise DomainError("AUTHENTICATION_ERROR", "用户名或密码错误", status_code=401)
            return self._issue_session(cursor, row[0])

    def refresh(self, refresh_token: str) -> TokenPair:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_sessions WHERE refresh_hash = %s RETURNING user_id, expires_at", (_hash(refresh_token),))
            row = cursor.fetchone()
            if row is None or row[1] <= _now() or not self._active(cursor, row[0]):
                raise DomainError("AUTHENTICATION_ERROR", "refresh session 无效", status_code=401)
            return self._issue_session(cursor, row[0])

    def logout(self, token: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_sessions WHERE access_hash = %s", (_hash(token),))

    def principal(self, token: str) -> Principal | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT u.user_id, u.username, u.is_admin FROM core_sessions s JOIN core_users u ON u.user_id = s.user_id WHERE s.access_hash = %s AND s.expires_at > now() AND u.active", (_hash(token),))
            row = cursor.fetchone()
            return Principal(*row) if row else None

    def replace_grants(self, actor: Principal, user_id: str, agent_ids: list[str]) -> frozenset[str]:
        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            if not self._active(cursor, user_id):
                raise DomainError("NOT_FOUND", "用户不存在", status_code=404)
            cursor.execute("SELECT agent_id FROM agents WHERE agent_id = ANY(%s) AND archived_at IS NULL", (agent_ids,))
            if len(cursor.fetchall()) != len(set(agent_ids)):
                raise DomainError("VALIDATION_ERROR", "包含未知或已归档 Agent", status_code=422)
            cursor.execute("DELETE FROM user_agent_grants WHERE user_id = %s", (user_id,))
            for agent_id in sorted(set(agent_ids)):
                cursor.execute("INSERT INTO user_agent_grants (user_id, agent_id, created_by) VALUES (%s, %s, %s)", (user_id, agent_id, actor.user_id))
        return frozenset(agent_ids)

    def has_grant(self, user_id: str, agent_id: str) -> bool:
        """以数据库事实表实时复核调用 grant，不缓存授权决定。"""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM user_agent_grants WHERE user_id = %s AND agent_id = %s", (user_id, agent_id))
            return cursor.fetchone() is not None

    def create_agent(self, actor: Principal, *, slug: str, display_name: str, description: str, config: dict[str, object]) -> tuple[AgentRecord, DraftRecord]:
        self._admin(actor)
        agent = AgentRecord(f"agent_{token_hex(16)}", slug, display_name.strip(), description.strip(), actor.user_id)
        draft = DraftRecord(agent.agent_id, 1, config, actor.user_id)
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO agents (agent_id, slug, display_name, description, created_by) VALUES (%s, %s, %s, %s, %s)", (agent.agent_id, agent.slug, agent.display_name, agent.description, actor.user_id))
                cursor.execute("INSERT INTO agent_drafts (agent_id, config_json, updated_by) VALUES (%s, %s::jsonb, %s)", (agent.agent_id, json.dumps(config), actor.user_id))
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise DomainError("CONFLICT", "slug 已永久保留") from exc
            raise
        return agent, draft

    def agents_page(self, cursor: str | None, limit: int) -> tuple[list[AgentRecord], str | None]:
        with self._connection() as connection, connection.cursor() as db_cursor:
            db_cursor.execute("SELECT agent_id, slug, display_name, description, created_by, archived_at, suspended_at FROM agents WHERE (%s::text IS NULL OR agent_id > %s) ORDER BY agent_id LIMIT %s", (cursor, cursor, limit + 1))
            rows = [AgentRecord(*row) for row in db_cursor.fetchall()]
        return rows[:limit], rows[limit - 1].agent_id if len(rows) > limit else None

    def agent_detail(self, agent_id: str) -> tuple[AgentRecord, DraftRecord | None]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT agent_id, slug, display_name, description, created_by, archived_at, suspended_at FROM agents WHERE agent_id = %s", (agent_id,))
            row = cursor.fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Agent 不存在", status_code=404)
            cursor.execute("SELECT agent_id, version, config_json, updated_by FROM agent_drafts WHERE agent_id = %s", (agent_id,))
            draft = cursor.fetchone()
            return AgentRecord(*row), DraftRecord(*draft) if draft else None

    def patch_draft(self, actor: Principal, agent_id: str, version: int, config: dict[str, object]) -> DraftRecord:
        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_drafts d SET config_json=%s::jsonb, version=version+1, updated_by=%s, updated_at=now() FROM agents a WHERE d.agent_id=%s AND a.agent_id=d.agent_id AND a.archived_at IS NULL AND a.suspended_at IS NULL AND d.version=%s RETURNING d.agent_id, d.version, d.config_json, d.updated_by", (json.dumps(config), actor.user_id, agent_id, version))
            row = cursor.fetchone()
            if row is None:
                raise DomainError("VERSION_CONFLICT", "Draft 不存在、不可编辑或已被更新")
            return DraftRecord(*row)

    def draft_impact(self, actor: Principal, agent_id: str):
        """在同一快照中比较开放 Draft、资料和最近冻结 Revision。"""

        self._admin(actor)
        from .impact import analyze_draft_impact

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT config_json FROM agent_drafts WHERE agent_id = %s", (agent_id,))
            draft = cursor.fetchone()
            if draft is None:
                raise DomainError("NOT_FOUND", "开放 Draft 不存在", status_code=404)
            cursor.execute(
                "SELECT asset_id FROM draft_sources WHERE agent_id = %s ORDER BY sort_order",
                (agent_id,),
            )
            source_ids = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                "SELECT revision_id FROM agent_revisions WHERE agent_id = %s ORDER BY revision_number DESC LIMIT 1",
                (agent_id,),
            )
            revision_row = cursor.fetchone()
        base_revision = self.revision_detail(revision_row[0]) if revision_row else None
        return analyze_draft_impact(
            draft_config=draft[0],
            draft_asset_ids=source_ids,
            base_revision=base_revision,
        )

    def discard_draft(self, actor: Principal, agent_id: str) -> None:
        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM agent_drafts WHERE agent_id = %s", (agent_id,))
            if cursor.rowcount == 0:
                raise DomainError("NOT_FOUND", "开放 Draft 不存在", status_code=404)

    def freeze_revision(self, actor: Principal, agent_id: str, draft_version: int) -> RevisionRecord:
        """在一个数据库事务内锁定 Draft、冻结 Asset 并创建待审 Revision。"""

        self._admin(actor)
        from .revisions import freeze_revision_spec

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT archived_at, suspended_at FROM agents WHERE agent_id = %s",
                (agent_id,),
            )
            agent_state = cursor.fetchone()
            if agent_state is None:
                raise DomainError("NOT_FOUND", "Agent 不存在", status_code=404)
            if agent_state[0] is not None or agent_state[1] is not None:
                raise DomainError("CONFLICT", "Agent 当前不能冻结 Revision")
            cursor.execute(
                "SELECT version, config_json FROM agent_drafts WHERE agent_id = %s FOR UPDATE",
                (agent_id,),
            )
            draft = cursor.fetchone()
            if draft is None:
                raise DomainError("NOT_FOUND", "开放 Draft 不存在", status_code=404)
            if draft[0] != draft_version:
                raise DomainError("VERSION_CONFLICT", "Draft 已被其他请求更新")
            cursor.execute(
                """
                SELECT source.asset_id, asset.sha256, source.display_name
                FROM draft_sources source
                JOIN source_assets asset ON asset.asset_id = source.asset_id
                WHERE source.agent_id = %s AND asset.parse_status IN ('SAFE', 'PARSED')
                ORDER BY source.sort_order
                """,
                (agent_id,),
            )
            sources = [
                {"asset_id": row[0], "sha256": row[1], "display_name": row[2]}
                for row in cursor.fetchall()
            ]
            if not sources:
                raise DomainError("VALIDATION_ERROR", "Revision 至少需要一份已安全检查的资料", status_code=422)
            cursor.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM agent_revisions WHERE agent_id = %s",
                (agent_id,),
            )
            revision_number = int(cursor.fetchone()[0])
            from secrets import token_hex
            spec, checksum = freeze_revision_spec(
                agent_id=agent_id,
                revision_id=f"revision_{token_hex(16)}",
                revision_number=revision_number,
                draft_config=draft[1],
                sources=sources,
            )
            cursor.execute(
                """
                INSERT INTO agent_revisions
                    (revision_id, agent_id, revision_number, checksum, spec_json, status, created_by)
                VALUES (%s, %s, %s, %s, %s::jsonb, 'REVIEW_REQUIRED', %s)
                """,
                (spec.revision_id, agent_id, revision_number, checksum, json.dumps(spec.model_dump(mode="json")), actor.user_id),
            )
            for source in sources:
                cursor.execute(
                    "INSERT INTO revision_sources (revision_id, asset_id, asset_sha256) VALUES (%s, %s, %s)",
                    (spec.revision_id, source["asset_id"], source["sha256"]),
                )
            return RevisionRecord(spec.revision_id, agent_id, revision_number, checksum, spec, "REVIEW_REQUIRED")

    def approve_revision(self, actor: Principal, revision_id: str, checksum: str) -> RevisionRecord:
        """以审批时复核的 checksum 原子批准待审 Revision。"""

        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_revisions
                SET status = 'APPROVED'
                WHERE revision_id = %s AND checksum = %s AND status = 'REVIEW_REQUIRED'
                RETURNING agent_id, revision_number, spec_json
                """,
                (revision_id, checksum),
            )
            row = cursor.fetchone()
            if row is None:
                existing = self._revision_row(cursor, revision_id)
                if existing is None:
                    raise DomainError("NOT_FOUND", "Revision 不存在", status_code=404)
                if existing[3] != checksum:
                    raise DomainError("CONFLICT", "Revision checksum 与待审批版本不一致")
                raise DomainError("CONFLICT", "Revision 当前不可审批")
            cursor.execute(
                "INSERT INTO revision_approvals (revision_id, revision_checksum, approved_by, approved_at) VALUES (%s, %s, %s, now())",
                (revision_id, checksum, actor.user_id),
            )
            spec = AgentRevisionSpecV1.model_validate(row[2])
            return RevisionRecord(revision_id, row[0], row[1], checksum, spec, "APPROVED", actor.user_id)

    def revision_detail(self, revision_id: str) -> RevisionRecord:
        """读取不可变 Revision 与当前状态，不公开可变 Draft。"""

        with self._connection() as connection, connection.cursor() as cursor:
            row = self._revision_row(cursor, revision_id)
            if row is None:
                raise DomainError("NOT_FOUND", "Revision 不存在", status_code=404)
            cursor.execute("SELECT approved_by FROM revision_approvals WHERE revision_id = %s", (revision_id,))
            approval = cursor.fetchone()
            return RevisionRecord(
                revision_id,
                row[0],
                row[1],
                row[3],
                AgentRevisionSpecV1.model_validate(row[2]),
                row[4],
                approval[0] if approval else None,
            )

    def revision_assets(self, revision_id: str) -> list[RevisionAssetRecord]:
        """读取冻结 Asset 元数据，数据库中的 hash 必须仍匹配 Revision 快照。"""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT asset.asset_id, asset.sha256, asset.size_bytes, asset.media_type,
                       asset.storage_key, source_spec.value->>'display_name', source.asset_sha256
                FROM agent_revisions revision
                CROSS JOIN LATERAL jsonb_array_elements(revision.spec_json->'source_assets')
                    WITH ORDINALITY AS source_spec(value, position)
                JOIN revision_sources source
                  ON source.revision_id = revision.revision_id
                 AND source.asset_id = source_spec.value->>'asset_id'
                JOIN source_assets asset ON asset.asset_id = source.asset_id
                WHERE revision.revision_id = %s
                ORDER BY source_spec.position
                """,
                (revision_id,),
            )
            rows = cursor.fetchall()
        if not rows:
            self.revision_detail(revision_id)
            raise DomainError("ASSET_DRIFT", "Revision 资料不存在或 checksum 漂移")
        if any(row[1] != row[6] for row in rows):
            raise DomainError("ASSET_DRIFT", "Revision 资料不存在或 checksum 漂移")
        return [RevisionAssetRecord(*row[:6]) for row in rows]

    def create_profile_proposal_job(self, actor: Principal, agent_id: str, *, idempotency_key: str):
        """事务性创建绑定当前 Draft version 的异步 Proposal Job。"""

        self._admin(actor)
        from .jobs import JobRecord

        if not idempotency_key or len(idempotency_key) > 128:
            raise DomainError("VALIDATION_ERROR", "Job 幂等键无效", status_code=422)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT version FROM agent_drafts WHERE agent_id = %s FOR UPDATE",
                (agent_id,),
            )
            draft = cursor.fetchone()
            if draft is None:
                raise DomainError("NOT_FOUND", "开放 Draft 不存在", status_code=404)
            cursor.execute("SELECT 1 FROM draft_sources WHERE agent_id = %s LIMIT 1", (agent_id,))
            if cursor.fetchone() is None:
                raise DomainError("VALIDATION_ERROR", "Profile Proposal 至少需要一份资料", status_code=422)
            job_id = f"job_{token_hex(16)}"
            cursor.execute(
                """
                INSERT INTO jobs (job_id, job_type, subject_id, revision_id, idempotency_key, status)
                VALUES (%s, 'PROFILE_PROPOSAL', %s, NULL, %s, 'PENDING')
                ON CONFLICT (job_type, subject_id, idempotency_key) DO NOTHING
                RETURNING job_id, job_type, revision_id, idempotency_key, status, attempt,
                          lease_owner, lease_until, error_code
                """,
                (job_id, agent_id, idempotency_key),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT job_id, job_type, revision_id, idempotency_key, status, attempt,
                           lease_owner, lease_until, error_code
                    FROM jobs WHERE job_type = 'PROFILE_PROPOSAL' AND subject_id = %s AND idempotency_key = %s
                    """,
                    (agent_id, idempotency_key),
                )
                return JobRecord(*cursor.fetchone())
            cursor.execute(
                "INSERT INTO profile_proposals (proposal_id, agent_id, draft_version, job_id) VALUES (%s,%s,%s,%s)",
                (f"proposal_{token_hex(16)}", agent_id, draft[0], job_id),
            )
            return JobRecord(*row)

    def profile_proposal_input(self, job_id: str):
        """读取 Proposal Job 的一致 Draft 与资料；版本漂移立即失败。"""

        from .proposals import ProfileProposalInput

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proposal.agent_id, proposal.draft_version,
                       agent.slug, agent.display_name, agent.description, agent.created_by,
                       agent.archived_at, agent.suspended_at,
                       draft.version, draft.config_json, draft.updated_by
                FROM profile_proposals proposal
                JOIN agents agent ON agent.agent_id = proposal.agent_id
                JOIN agent_drafts draft ON draft.agent_id = proposal.agent_id
                WHERE proposal.job_id = %s
                """,
                (job_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Profile Proposal Job 不存在", status_code=404)
            if row[1] != row[8]:
                raise DomainError("DRAFT_CHANGED", "Draft 已变化，请重新生成 Profile Proposal")
            cursor.execute(
                """
                SELECT asset.asset_id, asset.sha256, asset.size_bytes, asset.media_type,
                       asset.storage_key, source.display_name
                FROM draft_sources source
                JOIN source_assets asset ON asset.asset_id = source.asset_id
                WHERE source.agent_id = %s ORDER BY source.sort_order
                """,
                (row[0],),
            )
            assets = [RevisionAssetRecord(*asset_row) for asset_row in cursor.fetchall()]
        agent = AgentRecord(row[0], row[2], row[3], row[4], row[5], row[6], row[7])
        draft = DraftRecord(row[0], row[8], row[9], row[10])
        return ProfileProposalInput(job_id, agent, draft, assets)

    def publish_profile_proposal(self, *, worker_id: str, job_id: str, proposal: object) -> None:
        """同一事务写入严格 Proposal、完成 Job 并追加终态事件。"""

        from .proposals import ProfileProposalV1

        value = ProfileProposalV1.model_validate(proposal)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proposal.draft_version, draft.version, proposal.agent_id
                FROM profile_proposals proposal JOIN agent_drafts draft ON draft.agent_id = proposal.agent_id
                JOIN jobs job ON job.job_id = proposal.job_id
                WHERE proposal.job_id = %s AND job.job_type = 'PROFILE_PROPOSAL'
                  AND job.status = 'RUNNING' AND job.lease_owner = %s AND job.lease_until > now()
                FOR UPDATE OF proposal, job
                """,
                (job_id, worker_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise DomainError("CONFLICT", "Worker 不持有有效 Profile Proposal Job lease")
            if row[0] != row[1] or value.draft_version != row[0]:
                raise DomainError("DRAFT_CHANGED", "Draft 已变化，请重新生成 Profile Proposal")
            if value.agent_id != row[2]:
                raise DomainError("VALIDATION_ERROR", "Profile Proposal identity 不匹配", status_code=422)
            cursor.execute(
                "UPDATE profile_proposals SET proposal_json=%s::jsonb, proposal_checksum=%s, completed_at=now() WHERE job_id=%s",
                (json.dumps(value.model_dump(mode="json")), value.proposal_checksum, job_id),
            )
            cursor.execute("UPDATE jobs SET status='SUCCEEDED', completed_at=now(), lease_owner=NULL, lease_until=NULL WHERE job_id=%s", (job_id,))
            self._append_job_event(cursor, job_id, event_type="completed", stage="finished")

    def profile_proposal(self, job_id: str):
        from .proposals import ProfileProposalV1

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT proposal_json FROM profile_proposals WHERE job_id = %s", (job_id,))
            row = cursor.fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Profile Proposal Job 不存在", status_code=404)
        return ProfileProposalV1.model_validate(row[0]) if row[0] is not None else None

    def create_job(self, actor: Principal, *, revision_id: str, job_type: str, idempotency_key: str):
        """事务性创建 Revision Job；相同业务输入只返回原 Job。"""

        self._admin(actor)
        from .jobs import JobRecord

        if job_type not in {"BUILD", "EVALUATE"}:
            raise DomainError("VALIDATION_ERROR", "Job 类型无效", status_code=422)
        if not idempotency_key or len(idempotency_key) > 128:
            raise DomainError("VALIDATION_ERROR", "Job 幂等键无效", status_code=422)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM agent_revisions WHERE revision_id = %s", (revision_id,))
            revision = cursor.fetchone()
            if revision is None:
                raise DomainError("NOT_FOUND", "Revision 不存在", status_code=404)
            if revision[0] != "APPROVED":
                raise DomainError("CONFLICT", "只有已审批 Revision 可以构建或评测")
            job_id = f"job_{token_hex(16)}"
            cursor.execute(
                """
                INSERT INTO jobs (job_id, job_type, subject_id, revision_id, idempotency_key, status)
                VALUES (%s, %s, %s, %s, %s, 'PENDING')
                ON CONFLICT (job_type, subject_id, idempotency_key) DO NOTHING
                RETURNING job_id, job_type, revision_id, idempotency_key, status, attempt, lease_owner, lease_until, error_code
                """,
                (job_id, job_type, revision_id, revision_id, idempotency_key),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT job_id, job_type, revision_id, idempotency_key, status, attempt, lease_owner, lease_until, error_code
                    FROM jobs WHERE job_type = %s AND subject_id = %s AND idempotency_key = %s
                    """,
                    (job_type, revision_id, idempotency_key),
                )
                row = cursor.fetchone()
            return JobRecord(*row)

    def job_detail(self, job_id: str):
        """读取 Job，不泄露 Worker 私有的实现上下文。"""

        from .jobs import JobRecord

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT job_id, job_type, revision_id, idempotency_key, status, attempt, lease_owner, lease_until, error_code FROM jobs WHERE job_id = %s",
                (job_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Job 不存在", status_code=404)
            return JobRecord(*row)

    def claim_job(self, *, worker_id: str, lease_seconds: int = 60, job_types: frozenset[str] | None = None):
        """以 SKIP LOCKED 领取唯一 Job，避免多个 Worker 重复执行。"""

        from .jobs import JobRecord

        if not worker_id or lease_seconds < 1:
            raise ValueError("Worker 或 lease 参数无效")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT job_id FROM jobs
                    WHERE (status = 'PENDING'
                       OR (status = 'CANCEL_REQUESTED' AND (lease_until IS NULL OR lease_until <= now()))
                       OR (status = 'RUNNING' AND lease_until <= now()))
                      AND (%s::text[] IS NULL OR job_type = ANY(%s))
                    ORDER BY created_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE jobs
                SET status = CASE WHEN jobs.status = 'CANCEL_REQUESTED' THEN 'CANCEL_REQUESTED' ELSE 'RUNNING' END,
                    lease_owner = %s,
                    lease_until = now() + (%s * interval '1 second')
                FROM candidate
                WHERE jobs.job_id = candidate.job_id
                RETURNING jobs.job_id, jobs.job_type, jobs.revision_id, jobs.idempotency_key, jobs.status,
                          jobs.attempt, jobs.lease_owner, jobs.lease_until, jobs.error_code
                """,
                (list(job_types) if job_types is not None else None, list(job_types) if job_types is not None else None, worker_id, lease_seconds),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            job = JobRecord(*row)
            self._append_job_event(cursor, job.job_id, event_type="started", stage="claimed")
            return job

    def renew_job_lease(self, *, worker_id: str, job_id: str, lease_seconds: int = 60):
        """仅在原 lease 尚有效时续租，避免失去所有权的 Worker 复活。"""

        if not worker_id or lease_seconds < 1:
            raise ValueError("Worker 或 lease 参数无效")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET lease_until = now() + (%s * interval '1 second')
                WHERE job_id = %s AND lease_owner = %s
                  AND status IN ('RUNNING', 'CANCEL_REQUESTED')
                  AND lease_until > now()
                RETURNING job_id, job_type, revision_id, idempotency_key, status, attempt,
                          lease_owner, lease_until, error_code
                """,
                (lease_seconds, job_id, worker_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise DomainError("CONFLICT", "Worker 不持有有效 Job lease")
            from .jobs import JobRecord
            return JobRecord(*row)

    def record_job_progress(
        self,
        *,
        worker_id: str,
        job_id: str,
        stage: str,
        current: int,
        total: int,
    ) -> None:
        """在锁定 Job 且 lease 有效时追加进度 checkpoint。"""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM jobs
                WHERE job_id = %s AND lease_owner = %s
                  AND status IN ('RUNNING', 'CANCEL_REQUESTED') AND lease_until > now()
                FOR UPDATE
                """,
                (job_id, worker_id),
            )
            if cursor.fetchone() is None:
                raise DomainError("CONFLICT", "Worker 不持有有效 Job lease")
            self._append_job_event(
                cursor,
                job_id,
                event_type="progress",
                stage=stage,
                progress_current=current,
                progress_total=total,
            )

    def request_job_cancel(self, actor: Principal, job_id: str):
        """请求取消而不破坏 Worker 已开始的临界步骤。"""

        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET status = 'CANCEL_REQUESTED', cancel_requested_at = now()
                WHERE job_id = %s AND status NOT IN ('CANCELLED', 'SUCCEEDED', 'FAILED')
                RETURNING job_id, job_type, revision_id, idempotency_key, status, attempt, lease_owner, lease_until, error_code
                """,
                (job_id,),
            )
            row = cursor.fetchone()
            if row is None:
                self.job_detail(job_id)
                raise DomainError("CONFLICT", "Job 已处于终态，不能取消")
            from .jobs import JobRecord
            return JobRecord(*row)

    def retry_job(self, actor: Principal, job_id: str, *, idempotency_key: str):
        """事务性创建可恢复失败的下一 Attempt。"""

        self._admin(actor)
        from .jobs import JobRecord, RETRYABLE_ERROR_CODES

        if not idempotency_key or len(idempotency_key) > 128:
            raise DomainError("VALIDATION_ERROR", "Job 幂等键无效", status_code=422)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id, job_type, revision_id, idempotency_key, status, attempt,
                       lease_owner, lease_until, error_code
                FROM jobs WHERE job_id = %s FOR UPDATE
                """,
                (job_id,),
            )
            original_row = cursor.fetchone()
            if original_row is None:
                raise DomainError("NOT_FOUND", "Job 不存在", status_code=404)
            original = JobRecord(*original_row)
            if original.status != "FAILED" or original.error_code not in RETRYABLE_ERROR_CODES:
                raise DomainError("CONFLICT", "Job 当前不可重试")
            cursor.execute("SELECT subject_id FROM jobs WHERE job_id = %s", (job_id,))
            subject_id = cursor.fetchone()[0]
            new_job_id = f"job_{token_hex(16)}"
            cursor.execute(
                """
                INSERT INTO jobs
                    (job_id, job_type, subject_id, revision_id, idempotency_key, status, attempt)
                VALUES (%s, %s, %s, %s, %s, 'PENDING', %s)
                ON CONFLICT (job_type, subject_id, idempotency_key) DO NOTHING
                RETURNING job_id, job_type, revision_id, idempotency_key, status, attempt,
                          lease_owner, lease_until, error_code
                """,
                (
                    new_job_id,
                    original.job_type,
                    subject_id,
                    original.revision_id,
                    idempotency_key,
                    original.attempt + 1,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT job_id, job_type, revision_id, idempotency_key, status, attempt,
                           lease_owner, lease_until, error_code
                    FROM jobs
                    WHERE job_type = %s AND subject_id = %s AND idempotency_key = %s
                    """,
                    (original.job_type, subject_id, idempotency_key),
                )
                row = cursor.fetchone()
            if original.job_type == "PROFILE_PROPOSAL" and row[0] == new_job_id:
                cursor.execute(
                    """
                    INSERT INTO profile_proposals (proposal_id, agent_id, draft_version, job_id)
                    SELECT %s, agent_id, draft_version, %s FROM profile_proposals WHERE job_id = %s
                    """,
                    (f"proposal_{token_hex(16)}", new_job_id, job_id),
                )
            return JobRecord(*row)

    def complete_job(self, *, worker_id: str, job_id: str, status: str, error_code: str | None = None):
        """持有 lease 的 Worker 追加终态，过期或取消后拒绝竞争写入。"""

        if status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ValueError("Job 终态无效")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET status = %s, error_code = %s, completed_at = now(), lease_owner = NULL, lease_until = NULL
                WHERE job_id = %s AND lease_owner = %s
                  AND status NOT IN ('CANCELLED', 'SUCCEEDED', 'FAILED')
                  AND lease_until > now()
                  AND (status <> 'CANCEL_REQUESTED' OR %s = 'CANCELLED')
                RETURNING job_id, job_type, revision_id, idempotency_key, status, attempt, lease_owner, lease_until, error_code
                """,
                (status, error_code, job_id, worker_id, status),
            )
            row = cursor.fetchone()
            if row is None:
                raise DomainError("CONFLICT", "Worker 不持有 Job lease 或 Job 当前不可完成")
            terminal_event = {"SUCCEEDED": "completed", "FAILED": "failed", "CANCELLED": "cancelled"}[status]
            self._append_job_event(cursor, job_id, event_type=terminal_event, stage="finished", error_code=error_code)
            from .jobs import JobRecord
            return JobRecord(*row)

    def job_events(self, job_id: str, after_sequence: int = -1) -> list[object]:
        """读取追加 Job 事件，供 SSE 按 Last-Event-ID 恢复。"""

        from contracts.v3 import JobEventV1

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM jobs WHERE job_id = %s", (job_id,))
            if cursor.fetchone() is None:
                raise DomainError("NOT_FOUND", "Job 不存在", status_code=404)
            cursor.execute("SELECT payload_json FROM job_events WHERE job_id = %s AND sequence > %s ORDER BY sequence", (job_id, after_sequence))
            return [JobEventV1.model_validate(row[0]) for row in cursor.fetchall()]

    def publish_revision_ready(self, *, worker_id: str, job_id: str, revision_id: str, build_id: str, bundle_checksum: str, report_ref: str, storage_key: str, collection_name: str, collection_checksum: str, pass_rate: float) -> RevisionRecord:
        """在 Job lease 仍有效时原子持久化评测证据、Bundle、READY 和成功终态。"""

        if not isfinite(pass_rate) or not 0 <= pass_rate <= 1:
            raise DomainError("VALIDATION_ERROR", "评测通过率必须是 0 到 1 之间的有限数值", status_code=422)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT job_type, revision_id, status, lease_owner, lease_until FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,))
            job = cursor.fetchone()
            if job is None:
                raise DomainError("NOT_FOUND", "Job 不存在", status_code=404)
            if job[0] not in {"BUILD", "EVALUATE"} or job[1] != revision_id:
                raise DomainError("CONFLICT", "Job 与 Revision 不匹配")
            if job[2] == "CANCEL_REQUESTED":
                raise DomainError("CONFLICT", "Job 已请求取消")
            if job[2] != "RUNNING" or job[3] != worker_id or job[4] is None or job[4] <= _now():
                raise DomainError("CONFLICT", "Worker 不持有有效 Job lease")
            cursor.execute("SELECT agent_id, revision_number, spec_json, checksum, status FROM agent_revisions WHERE revision_id = %s FOR UPDATE", (revision_id,))
            row = cursor.fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Revision 不存在", status_code=404)
            if row[4] != "APPROVED":
                raise DomainError("CONFLICT", "Revision 当前不可标记为 READY")
            cursor.execute("SELECT string_agg(asset_sha256, '' ORDER BY asset_id) FROM revision_sources WHERE revision_id = %s", (revision_id,))
            document_checksum = _hash(cursor.fetchone()[0] or "")
            cursor.execute("INSERT INTO knowledge_builds (build_id, revision_id, collection_name, document_set_checksum, collection_checksum, status, completed_at) VALUES (%s,%s,%s,%s,%s,'SUCCEEDED',now())", (build_id, revision_id, collection_name, document_checksum, collection_checksum))
            cursor.execute("INSERT INTO evaluation_runs (evaluation_id, revision_id, build_id, report_key, metrics_json, passed, completed_at) VALUES (%s,%s,%s,%s,%s::jsonb,TRUE,now())", (f"evaluation_{token_hex(16)}", revision_id, build_id, report_ref, json.dumps({"pass_rate": pass_rate})))
            cursor.execute("INSERT INTO revision_bundles (revision_id, bundle_checksum, storage_key, runtime_contract_version) VALUES (%s,%s,%s,'muye-runtime/1')", (revision_id, bundle_checksum, storage_key))
            cursor.execute("UPDATE agent_revisions SET status = 'READY' WHERE revision_id = %s", (revision_id,))
            cursor.execute("UPDATE jobs SET status = 'SUCCEEDED', completed_at = now(), lease_owner = NULL, lease_until = NULL WHERE job_id = %s", (job_id,))
            self._append_job_event(cursor, job_id, event_type="completed", stage="finished")
            return RevisionRecord(revision_id, row[0], row[1], row[3], AgentRevisionSpecV1.model_validate(row[2]), "READY")

    def suspend(self, actor: Principal, agent_id: str) -> AgentRecord:
        return self._set_agent_time(actor, agent_id, "suspended_at", "停用")

    def archive(self, actor: Principal, agent_id: str) -> AgentRecord:
        result = self._set_agent_time(actor, agent_id, "archived_at", "归档")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_agent_grants WHERE agent_id=%s", (agent_id,))
            cursor.execute("DELETE FROM agent_drafts WHERE agent_id=%s", (agent_id,))
        return result

    def restore(self, actor: Principal, agent_id: str) -> AgentRecord:
        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agents SET archived_at=NULL, archived_by=NULL WHERE agent_id=%s AND archived_at IS NOT NULL RETURNING agent_id, slug, display_name, description, created_by, archived_at, suspended_at", (agent_id,))
            row = cursor.fetchone()
            if row is None:
                raise DomainError("CONFLICT", "Agent 尚未归档")
            return AgentRecord(*row)

    def attach_asset(self, actor: Principal, agent_id: str, *, sha256: str, size_bytes: int, media_type: str, storage_key: str, display_name: str) -> tuple[str, bool]:
        self._admin(actor)
        asset_id = f"asset_{sha256[:16]}"
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM agents WHERE agent_id=%s AND archived_at IS NULL AND suspended_at IS NULL", (agent_id,))
            if cursor.fetchone() is None:
                raise DomainError("CONFLICT", "Agent 当前不能接收资料")
            cursor.execute("INSERT INTO source_assets (asset_id, sha256, size_bytes, media_type, storage_key, parse_status, created_by) VALUES (%s,%s,%s,%s,%s,'SAFE',%s) ON CONFLICT (sha256) DO NOTHING RETURNING asset_id", (asset_id, sha256, size_bytes, media_type, storage_key, actor.user_id))
            reused = cursor.fetchone() is None
            cursor.execute("INSERT INTO draft_sources (agent_id, asset_id, display_name, sort_order) SELECT %s, %s, %s, count(*) FROM draft_sources WHERE agent_id=%s ON CONFLICT (agent_id, asset_id) DO NOTHING", (agent_id, asset_id, display_name, agent_id))
            cursor.execute("UPDATE agent_drafts SET version=version+1, updated_by=%s, updated_at=now() WHERE agent_id=%s", (actor.user_id, agent_id))
        return asset_id, reused

    def remove_draft_asset(self, actor: Principal, agent_id: str, asset_id: str) -> None:
        """事务性解除 Draft 资料绑定并重排 sort_order。"""

        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM agent_drafts draft
                JOIN agents agent ON agent.agent_id = draft.agent_id
                WHERE draft.agent_id = %s AND agent.archived_at IS NULL AND agent.suspended_at IS NULL
                FOR UPDATE
                """,
                (agent_id,),
            )
            if cursor.fetchone() is None:
                raise DomainError("CONFLICT", "Agent 当前不能移除资料")
            cursor.execute("DELETE FROM draft_sources WHERE agent_id = %s AND asset_id = %s", (agent_id, asset_id))
            if cursor.rowcount == 0:
                raise DomainError("NOT_FOUND", "Draft 资料不存在", status_code=404)
            cursor.execute(
                """
                WITH ordered AS (
                    SELECT asset_id, row_number() OVER (ORDER BY sort_order, asset_id) - 1 AS new_order
                    FROM draft_sources WHERE agent_id = %s
                )
                UPDATE draft_sources source SET sort_order = ordered.new_order
                FROM ordered WHERE source.agent_id = %s AND source.asset_id = ordered.asset_id
                """,
                (agent_id, agent_id),
            )
            cursor.execute("UPDATE agent_drafts SET version=version+1, updated_by=%s, updated_at=now() WHERE agent_id=%s", (actor.user_id, agent_id))

    def revision_evaluation(self, revision_id: str) -> dict[str, object]:
        """查询通过门禁的评测摘要，不返回内部 Collection 地址。"""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT build.build_id, evaluation.report_key, evaluation.metrics_json,
                       bundle.bundle_checksum, bundle.storage_key
                FROM evaluation_runs evaluation
                JOIN knowledge_builds build ON build.build_id = evaluation.build_id
                JOIN revision_bundles bundle ON bundle.revision_id = evaluation.revision_id
                WHERE evaluation.revision_id = %s AND evaluation.passed IS TRUE
                """,
                (revision_id,),
            )
            row = cursor.fetchone()
        if row is None:
            self.revision_detail(revision_id)
            raise DomainError("NOT_FOUND", "Revision 尚无通过的评测", status_code=404)
        return {
            "build_id": row[0],
            "report_ref": row[1],
            "pass_rate": float(row[2].get("pass_rate", 0)),
            "bundle_checksum": row[3],
            "storage_key": row[4],
        }

    def audit(self, *, actor_id: str | None, action: str, target_type: str, target_id: str, request_id: str, details: dict[str, object] | None = None) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO audit_events (audit_id, actor_id, action, target_type, target_id, request_id, details_json) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)", (f"audit_{token_urlsafe(12)}", actor_id, action, target_type, target_id, request_id, json.dumps(details or {})))

    def idempotent(self, scope: str, key: str, request_body: dict[str, object], create: Callable[[], tuple[int, dict[str, object]]]) -> IdempotentResponse:
        checksum = _hash_json(request_body)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_checksum, status_code, response_json FROM idempotency_records WHERE scope=%s AND idempotency_key=%s", (scope, key))
            previous = cursor.fetchone()
        if previous:
            if previous[0] != checksum:
                raise DomainError("IDEMPOTENCY_CONFLICT", "幂等键已绑定到不同请求")
            return IdempotentResponse(previous[0], previous[1], previous[2])
        status_code, body = create()
        try:
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO idempotency_records (scope,idempotency_key,request_checksum,status_code,response_json) VALUES (%s,%s,%s,%s,%s::jsonb)", (scope, key, checksum, status_code, json.dumps(body)))
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "23505":
                raise
            return self.idempotent(scope, key, request_body, create)
        return IdempotentResponse(checksum, status_code, body)

    def _set_agent_time(self, actor: Principal, agent_id: str, column: str, action: str) -> AgentRecord:
        self._admin(actor)
        actor_column = {"suspended_at": "suspended_by", "archived_at": "archived_by"}[column]
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE agents SET {column}=COALESCE({column}, now()), {actor_column}=%s WHERE agent_id=%s AND archived_at IS NULL RETURNING agent_id, slug, display_name, description, created_by, archived_at, suspended_at", (actor.user_id, agent_id))
            row = cursor.fetchone()
            if row is None:
                raise DomainError("CONFLICT", f"Agent 不可{action}")
            return AgentRecord(*row)

    def _issue_session(self, cursor: Any, user_id: str) -> TokenPair:
        access_token, refresh_token = token_urlsafe(32), token_urlsafe(48)
        expires_at = _now() + timedelta(seconds=self._ttl_seconds)
        cursor.execute("INSERT INTO core_sessions (access_hash, refresh_hash, user_id, expires_at) VALUES (%s,%s,%s,%s)", (_hash(access_token), _hash(refresh_token), user_id, expires_at))
        return TokenPair(access_token, refresh_token, expires_at)

    @staticmethod
    def _admin(principal: Principal) -> None:
        if not principal.is_admin:
            raise DomainError("AUTHORIZATION_ERROR", "需要管理员权限", status_code=403)

    @staticmethod
    def _active(cursor: Any, user_id: str) -> bool:
        cursor.execute("SELECT active FROM core_users WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        return bool(row and row[0])

    @staticmethod
    def _revision_row(cursor: Any, revision_id: str) -> tuple[object, ...] | None:
        """读取 Revision 基础字段，供审批冲突与详情查询复用。"""

        cursor.execute(
            "SELECT agent_id, revision_number, spec_json, checksum, status FROM agent_revisions WHERE revision_id = %s",
            (revision_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _append_job_event(
        cursor: Any,
        job_id: str,
        *,
        event_type: str,
        stage: str,
        error_code: str | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> None:
        """在持有 Job 行锁的事务内生成连续且已校验的 JobEvent。"""

        from .jobs import new_event

        cursor.execute("SELECT COALESCE(MAX(sequence) + 1, 0) FROM job_events WHERE job_id = %s", (job_id,))
        sequence = int(cursor.fetchone()[0])
        event = new_event(
            job_id=job_id,
            sequence=sequence,
            event_type=event_type,
            stage=stage,
            error_code=error_code,
            progress_current=progress_current,
            progress_total=progress_total,
        )
        cursor.execute(
            "INSERT INTO job_events (job_id, sequence, event_type, payload_json) VALUES (%s, %s, %s, %s::jsonb)",
            (job_id, sequence, event.event_type, json.dumps(event.model_dump(mode="json"))),
        )

    def _connection(self) -> Any:
        import psycopg
        return psycopg.connect(self._database_url, autocommit=False)
