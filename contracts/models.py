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
KNOWLEDGE_ID_PATTERN = r"^kb[._][a-z0-9][a-z0-9_.-]{1,126}$"
KNOWLEDGE_VERSION_PATTERN = r"^kv_[a-z0-9][a-z0-9_-]{2,63}$"
COLLECTION_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,254}$"
JOB_ID_PATTERN = r"^job_[a-f0-9]{16,64}$"
BLOCK_ID_PATTERN = r"^block_[a-f0-9]{16,64}$"
DOCUMENT_ID_PATTERN = r"^doc_[a-f0-9]{16,64}$"
SOURCE_FILE_ID_PATTERN = r"^file_[a-f0-9]{16,64}$"


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

    caller: Literal["agent-main"] = "agent-main"
    target_type: Literal["sub_agent"] = "sub_agent"
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
    max_concurrency: int = Field(default=8, ge=1, le=128)
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


def _validate_relative_source_path(value: str, field_name: str) -> str:
    """验证展示和引用共用的相对源文件路径，拒绝路径逃逸与控制字符。"""
    if not value or len(value) > 1024 or value.startswith(("/", "\\")) or "\\" in value:
        raise ValueError(f"{field_name} 必须是安全的相对路径")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{field_name} 不能包含空、当前目录或父目录片段")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} 不能包含控制字符")
    return value


class SourceLocatorV1(ContractModel):
    """一个解析 block 可回溯到的源文件定位信息。

    ``kind`` 表示定位单位，``start``/``end`` 均为从 1 开始的闭区间；其值不会被
    用作本地文件系统路径，只用于 citation 展示和可审计回溯。
    """

    source_path: str = Field(min_length=1, max_length=1024)
    kind: Literal["line", "page", "paragraph"]
    start: int = Field(ge=1, le=10_000_000)
    end: int = Field(ge=1, le=10_000_000)

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        """源定位只能保存导入根内的相对路径。"""
        return _validate_relative_source_path(value, "source_path")

    @model_validator(mode="after")
    def validate_range(self) -> "SourceLocatorV1":
        """定位区间必须正向，避免 citation 出现无法解释的范围。"""
        if self.end < self.start:
            raise ValueError("source locator 的 end 不能小于 start")
        return self


class ParsedBlockV1(ContractModel):
    """解析器输出的最小稳定文本单元。"""

    block_id: str = Field(pattern=BLOCK_ID_PATTERN)
    ordinal: int = Field(ge=0, le=10_000_000)
    content: str = Field(min_length=1, max_length=200_000)
    locator: SourceLocatorV1

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """拒绝空白或控制字符文本，避免空 chunk 被发布。"""
        if not value.strip() or "\x00" in value:
            raise ValueError("block content 必须包含非空文本且不能包含 NUL")
        return value


class ParsedDocumentV1(ContractModel):
    """PDF、DOCX、Markdown 与 TXT 统一的解析中间表示。

    原文件路径不进入该契约；``source_path`` 是相对于受控导入根的展示路径，
    ``source_checksum`` 将解析结果精确绑定到输入字节。
    """

    schema_version: Literal["muye.ai/parsed-document/v1"]
    knowledge_id: str = Field(pattern=KNOWLEDGE_ID_PATTERN)
    knowledge_version_id: str = Field(pattern=KNOWLEDGE_VERSION_PATTERN)
    document_id: str = Field(pattern=DOCUMENT_ID_PATTERN)
    source_file_id: str = Field(pattern=SOURCE_FILE_ID_PATTERN)
    source_path: str = Field(min_length=1, max_length=1024)
    source_checksum: str = Field(pattern=SHA256_PATTERN)
    parser_profile: str = Field(pattern=IDENTIFIER_PATTERN)
    blocks: list[ParsedBlockV1] = Field(min_length=1, max_length=100_000)

    @field_validator("source_path")
    @classmethod
    def validate_document_source_path(cls, value: str) -> str:
        """文档展示路径遵循 locator 相同的导入根约束。"""
        return _validate_relative_source_path(value, "source_path")

    @model_validator(mode="after")
    def validate_blocks(self) -> "ParsedDocumentV1":
        """block ID、序号和文件引用必须唯一且一致。"""
        identifiers = [block.block_id for block in self.blocks]
        ordinals = [block.ordinal for block in self.blocks]
        if len(set(identifiers)) != len(identifiers) or len(set(ordinals)) != len(ordinals):
            raise ValueError("blocks 中的 block_id 和 ordinal 不能重复")
        if sorted(ordinals) != list(range(len(ordinals))):
            raise ValueError("blocks.ordinal 必须从 0 连续递增")
        if any(block.locator.source_path != self.source_path for block in self.blocks):
            raise ValueError("每个 block locator 必须指向文档的 source_path")
        return self


class ChunkingPolicyV1(ContractModel):
    """确定性切分参数；字符而非 token 预算使离线构建可复现。"""

    max_characters: int = Field(ge=200, le=12_000)
    overlap_characters: int = Field(default=120, ge=0, le=4_000)
    min_characters: int = Field(default=80, ge=1, le=4_000)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ChunkingPolicyV1":
        """重叠和最小块大小不能吞掉整个块。"""
        if self.overlap_characters >= self.max_characters:
            raise ValueError("overlap_characters 必须小于 max_characters")
        if self.min_characters > self.max_characters:
            raise ValueError("min_characters 不能大于 max_characters")
        return self


class SchemaMetadataFieldV1(ContractModel):
    """可发布的受限元数据字段，禁止从文档正文派生任意数据库表达式。"""

    name: str = Field(pattern=FIELD_NAME_PATTERN)
    type: Literal["string", "integer", "boolean"]
    filterable: bool = False
    returnable: bool = False


class SchemaProposalV1(ContractModel):
    """待人工确认的知识字段、切分和索引逻辑提案。"""

    schema_version: Literal["muye.ai/schema-proposal/v1"]
    knowledge_id: str = Field(pattern=KNOWLEDGE_ID_PATTERN)
    knowledge_version_id: str = Field(pattern=KNOWLEDGE_VERSION_PATTERN)
    proposal_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    proposal_checksum: str = Field(pattern=SHA256_PATTERN)
    parser_profile: str = Field(pattern=IDENTIFIER_PATTERN)
    embedding_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    embedding_dimensions: int = Field(ge=1, le=65_536)
    chunking: ChunkingPolicyV1
    metadata_fields: list[SchemaMetadataFieldV1] = Field(default_factory=list, max_length=32)
    document_set_checksum: str = Field(pattern=SHA256_PATTERN)

    @field_validator("proposal_revision")
    @classmethod
    def validate_proposal_revision(cls, value: str) -> str:
        """提案 revision 必须可审计，不能成为路径或 URL 注入通道。"""
        return _validate_safe_relative_reference(value, "proposal_revision")

    @model_validator(mode="after")
    def validate_metadata_fields(self) -> "SchemaProposalV1":
        """每个逻辑元数据名称只能声明一次。"""
        fields = [item.name for item in self.metadata_fields]
        if len(set(fields)) != len(fields):
            raise ValueError("metadata_fields.name 不能重复")
        return self


class CollectionFieldPlanV1(ContractModel):
    """Planner 输出的固定 Milvus 字段，类型集合不允许透传原生 DDL。"""

    name: str = Field(pattern=FIELD_NAME_PATTERN)
    data_type: Literal["VARCHAR", "INT64", "JSON", "FLOAT_VECTOR", "SPARSE_FLOAT_VECTOR"]
    primary_key: bool = False
    max_length: int | None = Field(default=None, ge=1, le=65_535)
    dimension: int | None = Field(default=None, ge=1, le=65_536)
    enable_analyzer: bool = False

    @model_validator(mode="after")
    def validate_field_shape(self) -> "CollectionFieldPlanV1":
        """向量、字符串和标量字段只能拥有其适用参数。"""
        if self.data_type == "FLOAT_VECTOR":
            if self.dimension is None or self.max_length is not None or self.enable_analyzer:
                raise ValueError("FLOAT_VECTOR 字段必须且只能声明 dimension")
        elif self.data_type == "VARCHAR":
            if self.max_length is None or self.dimension is not None:
                raise ValueError("VARCHAR 字段必须声明 max_length 且不能声明 dimension")
        elif self.dimension is not None or self.max_length is not None or self.enable_analyzer:
            raise ValueError("当前字段类型不能声明长度、维度或 analyzer")
        return self


class MilvusIndexPlanV1(ContractModel):
    """受限的 Milvus 索引描述，供 Publisher 创建和回读验证。"""

    field_name: str = Field(pattern=FIELD_NAME_PATTERN)
    index_type: Literal["FLAT", "SPARSE_INVERTED_INDEX"]
    metric_type: Literal["COSINE", "IP", "L2", "BM25"]


class CollectionIndexPlanV1(ContractModel):
    """由确认 Proposal 确定性派生的不可变 Milvus Collection 计划。"""

    schema_version: Literal["muye.ai/collection-index-plan/v1"]
    knowledge_id: str = Field(pattern=KNOWLEDGE_ID_PATTERN)
    knowledge_version_id: str = Field(pattern=KNOWLEDGE_VERSION_PATTERN)
    plan_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    plan_checksum: str = Field(pattern=SHA256_PATTERN)
    collection_name: str = Field(pattern=COLLECTION_NAME_PATTERN)
    fields: list[CollectionFieldPlanV1] = Field(min_length=8, max_length=64)
    bm25_function_name: Literal["bm25_content"]
    indexes: list[MilvusIndexPlanV1] = Field(min_length=2, max_length=8)

    @field_validator("plan_revision")
    @classmethod
    def validate_plan_revision(cls, value: str) -> str:
        """计划 revision 只能保存逻辑引用。"""
        return _validate_safe_relative_reference(value, "plan_revision")

    @model_validator(mode="after")
    def validate_required_milvus_shape(self) -> "CollectionIndexPlanV1":
        """确保每个计划都具备 Dense、BM25/Sparse 与 citation 所需固定字段。"""
        fields = {field.name: field for field in self.fields}
        required = {
            "chunk_id": "VARCHAR",
            "knowledge_version_id": "VARCHAR",
            "document_id": "VARCHAR",
            "source_file_id": "VARCHAR",
            "content": "VARCHAR",
            "embedding": "FLOAT_VECTOR",
            "sparse_embedding": "SPARSE_FLOAT_VECTOR",
            "citation_id": "VARCHAR",
        }
        if any(name not in fields or fields[name].data_type != data_type for name, data_type in required.items()):
            raise ValueError("CollectionIndexPlan 缺少固定 Hybrid RAG 字段")
        if not fields["chunk_id"].primary_key or not fields["content"].enable_analyzer:
            raise ValueError("chunk_id 必须是主键且 content 必须启用 analyzer")
        index_shapes = {(item.field_name, item.index_type, item.metric_type) for item in self.indexes}
        if ("embedding", "FLAT", "COSINE") not in index_shapes or (
            "sparse_embedding", "SPARSE_INVERTED_INDEX", "BM25"
        ) not in index_shapes:
            raise ValueError("CollectionIndexPlan 必须包含 Dense 和 BM25 sparse 索引")
        return self


class ResourceFieldMappingV1(ContractModel):
    """发布快照中可被 muye-data 消费的逻辑字段映射。"""

    id: str = Field(pattern=FIELD_NAME_PATTERN)
    content: str = Field(pattern=FIELD_NAME_PATTERN)
    vector: str = Field(pattern=FIELD_NAME_PATTERN)
    keyword: str = Field(pattern=FIELD_NAME_PATTERN)
    exposed_fields: dict[str, str] = Field(default_factory=dict)
    filterable_fields: dict[str, str] = Field(default_factory=dict)

    @field_validator("exposed_fields", "filterable_fields")
    @classmethod
    def validate_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        """公开和过滤映射均只能使用受限逻辑/物理标识。"""
        for logical_name, physical_name in value.items():
            if re.fullmatch(FIELD_NAME_PATTERN, logical_name) is None or re.fullmatch(
                FIELD_NAME_PATTERN, physical_name
            ) is None:
                raise ValueError("资源字段映射包含非法名称")
        return value


class PublishedPipelineV1(ContractModel):
    """已评测的资源 pipeline；参数范围与只读召回服务保持一致。"""

    type: Literal["dense", "keyword", "hybrid"]
    candidate_k: int | None = Field(default=None, ge=1, le=1000)
    dense_candidate_k: int | None = Field(default=None, ge=1, le=1000)
    keyword_candidate_k: int | None = Field(default=None, ge=1, le=1000)
    dense_weight: float | None = Field(default=None, gt=0, le=100)
    keyword_weight: float | None = Field(default=None, gt=0, le=100)
    rank_constant: int | None = Field(default=None, ge=1, le=10_000)
    rerank_model: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    rerank_required: bool = False

    @model_validator(mode="after")
    def validate_pipeline_shape(self) -> "PublishedPipelineV1":
        """每种检索链路只能声明自身需要的参数，防止静默重排或隐藏降级。"""
        if self.type in {"dense", "keyword"}:
            if self.candidate_k is None or any(
                value is not None
                for value in (
                    self.dense_candidate_k,
                    self.keyword_candidate_k,
                    self.dense_weight,
                    self.keyword_weight,
                    self.rank_constant,
                )
            ):
                raise ValueError("单通道 pipeline 必须且只能声明 candidate_k")
        elif self.candidate_k is not None or any(
            value is None
            for value in (
                self.dense_candidate_k,
                self.keyword_candidate_k,
                self.dense_weight,
                self.keyword_weight,
                self.rank_constant,
            )
        ):
            raise ValueError("hybrid pipeline 必须声明完整的 RRF 参数")
        if self.rerank_required and self.rerank_model is None:
            raise ValueError("required rerank 必须声明 rerank_model")
        return self


class KnowledgeResourceManifestV1(ContractModel):
    """已构建但尚未激活的逻辑 Resource 与不可变 Collection 绑定。"""

    schema_version: Literal["muye.ai/knowledge-resource-manifest/v1"]
    resource_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    resource_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    resource_checksum: str = Field(pattern=SHA256_PATTERN)
    knowledge_id: str = Field(pattern=KNOWLEDGE_ID_PATTERN)
    knowledge_version_id: str = Field(pattern=KNOWLEDGE_VERSION_PATTERN)
    collection_plan_checksum: str = Field(pattern=SHA256_PATTERN)
    connection: str = Field(pattern=IDENTIFIER_PATTERN)
    target: str = Field(pattern=COLLECTION_NAME_PATTERN)
    fields: ResourceFieldMappingV1
    embedding_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    embedding_dimensions: int = Field(ge=1, le=65_536)
    pipelines: dict[str, PublishedPipelineV1] = Field(min_length=3, max_length=12)
    default_pipeline: str = Field(pattern=IDENTIFIER_PATTERN)
    default_return_fields: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_manifest(self) -> "KnowledgeResourceManifestV1":
        """确保快照不会暴露向量字段或引用不存在的 pipeline。"""
        if self.default_pipeline not in self.pipelines:
            raise ValueError("default_pipeline 必须出现在 pipelines")
        if set(self.default_return_fields) - set(self.fields.exposed_fields):
            raise ValueError("default_return_fields 必须属于 exposed_fields")
        if self.fields.vector in self.fields.exposed_fields.values():
            raise ValueError("向量字段不能出现在 exposed_fields")
        if {item.type for item in self.pipelines.values()} != {"dense", "keyword", "hybrid"}:
            raise ValueError("发布 Resource 必须包含 Dense、Keyword 和 Hybrid pipeline")
        return self


class ResourceSnapshotV1(ContractModel):
    """muye-data 原子加载的不可变已发布 Resource 集合。"""

    schema_version: Literal["muye.ai/resource-snapshot/v1"]
    snapshot_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    snapshot_checksum: str = Field(pattern=SHA256_PATTERN)
    resources: dict[str, KnowledgeResourceManifestV1] = Field(min_length=1, max_length=100)

    @field_validator("snapshot_revision")
    @classmethod
    def validate_snapshot_revision(cls, value: str) -> str:
        """snapshot 版本只允许可审计逻辑引用。"""
        return _validate_safe_relative_reference(value, "snapshot_revision")

    @model_validator(mode="after")
    def validate_resource_names(self) -> "ResourceSnapshotV1":
        """字典 key 必须是 Resource 自身 ID，避免调用方看到两个身份。"""
        for name, resource in self.resources.items():
            if re.fullmatch(RESOURCE_ID_PATTERN, name) is None or name != resource.resource_id:
                raise ValueError("resources key 必须等于 resource_id")
        return self


class EvaluationCaseV1(ContractModel):
    """固定评测问题及其相关 chunk/citation 集。"""

    case_id: str = Field(pattern=IDENTIFIER_PATTERN)
    query: str = Field(min_length=1, max_length=2000)
    relevant_chunk_ids: list[str] = Field(min_length=1, max_length=100)
    required_citation_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("relevant_chunk_ids", "required_citation_ids")
    @classmethod
    def validate_unique_identifiers(cls, values: list[str]) -> list[str]:
        """评测目标必须是稳定的逻辑 ID，不能用任意查询表达式。"""
        if any(re.fullmatch(IDENTIFIER_PATTERN, value) is None for value in values):
            raise ValueError("评测目标必须是逻辑标识")
        if len(set(values)) != len(values):
            raise ValueError("评测目标不能重复")
        return values


class EvaluationSetV1(ContractModel):
    """评测基线及发布门禁，数值阈值不写入 Prompt。"""

    schema_version: Literal["muye.ai/evaluation-set/v1"]
    evaluation_set_id: str = Field(pattern=IDENTIFIER_PATTERN)
    revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    checksum: str = Field(pattern=SHA256_PATTERN)
    recall_at_k: int = Field(default=5, ge=1, le=100)
    min_recall: float = Field(ge=0, le=1)
    min_mrr: float = Field(ge=0, le=1)
    min_citation_coverage: float = Field(ge=0, le=1)
    cases: list[EvaluationCaseV1] = Field(min_length=1, max_length=10_000)

    @field_validator("revision")
    @classmethod
    def validate_evaluation_revision(cls, value: str) -> str:
        """评测 revision 是逻辑引用，不是文件路径或远端地址。"""
        return _validate_safe_relative_reference(value, "evaluation revision")

    @model_validator(mode="after")
    def validate_cases(self) -> "EvaluationSetV1":
        """case ID 必须唯一，以便报告可精确比对历史运行。"""
        identifiers = [case.case_id for case in self.cases]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("evaluation cases.case_id 不能重复")
        return self


class KnowledgeJobV1(ContractModel):
    """本地 Knowledge Worker 的可持久化作业状态。

    ``report_ref`` 只在完成状态中出现；取消是协作式的，Worker 会在每个可中断步骤
    前读取该状态，因此已经提交的不可变 Collection 不会被删除或覆盖。
    """

    schema_version: Literal["muye.ai/knowledge-job/v1"]
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    kind: Literal["build", "evaluate"]
    knowledge_slug: str = Field(pattern=SLUG_PATTERN)
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    attempt: int = Field(ge=1, le=10)
    created_at: str = Field(pattern=TIMESTAMP_PATTERN)
    updated_at: str = Field(pattern=TIMESTAMP_PATTERN)
    input_checksum: str = Field(pattern=SHA256_PATTERN)
    report_ref: str | None = Field(default=None, pattern=SAFE_REFERENCE_PATTERN)
    error_code: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_job_timestamp(cls, value: str, info: object) -> str:
        """作业时间必须带时区，便于跨进程审计和排序。"""
        return _validate_rfc3339_timestamp(value, getattr(info, "field_name", "job timestamp"))

    @model_validator(mode="after")
    def validate_job_terminal_fields(self) -> "KnowledgeJobV1":
        """成功/失败作业必须有报告，运行中作业不得伪造失败原因。"""
        if self.status in {"SUCCEEDED", "FAILED"} and self.report_ref is None:
            raise ValueError("完成作业必须包含 report_ref")
        if self.status in {"QUEUED", "RUNNING"} and (self.report_ref is not None or self.error_code is not None):
            raise ValueError("未完成作业不能包含 report_ref 或 error_code")
        if self.status == "SUCCEEDED" and self.error_code is not None:
            raise ValueError("成功作业不能包含 error_code")
        return self


CONTRACT_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "agent-build-record-v1": AgentBuildRecordV1,
    "agent-catalog-snapshot-v1": AgentCatalogSnapshotV1,
    "agent-descriptor-v1": AgentDescriptorV1,
    "agent-generation-spec-v1": AgentGenerationSpecV1,
    "collection-index-plan-v1": CollectionIndexPlanV1,
    "evaluation-set-v1": EvaluationSetV1,
    "knowledge-job-v1": KnowledgeJobV1,
    "knowledge-resource-manifest-v1": KnowledgeResourceManifestV1,
    "parsed-document-v1": ParsedDocumentV1,
    "resource-snapshot-v1": ResourceSnapshotV1,
    "schema-proposal-v1": SchemaProposalV1,
    "source-provenance-v1": SourceProvenanceV1,
}
