"""Control 内部 API 的严格请求与响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from contracts.models import (
    AGENT_ID_PATTERN,
    KNOWLEDGE_VERSION_PATTERN,
    SEMVER_PATTERN,
    AgentCatalogSnapshotV1,
    SourceLocatorV1,
)


_CHECKSUM_PATTERN = r"^[a-f0-9]{64}$"
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$"
_USER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"


class ControlModel(BaseModel):
    """所有内部 Control payload 均拒绝未知字段与隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogCandidateRequest(ControlModel):
    """部署 CLI 提交的完整、幂等 Catalog candidate。"""

    idempotency_key: str = Field(pattern=_IDEMPOTENCY_PATTERN)
    expected_active_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    snapshot: AgentCatalogSnapshotV1


class CatalogCandidateResponse(ControlModel):
    """Control 完成运行时校验后的 active 身份。"""

    status: str
    catalog_revision: str
    catalog_checksum: str = Field(pattern=_CHECKSUM_PATTERN)


class CatalogAckRequest(ControlModel):
    """Main 对已完整加载 Catalog 的接受或拒绝证明。"""

    accepted: bool
    catalog_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    reason: str | None = Field(default=None, max_length=500)


class AuthorizationResolveRequest(ControlModel):
    """Main 使用可信 CallerContext 中的用户身份解析授权。"""

    user_id: str = Field(pattern=_USER_PATTERN)


class AuthorizationResolveResponse(ControlModel):
    """一次请求捕获的 active Catalog 与 grant 交集。"""

    user_id: str
    catalog_revision: str
    catalog_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    allowed_agent_ids: list[str]


class AgentObservationRequest(ControlModel):
    """Health Collector 对一个确切 Catalog 修订的探测结果。"""

    catalog_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    healthy: bool
    error_code: str | None = Field(default=None, min_length=1, max_length=64)


class AgentObservationResponse(ControlModel):
    """应用连续阈值后可供监控和部署脚本审计的状态。"""

    agent_id: str
    status: str
    changed: bool
    consecutive_successes: int = Field(ge=0)
    consecutive_failures: int = Field(ge=0)
    catalog_revision: str
    catalog_checksum: str = Field(pattern=_CHECKSUM_PATTERN)


class CitationRecordRequest(ControlModel):
    """Main 从已认证 SubAgent 响应投影出的可信引用身份。"""

    citation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    user_id: str = Field(pattern=_USER_PATTERN)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    knowledge_version_id: str = Field(pattern=KNOWLEDGE_VERSION_PATTERN)
    catalog_revision: str = Field(min_length=1, max_length=256)
    catalog_checksum: str = Field(pattern=_CHECKSUM_PATTERN)
    locator: SourceLocatorV1


class CitationResolveResponse(ControlModel):
    """通过当前 grant 后可返回给调用方的最小引用投影。"""

    citation_id: str
    agent_id: str
    agent_version: str
    knowledge_version_id: str
    locator: SourceLocatorV1
