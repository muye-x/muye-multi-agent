"""SubAgent 到 muye-data 的目标绑定服务身份与 Resource 授权。"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
import re
from secrets import compare_digest

from fastapi import Request
from pydantic import SecretStr

from src.errors import (
    AuthorizationUnavailableError,
    ServiceAuthenticationError,
    ServiceAuthorizationError,
)


_AGENT_ID_PATTERN = re.compile(r"agent_[a-z0-9][a-z0-9_-]{2,63}")
_CHECKSUM_PATTERN = re.compile(r"[a-f0-9]{64}")
_SCHEMA_VERSION = "muye.ai/agent-catalog-snapshot/v1"


def _canonical_checksum(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


class DataServiceAuthorizer:
    """按请求重新读取 active Catalog，使下线、降级和 Resource 撤权即时生效。"""

    def __init__(self, *, catalog_path: Path | None, tokens: Mapping[str, SecretStr]) -> None:
        if catalog_path is None and tokens:
            raise ValueError("启用 Data Agent token 时必须配置 active Catalog 路径")
        if catalog_path is not None and not tokens:
            raise ValueError("启用 Data Agent 身份时至少需要一个目标绑定 token")
        self._catalog_path = catalog_path
        self._tokens = dict(tokens)

    @property
    def enabled(self) -> bool:
        return self._catalog_path is not None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        base_directory: Path,
    ) -> "DataServiceAuthorizer":
        """从显式开关、Catalog 文件和 secret JSON 映射构造授权器。"""
        enabled_value = environ.get("MUYE_DATA_AGENT_AUTH_ENABLED", "false").strip().lower()
        if enabled_value not in {"true", "false"}:
            raise ValueError("MUYE_DATA_AGENT_AUTH_ENABLED 必须是 true 或 false")
        if enabled_value == "false":
            return cls(catalog_path=None, tokens={})

        configured_path = environ.get("MUYE_DATA_AGENT_CATALOG_PATH", "").strip()
        if not configured_path:
            raise ValueError("启用 Data Agent 身份时必须配置 MUYE_DATA_AGENT_CATALOG_PATH")
        path = Path(configured_path)
        catalog_path = path if path.is_absolute() else base_directory / path

        raw_tokens = environ.get("MUYE_DATA_AGENT_TOKENS_JSON", "")
        try:
            values = json.loads(raw_tokens)
        except json.JSONDecodeError as exc:
            raise ValueError("MUYE_DATA_AGENT_TOKENS_JSON 必须是 JSON object") from exc
        if not isinstance(values, dict) or not 1 <= len(values) <= 100:
            raise ValueError("MUYE_DATA_AGENT_TOKENS_JSON 必须包含 1 至 100 个 Agent token")
        tokens: dict[str, SecretStr] = {}
        normalized_values: set[str] = set()
        for agent_id, token in values.items():
            if not isinstance(agent_id, str) or _AGENT_ID_PATTERN.fullmatch(agent_id) is None:
                raise ValueError("MUYE_DATA_AGENT_TOKENS_JSON 包含无效 agent_id")
            if not isinstance(token, str) or not token.strip() or len(token.strip()) > 4096:
                raise ValueError("MUYE_DATA_AGENT_TOKENS_JSON 包含无效 token")
            normalized = token.strip()
            if normalized in normalized_values:
                raise ValueError("每个 SubAgent 必须使用不同的 Data service token")
            normalized_values.add(normalized)
            tokens[agent_id] = SecretStr(normalized)
        return cls(catalog_path=catalog_path, tokens=tokens)

    def authorize(self, request: Request, *, resource_id: str) -> None:
        """验证 Bearer token、部署 identity 和 active Catalog Resource 绑定。"""
        if not self.enabled:
            return
        agent_id = request.headers.get("X-Muye-Agent-Id", "").strip()
        authorization = request.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied_token = authorization[len(prefix) :].strip() if authorization.startswith(prefix) else ""
        expected = self._tokens.get(agent_id)
        if (
            expected is None
            or not supplied_token
            or not compare_digest(supplied_token, expected.get_secret_value())
        ):
            raise ServiceAuthenticationError()

        snapshot = self._load_catalog()
        entry = next(
            (
                item
                for item in snapshot["agents"]
                if isinstance(item, dict) and item.get("agent_id") == agent_id and item.get("status") == "ACTIVE"
            ),
            None,
        )
        if entry is None or not self._identity_matches(request, entry):
            raise ServiceAuthorizationError()
        resources = entry.get("resource_bindings")
        allowed_resources = {
            item.get("resource_id")
            for item in resources
            if isinstance(item, dict) and isinstance(item.get("resource_id"), str)
        } if isinstance(resources, list) else set()
        if resource_id not in allowed_resources:
            raise ServiceAuthorizationError()

    def _load_catalog(self) -> dict[str, object]:
        path = self._catalog_path
        assert path is not None
        if path.is_symlink() or not path.is_file():
            raise AuthorizationUnavailableError()
        try:
            raw_bytes = path.read_bytes()
            if len(raw_bytes) > 2_097_152:
                raise ValueError("Catalog exceeds size limit")
            payload = json.loads(raw_bytes.decode("utf-8"))
            self._validate_catalog(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AuthorizationUnavailableError() from exc
        return payload

    @staticmethod
    def _validate_catalog(payload: object) -> None:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "catalog_revision",
            "catalog_checksum",
            "agents",
        }:
            raise ValueError("Catalog envelope is invalid")
        agents = payload.get("agents")
        if payload.get("schema_version") != _SCHEMA_VERSION or not isinstance(agents, list) or len(agents) > 20:
            raise ValueError("Catalog schema is invalid")
        if any(not isinstance(item, dict) for item in agents):
            raise ValueError("Catalog agents are invalid")
        sort_keys = []
        for item in agents:
            key = (item.get("agent_id"), item.get("agent_version"), item.get("tool_name"))
            if any(not isinstance(value, str) for value in key):
                raise ValueError("Catalog Agent identity is invalid")
            sort_keys.append(key)
        if len({key[0] for key in sort_keys}) != len(sort_keys):
            raise ValueError("Catalog agent_id is not unique")
        ordered = sorted(agents, key=lambda item: (item.get("agent_id"), item.get("agent_version"), item.get("tool_name")))
        content_checksum = _canonical_checksum({"schema_version": _SCHEMA_VERSION, "agents": ordered})
        expected_revision = f"catalog-{content_checksum[:24]}"
        expected_checksum = _canonical_checksum(
            {
                "schema_version": _SCHEMA_VERSION,
                "catalog_revision": expected_revision,
                "agents": ordered,
            }
        )
        if payload.get("catalog_revision") != expected_revision or payload.get("catalog_checksum") != expected_checksum:
            raise ValueError("Catalog checksum is invalid")

    @staticmethod
    def _identity_matches(request: Request, entry: dict[str, object]) -> bool:
        agent_id = entry.get("agent_id")
        agent_version = entry.get("agent_version")
        descriptor_checksum = entry.get("descriptor_checksum")
        source_checksum = entry.get("source_tree_checksum")
        service_name = entry.get("service_name")
        if (
            not isinstance(agent_id, str)
            or _AGENT_ID_PATTERN.fullmatch(agent_id) is None
            or not isinstance(agent_version, str)
            or not isinstance(descriptor_checksum, str)
            or _CHECKSUM_PATTERN.fullmatch(descriptor_checksum) is None
            or not isinstance(source_checksum, str)
            or _CHECKSUM_PATTERN.fullmatch(source_checksum) is None
            or not isinstance(service_name, str)
        ):
            return False
        deployment_id = request.headers.get("X-Muye-Deployment-Id", "").strip()
        return (
            request.headers.get("X-Muye-Service-Id", "").strip() == service_name
            and request.headers.get("X-Muye-Agent-Version", "").strip() == agent_version
            and request.headers.get("X-Muye-Descriptor-Checksum", "").strip() == descriptor_checksum
            and request.headers.get("X-Muye-Source-Checksum", "").strip() == source_checksum
            and re.fullmatch(
                rf"{re.escape(agent_id)}:{re.escape(agent_version)}:[a-f0-9]{{12}}",
                deployment_id,
            )
            is not None
        )
