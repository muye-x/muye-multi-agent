"""v2.0 模板 Agent 工作流的严格数据契约。

模型只描述可提交、可验证且不含凭据的输入和产物。它们不能接受物理数据库地址、
任意 URL 或未知字段，以避免生成器和 Catalog 形成隐式配置通道。
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AGENT_ID_PATTERN = r"^agent_[a-z0-9][a-z0-9_-]{2,63}$"
BUILD_RECORD_ID_PATTERN = r"^build_[a-z0-9][a-z0-9_-]{2,63}$"
IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
RESOURCE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
SAFE_REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_./@:-]{0,255}$"
SEMVER_PATTERN = (
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*)|(?:\d*[A-Za-z-][0-9A-Za-z-]*))"
    r"(?:\.(?:(?:0|[1-9]\d*)|(?:\d*[A-Za-z-][0-9A-Za-z-]*)))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SHA256_PATTERN = r"^[a-f0-9]{64}$"
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
FIELD_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$"
SKILL_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_./:-]*@[A-Za-z0-9][A-Za-z0-9_.:-]*$"


class ContractModel(BaseModel):
    """所有 v2.0 公开契约的共同校验行为。"""

    model_config = ConfigDict(extra="forbid", strict=True)


def _validate_safe_relative_reference(value: str, field_name: str) -> str:
    """拒绝绝对路径、遍历片段和 URL，保留可提交的相对 artifact 引用。"""
    if value.startswith("/") or "://" in value or "\\" in value:
        raise ValueError(f"{field_name} 必须是安全的相对引用")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} 不能包含空、当前目录或父目录片段")
    return value


def _validate_rfc3339_timestamp(value: str, field_name: str) -> str:
    """验证带时区的 RFC 3339 时间，避免形状正确但不可解析的审计值。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是有效的 RFC 3339 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value


class ResourceBindingV1(ContractModel):
    """Agent 可访问的一个逻辑知识资源及其固定检索 Skill。"""

    resource_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    skill_ref: str = Field(min_length=3, max_length=256, pattern=SKILL_REF_PATTERN)

    @field_validator("skill_ref")
    @classmethod
    def validate_skill_ref(cls, value: str) -> str:
        """Skill 必须以可审计版本引用，不能退化为隐式最新版本。"""
        skill_name, separator, revision = value.rpartition("@")
        if not separator or not skill_name or not revision or "@" in skill_name or "@" in revision:
            raise ValueError("skill_ref 必须是名称@版本格式，例如 skill_product@1")
        _validate_safe_relative_reference(skill_name, "skill_ref 名称")
        return value


class AgentRuntimeV1(ContractModel):
    """SubAgent 运行与模型预算；所有字段由部署前的 descriptor 显式固定。

    `timeout_seconds` 限制单个请求的完整生命周期，`token_budget` 限制单次模型输出，
    `tool_budget` 限制一次 ReAct 请求中实际执行的工具调用数。并发和内存字段由后续
    部署生成器消费，不能由模型请求覆盖。
    """

    internal_port: int = Field(ge=1, le=65535)
    timeout_seconds: int = Field(ge=1, le=300)
    token_budget: int = Field(ge=128, le=65536)
    tool_budget: int = Field(ge=1, le=20)
    max_concurrency: int = Field(ge=1, le=128)
    memory_limit: str = Field(pattern=r"^[1-9][0-9]{0,3}[mMgG]$")


class AgentDeploymentV1(ContractModel):
    """部署开关；生成完成后默认关闭，必须由开发者显式启用。"""

    enabled: bool


class AgentSourceV1(ContractModel):
    """模板和 provenance 的可复现来源，不包含源码、密钥或部署地址。"""

    template_id: str = Field(pattern=SLUG_PATTERN)
    template_version: str = Field(pattern=SEMVER_PATTERN)
    provenance_file: Literal[".muye-generation.json"]


class AgentDescriptorV1(ContractModel):
    """`agent.yaml` 的身份、能力、资源和运行描述。"""

    schema_version: Literal["muye.ai/agent-descriptor/v1"]
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    slug: str = Field(min_length=1, max_length=63, pattern=SLUG_PATTERN)
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=SEMVER_PATTERN)
    description: str = Field(min_length=1, max_length=1000)
    supported_intents: list[str] = Field(min_length=1, max_length=20)
    entrypoint: Literal["main:app"]
    api_profile: Literal["internal"]
    protocol_version: str = Field(pattern=r"^muye-agent-internal/3(?:\.\d+)?$")
    model_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    resources: list[ResourceBindingV1] = Field(min_length=1, max_length=20)
    runtime: AgentRuntimeV1
    deployment: AgentDeploymentV1
    source: AgentSourceV1

    @field_validator("supported_intents")
    @classmethod
    def validate_supported_intents(cls, values: list[str]) -> list[str]:
        """意图名称应可展示且不重复，避免路由 Prompt 出现歧义项。"""
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("supported_intents 的每一项必须是 1 至 128 个字符")
        if len(set(normalized)) != len(normalized):
            raise ValueError("supported_intents 不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_resource_bindings(self) -> "AgentDescriptorV1":
        """同一 Agent 对同一逻辑资源只能声明一次固定绑定。"""
        resource_ids = [binding.resource_id for binding in self.resources]
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("resources 中的 resource_id 不能重复")
        return self


class AgentGenerationSpecV1(ContractModel):
    """模板生成器接收的已确认输入，LLM 不能直接构造该对象。"""

    schema_version: Literal["muye.ai/agent-generation-spec/v1"]
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    slug: str = Field(min_length=1, max_length=63, pattern=SLUG_PATTERN)
    template_id: str = Field(pattern=SLUG_PATTERN)
    template_version: str = Field(pattern=SEMVER_PATTERN)
    sdk_version: str = Field(pattern=SEMVER_PATTERN)
    agent_profile_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    agent_profile_checksum: str = Field(pattern=SHA256_PATTERN)
    resource_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    resource_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    skill_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    skill_checksum: str = Field(pattern=SHA256_PATTERN)
    model_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    retrieval_pipeline: str = Field(pattern=IDENTIFIER_PATTERN)
    scope_filter_ref: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    allowed_filter_fields: list[str] = Field(default_factory=list, max_length=50)
    allowed_return_fields: list[str] = Field(min_length=1, max_length=50)
    tool_budget: int = Field(ge=1, le=20)
    token_budget: int = Field(ge=128, le=65536)
    timeout_budget_seconds: int = Field(ge=1, le=300)
    evaluation_set_ref: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    input_checksum: str = Field(pattern=SHA256_PATTERN)

    @field_validator(
        "agent_profile_revision",
        "resource_revision",
        "skill_revision",
        "scope_filter_ref",
        "evaluation_set_ref",
    )
    @classmethod
    def validate_safe_reference(cls, value: str) -> str:
        """生成输入的所有引用都必须是安全、版本化的逻辑标识。"""
        return _validate_safe_relative_reference(value, "生成输入引用")

    @field_validator("allowed_filter_fields", "allowed_return_fields")
    @classmethod
    def validate_field_names(cls, values: list[str]) -> list[str]:
        """限制生成模板只能暴露已确认的逻辑字段集合。"""
        if any(re.fullmatch(FIELD_NAME_PATTERN, value) is None for value in values):
            raise ValueError("允许字段必须是 1 至 128 个字符的逻辑字段名")
        if len(set(values)) != len(values):
            raise ValueError("允许字段不能重复")
        return values


class SourceProvenanceV1(ContractModel):
    """首次模板生成的来源证明；其易变时间字段不参与源文件树 checksum。"""

    schema_version: Literal["muye.ai/source-provenance/v1"]
    generator_version: str = Field(pattern=SEMVER_PATTERN)
    template_id: str = Field(pattern=SLUG_PATTERN)
    template_version: str = Field(pattern=SEMVER_PATTERN)
    sdk_version: str = Field(pattern=SEMVER_PATTERN)
    generation_spec_checksum: str = Field(pattern=SHA256_PATTERN)
    knowledge_resource_checksum: str = Field(pattern=SHA256_PATTERN)
    skill_checksum: str = Field(pattern=SHA256_PATTERN)
    profile_checksum: str = Field(pattern=SHA256_PATTERN)
    generated_at: str = Field(pattern=TIMESTAMP_PATTERN)
    generated_files: list[str] = Field(min_length=1, max_length=200)
    generated_source_tree_checksum: str = Field(pattern=SHA256_PATTERN)

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: str) -> str:
        """生成时间必须可被审计系统按 RFC 3339 解析。"""
        return _validate_rfc3339_timestamp(value, "generated_at")

    @field_validator("generated_files")
    @classmethod
    def validate_generated_files(cls, values: list[str]) -> list[str]:
        """来源记录只能列出生成目录内部的相对文件。"""
        normalized = [_validate_safe_relative_reference(value, "generated_files") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("generated_files 不能重复")
        return normalized


class AgentBuildRecordV1(ContractModel):
    """CI 或受信任本地构建产生的不可变镜像与验证证明。"""

    schema_version: Literal["muye.ai/agent-build-record/v1"]
    build_record_id: str = Field(pattern=BUILD_RECORD_ID_PATTERN)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    descriptor_checksum: str = Field(pattern=SHA256_PATTERN)
    source_tree_checksum: str = Field(pattern=SHA256_PATTERN)
    sdk_version: str = Field(pattern=SEMVER_PATTERN)
    base_image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sbom_ref: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    test_report_ref: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    built_at: str = Field(pattern=TIMESTAMP_PATTERN)
    builder_version: str = Field(pattern=SEMVER_PATTERN)

    @field_validator("built_at")
    @classmethod
    def validate_built_at(cls, value: str) -> str:
        """构建时间必须可被审计系统按 RFC 3339 解析。"""
        return _validate_rfc3339_timestamp(value, "built_at")

    @field_validator("sbom_ref", "test_report_ref")
    @classmethod
    def validate_artifact_reference(cls, value: str) -> str:
        """构建证明只可指向受控 artifact，不可注入远端 URL 或路径遍历。"""
        return _validate_safe_relative_reference(value, "构建 artifact 引用")


class AgentCatalogEntryV1(ContractModel):
    """MainAgent 加载的单个 SubAgent 可调用描述，URL 必须由服务名派生。"""

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=SEMVER_PATTERN)
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    supported_intents: list[str] = Field(min_length=1, max_length=20)
    service_name: str = Field(min_length=1, max_length=63, pattern=r"^agent-[a-z0-9]+(?:-[a-z0-9]+)*$")
    base_url: str = Field(min_length=1, max_length=255)
    timeout_seconds: int = Field(ge=1, le=300)
    internal_protocol_version: str = Field(pattern=r"^muye-agent-internal/3(?:\.\d+)?$")
    api_profile: Literal["internal"]
    descriptor_checksum: str = Field(pattern=SHA256_PATTERN)
    source_tree_checksum: str = Field(pattern=SHA256_PATTERN)
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    resource_bindings: list[ResourceBindingV1] = Field(min_length=1, max_length=20)
    capabilities_checksum: str = Field(pattern=SHA256_PATTERN)
    status: Literal["DISCOVERED", "STARTING", "ACTIVE", "DEGRADED", "INACTIVE", "REJECTED"]

    @field_validator("supported_intents")
    @classmethod
    def validate_catalog_intents(cls, values: list[str]) -> list[str]:
        """Catalog 继续保持唯一且可展示的意图，以免与 Descriptor 漂移。"""
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("supported_intents 的每一项必须是 1 至 128 个字符")
        if len(set(normalized)) != len(normalized):
            raise ValueError("supported_intents 不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_internal_endpoint_and_resources(self) -> "AgentCatalogEntryV1":
        """阻止 Catalog 变成模型或配置可控的任意网络访问入口。"""
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != self.service_name
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("base_url 必须是由 service_name 派生的内部 HTTP 根地址")
        resource_ids = [binding.resource_id for binding in self.resource_bindings]
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("resource_bindings 中的 resource_id 不能重复")
        return self


class AgentCatalogSnapshotV1(ContractModel):
    """MainAgent 原子切换的不可变 Catalog 快照。"""

    schema_version: Literal["muye.ai/agent-catalog-snapshot/v1"]
    catalog_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    catalog_checksum: str = Field(pattern=SHA256_PATTERN)
    agents: list[AgentCatalogEntryV1] = Field(default_factory=list, max_length=20)

    @field_validator("catalog_revision")
    @classmethod
    def validate_catalog_revision(cls, value: str) -> str:
        """Catalog revision 只能是可审计逻辑标识，不能携带路径或 URL。"""
        return _validate_safe_relative_reference(value, "catalog_revision")

    @model_validator(mode="after")
    def validate_agent_identity_uniqueness(self) -> "AgentCatalogSnapshotV1":
        """同一快照中每个身份、工具和服务只能出现一次。"""
        for attribute in ("agent_id", "tool_name", "service_name"):
            values = [getattr(agent, attribute) for agent in self.agents]
            if len(set(values)) != len(values):
                raise ValueError(f"agents 中的 {attribute} 不能重复")
        return self


CONTRACT_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "agent-build-record-v1": AgentBuildRecordV1,
    "agent-catalog-snapshot-v1": AgentCatalogSnapshotV1,
    "agent-descriptor-v1": AgentDescriptorV1,
    "agent-generation-spec-v1": AgentGenerationSpecV1,
    "source-provenance-v1": SourceProvenanceV1,
}
