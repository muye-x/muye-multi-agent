"""v3 Core API 的阶段 1 输入输出模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """公开 API 的严格模型，拒绝未知字段和隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class LoginRequest(ApiModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=12, max_length=1024)


class UserCreateRequest(LoginRequest):
    """管理员创建普通用户的请求。"""


class GrantReplaceRequest(ApiModel):
    agent_ids: list[str] = Field(max_length=100)


class AgentCreateRequest(ApiModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=63)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1_000)
    config: dict[str, object] = Field(default_factory=dict)


class DraftPatchRequest(ApiModel):
    version: int = Field(ge=1)
    config: dict[str, object]


class ErrorResponse(ApiModel):
    code: str
    message: str
    request_id: str
    details: dict[str, object] | None = None


class AccessTokenResponse(ApiModel):
    access_token: str = Field(min_length=32)
    token_type: str = "Bearer"
    expires_at: str


class MeResponse(ApiModel):
    user_id: str
    username: str
    is_admin: bool


class AgentSummary(ApiModel):
    agent_id: str
    slug: str
    display_name: str
    description: str
    archived_at: str | None = None
    suspended_at: str | None = None


class DraftResponse(ApiModel):
    agent_id: str
    version: int
    config: dict[str, object]


class AgentDetail(AgentSummary):
    draft: DraftResponse | None = None


class CursorPage(ApiModel):
    items: list[AgentSummary]
    next_cursor: str | None = None


class SourceUploadResponse(ApiModel):
    asset_id: str
    sha256: str
    size_bytes: int
    media_type: str
    display_name: str
    reused: bool


class RevisionFreezeRequest(ApiModel):
    """基于指定 Draft 版本冻结 Revision 的请求。"""

    draft_version: int = Field(ge=1)


class RevisionApprovalRequest(ApiModel):
    """审批人确认其审阅的不可变 Revision checksum。"""

    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")


class RevisionResponse(ApiModel):
    """Revision 的公开身份、不可变 checksum 与当前状态。"""

    revision_id: str
    agent_id: str
    revision_number: int
    checksum: str
    status: str
    spec: dict[str, object]


class JobCreateRequest(ApiModel):
    """请求对已冻结 Revision 执行构建或评测。"""

    job_type: str = Field(pattern=r"^(BUILD|EVALUATE)$")


class RuntimeInvokeRequest(ApiModel):
    """已认证用户向 Active Runtime 发起的一次受控知识调用。"""

    session_id: str = Field(pattern=r"^session_[A-Za-z0-9_-]{8,128}$")
    task: str = Field(min_length=1, max_length=20_000)


class JobResponse(ApiModel):
    """可恢复 Job 的公开状态；lease owner 不向 API 客户端公开。"""

    job_id: str
    job_type: str
    revision_id: str
    status: str
    attempt: int
    error_code: str | None = None
