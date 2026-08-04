"""两步式 Agent 创建流程的受限用户输入与审计产物。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from contracts.models import AGENT_ID_PATTERN, IDENTIFIER_PATTERN, SEMVER_PATTERN, SLUG_PATTERN, ContractModel, SHA256_PATTERN, TIMESTAMP_PATTERN


class AgentProjectSpecV1(ContractModel):
    """用户维护的唯一创建意图文件。

    文档目录固定为项目目录下的 ``sources``。连接地址和密钥不属于此契约，均从
    受信任的环境变量读取。
    """

    schema_version: Literal["muye.ai/agent-project/v1"]
    slug: str = Field(pattern=SLUG_PATTERN)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=2_000)
    prohibited_actions: list[str] = Field(min_length=1, max_length=20)
    examples: list[str] = Field(default_factory=list, max_length=10)
    agent_version: str = Field(default="0.1.0", pattern=SEMVER_PATTERN)
    chat_model_alias: str = Field(default="chat-default", pattern=IDENTIFIER_PATTERN)
    embedding_model_alias: str = Field(default="text-embedding-default", pattern=IDENTIFIER_PATTERN)
    connection: str = Field(default="milvus_default", pattern=IDENTIFIER_PATTERN)
    ocr_available: bool = False
    max_characters: int = Field(default=1200, ge=200, le=12_000)
    overlap_characters: int = Field(default=120, ge=0, le=4_000)
    min_characters: int = Field(default=80, ge=1, le=4_000)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    evaluation_case_count: int = Field(default=12, ge=1, le=30)
    min_recall: float = Field(default=0.8, ge=0, le=1)
    min_mrr: float = Field(default=0.6, ge=0, le=1)

    @field_validator("display_name", "objective", "prohibited_actions", "examples")
    @classmethod
    def reject_control_characters(cls, value: str | list[str]) -> str | list[str]:
        values = [value] if isinstance(value, str) else value
        if any(not item.strip() or any(ord(character) < 32 for character in item) for item in values):
            raise ValueError("项目说明不能包含空值或控制字符")
        if len(set(values)) != len(values):
            raise ValueError("项目说明不能重复")
        return value

    @model_validator(mode="after")
    def validate_chunking(self) -> "AgentProjectSpecV1":
        if self.slug == "main" or len(self.slug) < 2:
            raise ValueError("slug 必须至少两个字符且不能为 main")
        if self.overlap_characters >= self.max_characters or self.min_characters > self.max_characters:
            raise ValueError("分块参数不合法")
        return self


class AgentCreationPlanV1(ContractModel):
    """`prepare` 生成的不可变审阅计划。

    payload 保存已验证的派生配置，令 ``create`` 无需再次向 LLM 请求提案；所有内容
    仍会在执行前与当前源文件及配置 checksum 复核。
    """

    schema_version: Literal["muye.ai/agent-creation-plan/v1"]
    project_slug: str = Field(pattern=SLUG_PATTERN)
    project_checksum: str = Field(pattern=SHA256_PATTERN)
    source_set_checksum: str = Field(pattern=SHA256_PATTERN)
    proposal_checksum: str = Field(pattern=SHA256_PATTERN)
    plan_checksum: str = Field(pattern=SHA256_PATTERN)
    created_at: str = Field(pattern=TIMESTAMP_PATTERN)
    source_config: dict[str, Any]
    knowledge_input: dict[str, Any]
    profile_input: dict[str, Any]
    evaluation_set: dict[str, Any]
    summary: dict[str, Any]

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("created_at 必须包含时区")
        return value


class AgentCreationApprovalV1(ContractModel):
    """一次人工确认完整 Creation Plan 的审计记录。"""

    schema_version: Literal["muye.ai/agent-creation-approval/v1"]
    project_slug: str = Field(pattern=SLUG_PATTERN)
    plan_checksum: str = Field(pattern=SHA256_PATTERN)
    approved_by: str = Field(pattern=IDENTIFIER_PATTERN)
    approved_at: str = Field(pattern=TIMESTAMP_PATTERN)


class AgentCreationRunV1(ContractModel):
    """一次 create 执行的最小可审计状态。"""

    schema_version: Literal["muye.ai/agent-creation-run/v1"]
    run_id: str = Field(pattern=r"^creation_[a-f0-9]{32}$")
    project_slug: str = Field(pattern=SLUG_PATTERN)
    plan_checksum: str = Field(pattern=SHA256_PATTERN)
    status: Literal["RUNNING", "SUCCEEDED", "FAILED"]
    stage: str = Field(min_length=1, max_length=64)
    created_at: str = Field(pattern=TIMESTAMP_PATTERN)
    updated_at: str = Field(pattern=TIMESTAMP_PATTERN)
    error: str | None = Field(default=None, max_length=1_000)
