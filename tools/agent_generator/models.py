"""Generator 的受限输入、模板与 replay recipe 模型。

这些模型只表达已确认的逻辑资源、Profile 和模板参数。它们刻意不提供物理
Collection、网络地址、凭据或可执行表达式字段，以保持生成器离线且可重放。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from contracts.models import (
    AGENT_ID_PATTERN,
    FIELD_NAME_PATTERN,
    IDENTIFIER_PATTERN,
    SAFE_REFERENCE_PATTERN,
    SEMVER_PATTERN,
    SHA256_PATTERN,
    SLUG_PATTERN,
    TIMESTAMP_PATTERN,
    AgentGenerationSpecV1,
    ContractModel,
)

from .checksums import canonical_checksum


_UNSAFE_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EXECUTABLE_PROPOSAL_LINE = re.compile(
    r"(?im)^\s*(?:from\s+\S+\s+import\s+|import\s+|def\s+|class\s+|exec\s*\(|eval\s*\(|__import__\s*\()"
)
_PROFILE_FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(?:https?|ftp)://|www\."), "URL"),
    (
        re.compile(
            r"(?:~[\\/]|(?:[A-Za-z]:)?[\\/][^\s\\/]+|\.{1,2}[\\/]|"
            r"(?:[A-Za-z0-9_.-]+[\\/])+(?:[A-Za-z0-9_.-]+))"
        ),
        "文件路径",
    ),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])(?:curl|wget|bash|zsh|fish|powershell|cmd|kubectl|docker(?:file)?)(?![A-Za-z0-9_])|"
            r"(?<![A-Za-z0-9_])(?:pip|npm|yarn|apt(?:-get)?|brew)\s+(?:install|add)(?![A-Za-z0-9_])"
        ),
        "Shell、Docker 或依赖命令",
    ),
    (re.compile(r"(?i)(?:requirements?\.txt|pyproject\.toml)"), "依赖文件"),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])(?:api[_-]?key|access[_-]?token|secret|password|passwd|private[_-]?key)"
            r"(?![A-Za-z0-9_])\s*[:=]"
        ),
        "凭据形态",
    ),
)


def _validate_human_text(value: str, field_name: str, *, max_length: int) -> str:
    """限制可由 Proposal 提供的文本，防止模板语法和代码进入生成产物。"""
    if not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    if len(value) > max_length:
        raise ValueError(f"{field_name} 不能超过 {max_length} 个字符")
    if _UNSAFE_CONTROL_CHARACTER.search(value):
        raise ValueError(f"{field_name} 不能包含控制字符")
    if "```" in value or "{{" in value or "}}" in value:
        raise ValueError(f"{field_name} 不能包含代码围栏或模板语法")
    if _EXECUTABLE_PROPOSAL_LINE.search(value):
        raise ValueError(f"{field_name} 不能包含可执行 Python")
    return value


def _validate_profile_text(value: str, field_name: str, *, max_length: int) -> str:
    """将不可信 Proposal 限制为纯说明文本，拒绝路径、命令和凭据通道。"""
    value = _validate_human_text(value, field_name, max_length=max_length)
    for pattern, label in _PROFILE_FORBIDDEN_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"{field_name} 不能包含{label}")
    return value


class FixedScopeFilterV1(ContractModel):
    """由已确认 Retrieval Skill 提供的单个固定等值过滤条件。"""

    op: Literal["eq"]
    field: str = Field(pattern=FIELD_NAME_PATTERN)
    value: str = Field(min_length=1, max_length=256)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        """固定 scope 是逻辑值，不能携带控制字符或模板片段。"""
        return _validate_human_text(value, "scope value", max_length=256)


class AgentProfileProposalV1(ContractModel):
    """可由 LLM 提议、经人工确认后才能用于模板渲染的 Profile 内容。"""

    schema_version: Literal["muye.ai/agent-profile-proposal/v1"]
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    supported_intents: list[str] = Field(min_length=1, max_length=20)
    instructions: str = Field(min_length=1, max_length=8000)
    do_not_use_when: list[str] = Field(default_factory=list, max_length=20)
    examples: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("display_name", "description", "instructions")
    @classmethod
    def validate_profile_text(cls, value: str, info: object) -> str:
        """Profile 文字仅能成为字面量 Prompt 或描述，不能成为源代码。"""
        field_name = getattr(info, "field_name", "profile field")
        limits = {"display_name": 128, "description": 1000, "instructions": 8000}
        return _validate_profile_text(value, field_name, max_length=limits[field_name])

    @field_validator("supported_intents", "do_not_use_when", "examples")
    @classmethod
    def validate_profile_lists(cls, values: list[str], info: object) -> list[str]:
        """限制列表项长度和重复项，避免 Profile 形成无界模型上下文。"""
        field_name = getattr(info, "field_name", "profile list")
        normalized = [_validate_profile_text(value, field_name, max_length=512).strip() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{field_name} 不能重复")
        return normalized


class AgentProfileInputV1(ContractModel):
    """开发者维护的稳定身份与已经确认的 Profile 组合。"""

    schema_version: Literal["muye.ai/agent-profile-input/v1"]
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    slug: str = Field(min_length=1, max_length=63, pattern=SLUG_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    profile_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    profile: AgentProfileProposalV1


class GenerationApprovalV1(ContractModel):
    """可提交的人工确认记录，绑定单个 Generator 输入版本。

    每条记录只能确认 Resource、Skill 或 Profile 中的一项。Generator 在写入 staging
    目录前读取该记录并复核 slug、revision 和 checksum，避免确认旧 Diff 后使用新输入。
    """

    schema_version: Literal["muye.ai/generation-approval/v1"]
    subject_type: Literal["resource", "skill", "profile"]
    subject_slug: str = Field(min_length=1, max_length=63, pattern=SLUG_PATTERN)
    revision: str = Field(min_length=1, max_length=256, pattern=SAFE_REFERENCE_PATTERN)
    checksum: str = Field(pattern=SHA256_PATTERN)
    approved_at: str = Field(pattern=TIMESTAMP_PATTERN)
    approved_by: str = Field(pattern=IDENTIFIER_PATTERN)

    @field_validator("revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """审批记录只能引用可提交的逻辑 revision，不能成为路径或 URL 通道。"""
        if value.startswith("/") or "://" in value or "\\" in value:
            raise ValueError("approval revision 必须是安全的逻辑引用")
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("approval revision 不能包含路径遍历片段")
        return value

    @field_validator("approved_at")
    @classmethod
    def validate_approved_at(cls, value: str) -> str:
        """审批时间必须是可审计的带时区 RFC 3339 值。"""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("approved_at 必须是有效的 RFC 3339 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("approved_at 必须包含时区")
        return value


class KnowledgeGenerationInputV1(ContractModel):
    """已确认知识 Resource 与 Retrieval Skill 到 Generator 的最小逻辑投影。"""

    schema_version: Literal["muye.ai/knowledge-generation-input/v1"]
    knowledge_slug: str = Field(min_length=1, max_length=63, pattern=SLUG_PATTERN)
    resource_id: str = Field(pattern=IDENTIFIER_PATTERN)
    resource_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    resource_checksum: str = Field(pattern=SHA256_PATTERN)
    skill_revision: str = Field(min_length=3, max_length=256, pattern=SAFE_REFERENCE_PATTERN)
    skill_checksum: str = Field(pattern=SHA256_PATTERN)
    model_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    retrieval_pipeline: str = Field(pattern=IDENTIFIER_PATTERN)
    scope_filter_ref: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    fixed_scope_filter: FixedScopeFilterV1
    allowed_filter_fields: list[str] = Field(default_factory=list, max_length=50)
    allowed_return_fields: list[str] = Field(min_length=1, max_length=50)
    tool_budget: int = Field(ge=1, le=20)
    token_budget: int = Field(ge=128, le=65536)
    timeout_budget_seconds: int = Field(ge=1, le=300)
    evaluation_set_ref: str = Field(pattern=SAFE_REFERENCE_PATTERN)

    @field_validator("skill_revision")
    @classmethod
    def validate_skill_revision(cls, value: str) -> str:
        """Skill 必须使用版本化名称，避免生成器绑定隐式 latest。"""
        skill_name, separator, revision = value.rpartition("@")
        if not separator or not skill_name or not revision or "@" in skill_name or "@" in revision:
            raise ValueError("skill_revision 必须是名称@版本格式")
        return value

    @field_validator("allowed_filter_fields", "allowed_return_fields")
    @classmethod
    def validate_field_lists(cls, values: list[str]) -> list[str]:
        """只允许描述逻辑字段名，不能把路径或物理表达式带入模板。"""
        if any(re.fullmatch(FIELD_NAME_PATTERN, value) is None for value in values):
            raise ValueError("允许字段必须是逻辑字段名")
        if len(set(values)) != len(values):
            raise ValueError("允许字段不能重复")
        return values

    @model_validator(mode="after")
    def validate_fixed_scope(self) -> "KnowledgeGenerationInputV1":
        """固定 scope 必须在允许字段内，保证模板和 Skill 使用同一白名单。"""
        if self.fixed_scope_filter.field not in self.allowed_filter_fields:
            raise ValueError("fixed_scope_filter.field 必须位于 allowed_filter_fields")
        return self


class TemplateManifestV1(ContractModel):
    """模板目录的静态元数据，Generator 只接受受版本控制的本地模板。"""

    template_id: str = Field(pattern=SLUG_PATTERN)
    template_version: str = Field(pattern=SEMVER_PATTERN)
    sdk_version: str = Field(pattern=SEMVER_PATTERN)
    sdk_version_specifier: str = Field(min_length=4, max_length=80)
    base_image_build_arg: Literal["MUYE_AGENT_BASE_IMAGE"]
    api_profile: Literal["internal"]

    @model_validator(mode="after")
    def validate_sdk_version_specifier(self) -> "TemplateManifestV1":
        """标准模板必须固定 SDK 精确版本，不能在生成时引入浮动依赖。"""
        if self.sdk_version_specifier != f"=={self.sdk_version}":
            raise ValueError("sdk_version_specifier 必须精确固定 sdk_version")
        return self


class GenerationRecipeV1(ContractModel):
    """写入生成目录的非敏感重放输入，仅供 validate 和只读 diff 使用。"""

    schema_version: Literal["muye.ai/generation-recipe/v1"]
    generation_spec: AgentGenerationSpecV1
    knowledge: KnowledgeGenerationInputV1
    profile_input: AgentProfileInputV1

    @model_validator(mode="after")
    def validate_recipe_consistency(self) -> "GenerationRecipeV1":
        """防止 recipe 被编辑为与 provenance 声明不同的逻辑输入。"""
        spec = self.generation_spec
        knowledge = self.knowledge
        profile_input = self.profile_input
        mismatches: list[str] = []
        for field_name, actual, expected in (
            ("agent_id", spec.agent_id, profile_input.agent_id),
            ("slug", spec.slug, profile_input.slug),
            ("agent_profile_revision", spec.agent_profile_revision, profile_input.profile_revision),
            ("resource_id", spec.resource_id, knowledge.resource_id),
            ("resource_revision", spec.resource_revision, knowledge.resource_revision),
            ("skill_revision", spec.skill_revision, knowledge.skill_revision),
            ("skill_checksum", spec.skill_checksum, knowledge.skill_checksum),
            ("model_alias", spec.model_alias, knowledge.model_alias),
            ("retrieval_pipeline", spec.retrieval_pipeline, knowledge.retrieval_pipeline),
            ("scope_filter_ref", spec.scope_filter_ref, knowledge.scope_filter_ref),
            ("allowed_filter_fields", spec.allowed_filter_fields, knowledge.allowed_filter_fields),
            ("allowed_return_fields", spec.allowed_return_fields, knowledge.allowed_return_fields),
            ("tool_budget", spec.tool_budget, knowledge.tool_budget),
            ("token_budget", spec.token_budget, knowledge.token_budget),
            ("timeout_budget_seconds", spec.timeout_budget_seconds, knowledge.timeout_budget_seconds),
            ("evaluation_set_ref", spec.evaluation_set_ref, knowledge.evaluation_set_ref),
        ):
            if actual != expected:
                mismatches.append(field_name)
        if mismatches:
            raise ValueError("generation recipe 字段不一致：" + ", ".join(mismatches))
        expected_profile_checksum = canonical_checksum(profile_input.profile.model_dump(mode="json"))
        if spec.agent_profile_checksum != expected_profile_checksum:
            raise ValueError("generation recipe 的 agent_profile_checksum 与 Profile 不一致")
        spec_payload = spec.model_dump(mode="json")
        actual_input_checksum = str(spec_payload.pop("input_checksum"))
        expected_input_checksum = canonical_checksum(spec_payload)
        if actual_input_checksum != expected_input_checksum:
            raise ValueError("generation recipe 的 input_checksum 与 GenerationSpec 不一致")
        return self
