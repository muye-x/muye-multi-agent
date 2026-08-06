"""仅供本地联调使用的单 Agent 注册契约。

该契约刻意不复用生产 ``AgentCatalogSnapshotV1``：本地联调没有 BuildRecord
或镜像摘要，且只能暴露一个 loopback SubAgent。Main 和 muye-data 使用它完成
最小身份、Resource 与调用边校验，生产 Control/Catalog 永远不会加载此文件。
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .models import (
    AGENT_ID_PATTERN,
    IDENTIFIER_PATTERN,
    SHA256_PATTERN,
    SEMVER_PATTERN,
    SLUG_PATTERN,
    TOOL_NAME_PATTERN,
    ContractModel,
    ResourceBindingV1,
)


_LOCAL_REVISION_PATTERN = r"^local-dev-[a-f0-9]{12,64}$"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def canonical_checksum(value: object) -> str:
    """返回本地注册文件使用的稳定 SHA-256。"""

    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


class LocalDevAgentV1(ContractModel):
    """一次本地联调中唯一可由 Main 调用的 SubAgent 描述。"""

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    slug: str = Field(pattern=SLUG_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    supported_intents: list[str] = Field(min_length=1, max_length=20)
    service_name: str = Field(pattern=r"^agent-[a-z0-9]+(?:-[a-z0-9]+)*$")
    base_url: str = Field(min_length=1, max_length=255)
    timeout_seconds: int = Field(ge=1, le=300)
    internal_protocol_version: str = Field(pattern=r"^muye-agent-internal/3(?:\.\d+)?$")
    descriptor_checksum: str = Field(pattern=SHA256_PATTERN)
    source_tree_checksum: str = Field(pattern=SHA256_PATTERN)
    resource_bindings: list[ResourceBindingV1] = Field(min_length=1, max_length=20)
    max_concurrency: int = Field(default=8, ge=1, le=128)

    @field_validator("base_url")
    @classmethod
    def require_literal_loopback_url(cls, value: str) -> str:
        """拒绝 DNS、路径与凭据，避免开发注册表成为任意 URL 通道。"""

        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in _LOOPBACK_HOSTS
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("local-dev SubAgent URL 必须是带端口的字面量 loopback HTTP 地址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def verify_service_name(self) -> "LocalDevAgentV1":
        """本地服务身份与正式 Catalog 一样只能由 descriptor slug 推导。"""

        if self.service_name != f"agent-{self.slug}":
            raise ValueError("local-dev service_name 必须由 slug 派生")
        return self


class LocalDevRegistrationV1(ContractModel):
    """local-dev Main/Data 共享的无密钥注册文件。

    ``catalog_revision``/``catalog_checksum`` 只标识本次本地会话，不能提交到
    Control。``user_id`` 是 Dev Gateway 唯一可注入的可信开发身份。
    """

    schema_version: Literal["muye.ai/local-dev-agent-registration/v1"]
    catalog_revision: str = Field(pattern=_LOCAL_REVISION_PATTERN)
    catalog_checksum: str = Field(pattern=SHA256_PATTERN)
    user_id: str = Field(pattern=IDENTIFIER_PATTERN)
    agent: LocalDevAgentV1

    @model_validator(mode="after")
    def verify_checksum(self) -> "LocalDevRegistrationV1":
        """确保无密钥注册文件未被篡改。"""
        payload = {
            "schema_version": self.schema_version,
            "user_id": self.user_id,
            "agent": self.agent.model_dump(mode="json"),
        }
        content_checksum = canonical_checksum(payload)
        expected_revision = f"local-dev-{content_checksum[:24]}"
        expected_checksum = canonical_checksum(
            {**payload, "catalog_revision": expected_revision}
        )
        if self.catalog_revision != expected_revision or self.catalog_checksum != expected_checksum:
            raise ValueError("local-dev 注册文件 checksum 无效")
        return self


def build_local_dev_registration(*, user_id: str, agent: LocalDevAgentV1) -> LocalDevRegistrationV1:
    """从已验证的单 Agent 描述构造可重放的 local-dev 注册文件。"""

    payload = {
        "schema_version": "muye.ai/local-dev-agent-registration/v1",
        "user_id": user_id,
        "agent": agent.model_dump(mode="json"),
    }
    content_checksum = canonical_checksum(payload)
    revision = f"local-dev-{content_checksum[:24]}"
    checksum = canonical_checksum({**payload, "catalog_revision": revision})
    return LocalDevRegistrationV1(
        **payload,
        catalog_revision=revision,
        catalog_checksum=checksum,
    )
