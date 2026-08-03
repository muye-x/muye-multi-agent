"""Knowledge Worker 输入、审批与内部产物模型。"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from contracts.models import (
    IDENTIFIER_PATTERN,
    KNOWLEDGE_ID_PATTERN,
    SAFE_REFERENCE_PATTERN,
    SHA256_PATTERN,
    SLUG_PATTERN,
    TIMESTAMP_PATTERN,
    ChunkingPolicyV1,
    ContractModel,
    SourceLocatorV1,
)


_ALLOWED_SOURCE_GLOBS = {"**/*.pdf", "**/*.docx", "**/*.md", "**/*.txt"}


def _validate_relative_path(value: str, field_name: str) -> str:
    """仅允许导入根内的 POSIX 相对路径，不接受 URL、盘符或路径遍历。"""
    if not value or len(value) > 1024 or value.startswith(("/", "\\")) or "\\" in value or "://" in value:
        raise ValueError(f"{field_name} 必须是安全的相对路径")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{field_name} 不能包含空、当前目录或父目录片段")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} 不能包含控制字符")
    return value


class KnowledgeSourceSpecV1(ContractModel):
    """一个导入根内的文件或目录与固定格式 allowlist。"""

    path: str = Field(min_length=1, max_length=1024)
    include: list[str] = Field(min_length=1, max_length=4)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """配置只能选择导入根内的相对源位置。"""
        return _validate_relative_path(value, "sources.path")

    @field_validator("include")
    @classmethod
    def validate_include(cls, values: list[str]) -> list[str]:
        """仅允许阶段 4 支持的四种文档扩展名。"""
        if any(value not in _ALLOWED_SOURCE_GLOBS for value in values):
            raise ValueError("sources.include 只允许 PDF/DOCX/MD/TXT 固定 glob")
        if len(set(values)) != len(values):
            raise ValueError("sources.include 不能重复")
        return values


class KnowledgeSourceConfigV1(ContractModel):
    """写侧知识构建配置，不复用阶段 3 的 Generator 逻辑输入。

    源路径相对于 CLI 显式传入的 ``--import-root``，配置中不接受绝对路径、远程 URL、
    Milvus Collection 名称或任何凭据。
    """

    schema_version: Literal["muye.ai/knowledge-source-config/v1"]
    knowledge_id: str = Field(pattern=KNOWLEDGE_ID_PATTERN)
    resource_id: str = Field(pattern=KNOWLEDGE_ID_PATTERN)
    slug: str = Field(pattern=SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    sources: list[KnowledgeSourceSpecV1] = Field(min_length=1, max_length=32)
    parser_profile: Literal["docling-default-v1", "deterministic-text-v1"] = "docling-default-v1"
    embedding_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    embedding_revision: str = Field(default="r1", pattern=IDENTIFIER_PATTERN)
    embedding_dimensions: int = Field(ge=1, le=65_536)
    connection: str = Field(pattern=IDENTIFIER_PATTERN)
    chunking: ChunkingPolicyV1
    keyword_analyzer: Literal["jieba"] = "jieba"
    default_pipeline: Literal["hybrid"] = "hybrid"
    rerank_alias: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    rerank_required: bool = False
    evaluation_set_ref: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_source_files: int = Field(default=1_000, ge=1, le=10_000)
    max_total_source_bytes: int = Field(default=100 * 1024 * 1024, ge=1, le=1_024 * 1024 * 1024)
    max_pdf_pages: int = Field(default=1_000, ge=1, le=10_000)
    max_docx_archive_entries: int = Field(default=10_000, ge=1, le=100_000)
    max_docx_uncompressed_bytes: int = Field(default=100 * 1024 * 1024, ge=1, le=1_024 * 1024 * 1024)
    max_parsed_blocks: int = Field(default=100_000, ge=1, le=1_000_000)
    max_chunks: int = Field(default=10_000, ge=1, le=10_000)
    max_total_chunk_characters: int = Field(default=20_000_000, ge=1, le=100_000_000)
    embedding_batch_size: int = Field(default=64, ge=1, le=256)

    @field_validator("evaluation_set_ref")
    @classmethod
    def validate_evaluation_set_ref(cls, value: str) -> str:
        """评测文件引用只能位于受控工作区，不是 URL 或绝对路径。"""
        return _validate_relative_path(value, "evaluation_set_ref")

    @model_validator(mode="after")
    def validate_rerank(self) -> "KnowledgeSourceConfigV1":
        """required rerank 不能缺失 alias，避免评测与运行时静默不一致。"""
        if self.rerank_required and self.rerank_alias is None:
            raise ValueError("rerank_required=true 时必须提供 rerank_alias")
        return self


class SchemaApprovalV1(ContractModel):
    """人工确认的 Schema Proposal 证明，与阶段 3 的 Resource 审批目录隔离。"""

    schema_version: Literal["muye.ai/schema-approval/v1"]
    knowledge_slug: str = Field(pattern=SLUG_PATTERN)
    proposal_revision: str = Field(pattern=SAFE_REFERENCE_PATTERN)
    proposal_checksum: str = Field(pattern=SHA256_PATTERN)
    approved_by: str = Field(pattern=IDENTIFIER_PATTERN)
    approved_at: str = Field(pattern=TIMESTAMP_PATTERN)

    @field_validator("proposal_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """审批记录不得把路径或 URL 带入 worker。"""
        return _validate_relative_path(value, "proposal_revision")

    @field_validator("approved_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        """确认时间必须为可审计的 RFC 3339 时间。"""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("approved_at 必须是有效 RFC 3339 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("approved_at 必须包含时区")
        return value


class KnowledgeChunkV1(ContractModel):
    """待向量化并发布到不可变 Collection 的确定性文本块。"""

    chunk_id: str = Field(pattern=r"^chunk_[a-f0-9]{16,64}$")
    knowledge_version_id: str = Field(pattern=r"^kv_[a-z0-9][a-z0-9_-]{2,63}$")
    document_id: str = Field(pattern=r"^doc_[a-f0-9]{16,64}$")
    source_file_id: str = Field(pattern=r"^file_[a-f0-9]{16,64}$")
    content: str = Field(min_length=1, max_length=12_000)
    title: str = Field(min_length=1, max_length=512)
    citation_id: str = Field(pattern=r"^citation_[a-f0-9]{16,64}$")
    source_locators: list[SourceLocatorV1] = Field(min_length=1, max_length=256)
    block_ids: list[str] = Field(min_length=1, max_length=256)
    chunk_index: int = Field(ge=0)
    content_hash: str = Field(pattern=SHA256_PATTERN)

    @field_validator("block_ids")
    @classmethod
    def validate_block_ids(cls, values: list[str]) -> list[str]:
        """chunk 只能引用稳定 block ID，避免任意 locator 字符串进入发布记录。"""
        if any(re.fullmatch(r"^block_[a-f0-9]{16,64}$", value) is None for value in values):
            raise ValueError("block_ids 必须是稳定 block ID")
        if len(set(values)) != len(values):
            raise ValueError("block_ids 不能重复")
        return values


class RetrievedEvaluationHitV1(ContractModel):
    """评测器消费的最小只读检索结果。"""

    chunk_id: str = Field(min_length=1, max_length=256, pattern=IDENTIFIER_PATTERN)
    citation_id: str | None = Field(default=None, max_length=256, pattern=IDENTIFIER_PATTERN)
