"""阶段 5 Control Catalog 内部服务入口。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from dotenv import load_dotenv
from pydantic import SecretStr
import uvicorn

from .api import create_app
from .catalog import CatalogProjection
from .health import AgentHealthCollector
from .identity import IdentityStore, PostgresIdentityStore


_AGENT_ID_PATTERN = re.compile(r"agent_[a-z0-9][a-z0-9_-]{2,63}")
load_dotenv(Path(__file__).resolve().parent / ".env")


def _agent_token(agent_id: str) -> SecretStr:
    """从单个 JSON secret 环境变量读取目标绑定 token，禁止动态 URL 或 token 名进入 Catalog。"""
    raw = os.environ.get("MUYE_CONTROL_AGENT_TOKENS_JSON", "")
    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MUYE_CONTROL_AGENT_TOKENS_JSON 必须是 JSON 对象") from exc
    if not isinstance(tokens, dict) or len(tokens) > 100:
        raise ValueError("MUYE_CONTROL_AGENT_TOKENS_JSON 必须是字符串映射")
    normalized: dict[str, str] = {}
    for key, value in tokens.items():
        if (
            not isinstance(key, str)
            or _AGENT_ID_PATTERN.fullmatch(key) is None
            or not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 4096
        ):
            raise ValueError("MUYE_CONTROL_AGENT_TOKENS_JSON 包含无效 Agent token")
        normalized[key] = value.strip()
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("每个 SubAgent 必须使用不同的 Control service token")
    try:
        return SecretStr(normalized[agent_id])
    except KeyError as exc:
        raise ValueError(f"未配置 Agent 服务凭据：{agent_id}") from exc


def create_configured_app():
    """延迟读取运行时 secret，允许工具和测试安全导入本模块。"""
    state_root = Path(os.environ.get("MUYE_CONTROL_STATE_ROOT", "config/runtime/control"))
    identity_store = _identity_store()
    projection = CatalogProjection(
        health_collector=AgentHealthCollector(
            token_provider=_agent_token,
            failure_threshold=int(os.environ.get("MUYE_CONTROL_HEALTH_FAILURE_THRESHOLD", "3")),
            success_threshold=int(os.environ.get("MUYE_CONTROL_HEALTH_SUCCESS_THRESHOLD", "2")),
            observation_window_seconds=float(os.environ.get("MUYE_CONTROL_HEALTH_WINDOW_SECONDS", "60")),
        ),
        grant_store=identity_store,
        active_path=state_root / "active-catalog.json",
        citations_path=state_root / "citations.json",
    )
    return create_app(
        projection=projection,
        operator_token=os.environ.get("MUYE_CONTROL_OPERATOR_TOKEN", ""),
        main_token=os.environ.get("MUYE_CONTROL_MAIN_TOKEN", ""),
        health_token=os.environ.get("MUYE_CONTROL_HEALTH_TOKEN", ""),
        gateway_token=os.environ.get("MUYE_CONTROL_GATEWAY_TOKEN", ""),
        identity_store=identity_store,
        cookie_secure=os.environ.get("MUYE_CONTROL_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"},
        health_poll_seconds=float(os.environ.get("MUYE_CONTROL_HEALTH_POLL_SECONDS", "5")),
    )


def _identity_store() -> IdentityStore:
    """运行服务必须使用 PostgreSQL；内存实现只允许通过测试依赖注入。"""
    ttl = int(os.environ.get("MUYE_CONTROL_ACCESS_TTL_SECONDS", "900"))
    database_url = os.environ.get("MUYE_CONTROL_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("MUYE_CONTROL_DATABASE_URL 为 Control 运行服务的必填项")
    store = PostgresIdentityStore(database_url, session_ttl_seconds=ttl)
    store.initialize()
    return store


if __name__ == "__main__":
    uvicorn.run(
        "control_server.main:create_configured_app",
        factory=True,
        host=os.environ.get("MUYE_CONTROL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MUYE_CONTROL_PORT", "9880")),
    )
