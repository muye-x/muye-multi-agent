"""阶段 6 的用户、会话和 Agent grant 领域服务。

该模块把密码校验、refresh token 轮换和 Admin/grant 判断集中在 Control 内，
Gateway 与 Main 只能消费经 introspection 验证后的用户身份，不能自行解释用户凭据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from threading import RLock
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


# 19 MiB / 2 iterations / 单线程在容器与 CI 中保持可预测的 Argon2id 成本。
_PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)


@dataclass(frozen=True, slots=True)
class Principal:
    """经验证的登录用户；`is_admin` 只控制管理 API，不替代 Agent grant。"""

    user_id: str
    username: str
    is_admin: bool


@dataclass(frozen=True, slots=True)
class TokenPair:
    """登录或刷新后发放的一次 access/refresh token 对。"""

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


class IdentityStore:
    """Control 身份存储协议；生产实现必须提供与此相同的事务语义。"""

    def allowed_agent_ids(self, user_id: str) -> frozenset[str]:
        raise NotImplementedError

    def bootstrap_admin(self, *, username: str, password: str) -> Principal:
        raise NotImplementedError

    def create_user(self, *, username: str, password: str) -> Principal:
        raise NotImplementedError

    def list_users(self) -> tuple[Principal, ...]:
        raise NotImplementedError

    def login(self, *, username: str, password: str) -> TokenPair:
        raise NotImplementedError

    def refresh(self, refresh_token: str) -> TokenPair:
        raise NotImplementedError

    def logout(self, access_token: str) -> None:
        raise NotImplementedError

    def introspect(self, access_token: str) -> Principal | None:
        raise NotImplementedError

    def replace_grants(
        self, *, actor_id: str, user_id: str, agent_ids: frozenset[str]
    ) -> frozenset[str]:
        raise NotImplementedError


class InMemoryIdentityStore(IdentityStore):
    """带锁的测试实现，完整覆盖生产 API 所需的 fail-closed 行为。

    它不用于生产重启后的会话持久化；阶段 6 PostgreSQL 适配器会复用该公开协议。
    """

    def __init__(self, *, session_ttl_seconds: int = 900) -> None:
        if not 60 <= session_ttl_seconds <= 86_400:
            raise ValueError("session ttl 必须在 60 至 86400 秒之间")
        self._ttl = timedelta(seconds=session_ttl_seconds)
        self._users: dict[str, _User] = {}
        self._users_by_name: dict[str, str] = {}
        self._sessions: dict[str, _Session] = {}
        self._grants: dict[str, frozenset[str]] = {}
        self._lock = RLock()

    def allowed_agent_ids(self, user_id: str) -> frozenset[str]:
        with self._lock:
            user = self._users.get(user_id)
            return self._grants.get(user_id, frozenset()) if user is not None and user.active else frozenset()

    def bootstrap_admin(self, *, username: str, password: str) -> Principal:
        normalized = _validate_username(username)
        _validate_password(password)
        with self._lock:
            if self._users:
                raise ValueError("Control 已初始化，不能再次创建初始管理员")
            user_id = f"usr_{token_urlsafe(12)}"
            principal = Principal(user_id=user_id, username=normalized, is_admin=True)
            self._users[user_id] = _User(principal=principal, password_hash=_PASSWORD_HASHER.hash(password))
            self._users_by_name[normalized] = user_id
            return principal

    def create_user(self, *, username: str, password: str) -> Principal:
        normalized = _validate_username(username)
        _validate_password(password)
        with self._lock:
            if normalized in self._users_by_name:
                raise ValueError("用户名已存在")
            principal = Principal(user_id=f"usr_{token_urlsafe(12)}", username=normalized, is_admin=False)
            self._users[principal.user_id] = _User(principal=principal, password_hash=_PASSWORD_HASHER.hash(password))
            self._users_by_name[normalized] = principal.user_id
            return principal

    def list_users(self) -> tuple[Principal, ...]:
        with self._lock:
            return tuple(sorted((user.principal for user in self._users.values()), key=lambda item: item.username))

    def login(self, *, username: str, password: str) -> TokenPair:
        normalized = _validate_username(username)
        with self._lock:
            user_id = self._users_by_name.get(normalized)
            user = self._users.get(user_id) if user_id is not None else None
            if user is None or not user.active or not _verify_password(user.password_hash, password):
                raise PermissionError("用户名或密码错误")
            return self._issue_session(user.principal.user_id)

    def refresh(self, refresh_token: str) -> TokenPair:
        token_hash = _token_hash(refresh_token)
        with self._lock:
            session_key = next((key for key, item in self._sessions.items() if item.refresh_hash == token_hash), None)
            if session_key is None:
                raise PermissionError("refresh session 无效")
            session = self._sessions.pop(session_key)
            user = self._users.get(session.user_id)
            if session.expires_at <= _now() or user is None or not user.active:
                raise PermissionError("refresh session 已过期")
            return self._issue_session(session.user_id)

    def logout(self, access_token: str) -> None:
        token_hash = _token_hash(access_token)
        with self._lock:
            for key, session in tuple(self._sessions.items()):
                if session.access_hash == token_hash:
                    self._sessions.pop(key, None)

    def introspect(self, access_token: str) -> Principal | None:
        token_hash = _token_hash(access_token)
        with self._lock:
            for key, session in tuple(self._sessions.items()):
                if session.access_hash != token_hash:
                    continue
                user = self._users.get(session.user_id)
                if session.expires_at <= _now() or user is None or not user.active:
                    self._sessions.pop(key, None)
                    return None
                return user.principal
            return None

    def replace_grants(self, *, actor_id: str, user_id: str, agent_ids: frozenset[str]) -> frozenset[str]:
        with self._lock:
            if actor_id not in self._users:
                raise PermissionError("审计主体不存在")
            if user_id not in self._users:
                raise KeyError("用户不存在")
            self._grants[user_id] = frozenset(agent_ids)
            return self._grants[user_id]

    def _issue_session(self, user_id: str) -> TokenPair:
        access_token, refresh_token = token_urlsafe(32), token_urlsafe(48)
        expires_at = _now() + self._ttl
        self._sessions[_token_hash(access_token)] = _Session(
            user_id=user_id,
            access_hash=_token_hash(access_token),
            refresh_hash=_token_hash(refresh_token),
            expires_at=expires_at,
        )
        return TokenPair(access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)


class PostgresIdentityStore(IdentityStore):
    """PostgreSQL 的 Control 身份与 grant 存储。

    每个同步操作使用独立短事务，避免在 FastAPI 事件循环中长期持有连接。部署启动时
    必须调用 :meth:`initialize`；DDL 仅包含阶段 6 的用户、会话、grant 与审计最小集合。
    """

    def __init__(self, database_url: str, *, session_ttl_seconds: int = 900) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("MUYE_CONTROL_DATABASE_URL 必须是 PostgreSQL URL")
        if not 60 <= session_ttl_seconds <= 86_400:
            raise ValueError("session ttl 必须在 60 至 86400 秒之间")
        self._database_url = database_url
        self._ttl = timedelta(seconds=session_ttl_seconds)

    def initialize(self) -> None:
        """幂等创建最小 schema，不接受自动填充默认管理员。"""
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS control_users (
                  user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL, is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                  active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS control_sessions (
                  access_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES control_users(user_id),
                  refresh_hash TEXT UNIQUE NOT NULL, expires_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_agent_grants (
                  user_id TEXT NOT NULL REFERENCES control_users(user_id), agent_id TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_by TEXT NOT NULL,
                  PRIMARY KEY (user_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS control_audit_logs (
                  audit_id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL, target TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

    def allowed_agent_ids(self, user_id: str) -> frozenset[str]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT grant.agent_id FROM user_agent_grants grant
                   JOIN control_users user_record ON user_record.user_id = grant.user_id
                   WHERE grant.user_id = %s AND user_record.active""",
                (user_id,),
            )
            return frozenset(row[0] for row in cursor.fetchall())

    def bootstrap_admin(self, *, username: str, password: str) -> Principal:
        normalized = _validate_username(username)
        _validate_password(password)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM control_users")
            if cursor.fetchone()[0] != 0:
                raise ValueError("Control 已初始化，不能再次创建初始管理员")
            principal = Principal(user_id=f"usr_{token_urlsafe(12)}", username=normalized, is_admin=True)
            cursor.execute(
                "INSERT INTO control_users (user_id, username, password_hash, is_admin) VALUES (%s, %s, %s, TRUE)",
                (principal.user_id, principal.username, _PASSWORD_HASHER.hash(password)),
            )
            self._audit(cursor, principal.user_id, "bootstrap_admin", principal.user_id)
            return principal

    def create_user(self, *, username: str, password: str) -> Principal:
        normalized = _validate_username(username)
        _validate_password(password)
        principal = Principal(user_id=f"usr_{token_urlsafe(12)}", username=normalized, is_admin=False)
        with self._connection() as connection, connection.cursor() as cursor:
            try:
                cursor.execute(
                    "INSERT INTO control_users (user_id, username, password_hash, is_admin) VALUES (%s, %s, %s, FALSE)",
                    (principal.user_id, principal.username, _PASSWORD_HASHER.hash(password)),
                )
            except Exception as exc:
                if getattr(exc, "sqlstate", None) == "23505":
                    raise ValueError("用户名已存在") from exc
                raise
            return principal

    def list_users(self) -> tuple[Principal, ...]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT user_id, username, is_admin FROM control_users WHERE active ORDER BY username")
            return tuple(Principal(user_id=row[0], username=row[1], is_admin=row[2]) for row in cursor.fetchall())

    def login(self, *, username: str, password: str) -> TokenPair:
        normalized = _validate_username(username)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT user_id, password_hash, is_admin, active FROM control_users WHERE username = %s", (normalized,))
            row = cursor.fetchone()
            if row is None or not row[3] or not _verify_password(row[1], password):
                raise PermissionError("用户名或密码错误")
            return self._issue_session(cursor, row[0])

    def refresh(self, refresh_token: str) -> TokenPair:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """DELETE FROM control_sessions WHERE refresh_hash = %s
                   RETURNING user_id, expires_at""",
                (_token_hash(refresh_token),),
            )
            row = cursor.fetchone()
            if row is None or row[1] <= _now() or not self._active(cursor, row[0]):
                raise PermissionError("refresh session 无效")
            return self._issue_session(cursor, row[0])

    def logout(self, access_token: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM control_sessions WHERE access_hash = %s", (_token_hash(access_token),))

    def introspect(self, access_token: str) -> Principal | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT user_record.user_id, user_record.username, user_record.is_admin
                   FROM control_sessions session_record JOIN control_users user_record ON user_record.user_id = session_record.user_id
                   WHERE session_record.access_hash = %s AND session_record.expires_at > now() AND user_record.active""",
                (_token_hash(access_token),),
            )
            row = cursor.fetchone()
            return Principal(user_id=row[0], username=row[1], is_admin=row[2]) if row is not None else None

    def replace_grants(self, *, actor_id: str, user_id: str, agent_ids: frozenset[str]) -> frozenset[str]:
        with self._connection() as connection, connection.cursor() as cursor:
            if not self._active(cursor, actor_id):
                raise PermissionError("审计主体不存在")
            if not self._active(cursor, user_id):
                raise KeyError("用户不存在")
            cursor.execute("DELETE FROM user_agent_grants WHERE user_id = %s", (user_id,))
            for agent_id in sorted(agent_ids):
                cursor.execute(
                    "INSERT INTO user_agent_grants (user_id, agent_id, created_by) VALUES (%s, %s, %s)",
                    (user_id, agent_id, actor_id),
                )
            self._audit(cursor, actor_id, "replace_agent_grants", user_id)
            return agent_ids

    def _issue_session(self, cursor: Any, user_id: str) -> TokenPair:
        access_token, refresh_token = token_urlsafe(32), token_urlsafe(48)
        expires_at = _now() + self._ttl
        cursor.execute(
            "INSERT INTO control_sessions (access_hash, user_id, refresh_hash, expires_at) VALUES (%s, %s, %s, %s)",
            (_token_hash(access_token), user_id, _token_hash(refresh_token), expires_at),
        )
        return TokenPair(access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)

    @staticmethod
    def _active(cursor: Any, user_id: str) -> bool:
        cursor.execute("SELECT active FROM control_users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        return bool(row and row[0])

    @staticmethod
    def _audit(cursor: Any, actor_id: str, action: str, target: str) -> None:
        cursor.execute(
            "INSERT INTO control_audit_logs (audit_id, actor_id, action, target) VALUES (%s, %s, %s, %s)",
            (f"audit_{token_urlsafe(12)}", actor_id, action, target),
        )

    def _connection(self) -> Any:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - requirements gate
            raise RuntimeError("PostgreSQL Control store 需要 psycopg") from exc
        return psycopg.connect(self._database_url, autocommit=False)


def _validate_username(value: str) -> str:
    normalized = value.strip()
    if not 3 <= len(normalized) <= 128 or not normalized.replace("_", "").replace("-", "").isalnum():
        raise ValueError("用户名格式无效")
    return normalized


def _validate_password(value: str) -> None:
    if len(value) < 12 or len(value) > 1024:
        raise ValueError("密码长度必须在 12 至 1024 之间")


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False


def _token_hash(value: str) -> str:
    if not value or len(value) > 4096:
        return ""
    return sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)
