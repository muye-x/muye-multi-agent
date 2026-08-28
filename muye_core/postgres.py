"""阶段 1 Core 的 PostgreSQL 事实源适配器。"""

from __future__ import annotations

import json
from datetime import timedelta
from secrets import token_urlsafe
from typing import Any, Callable

from .service import (
    AgentRecord,
    CoreStore,
    DomainError,
    DraftRecord,
    IdempotentResponse,
    Principal,
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

    def create_agent(self, actor: Principal, *, slug: str, display_name: str, description: str, config: dict[str, object]) -> tuple[AgentRecord, DraftRecord]:
        self._admin(actor)
        agent = AgentRecord(f"agent_{token_urlsafe(12).lower()}", slug, display_name.strip(), description.strip(), actor.user_id)
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

    def discard_draft(self, actor: Principal, agent_id: str) -> None:
        self._admin(actor)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM agent_drafts WHERE agent_id = %s", (agent_id,))
            if cursor.rowcount == 0:
                raise DomainError("NOT_FOUND", "开放 Draft 不存在", status_code=404)

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

    def _connection(self) -> Any:
        import psycopg
        return psycopg.connect(self._database_url, autocommit=False)
