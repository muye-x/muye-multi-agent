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

