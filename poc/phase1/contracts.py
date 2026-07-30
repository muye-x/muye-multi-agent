"""阶段 1 PoC 的临时输入与中间产物契约。

这些模型验证 Markdown 到 Agent 目录的链路，不替代后续 Knowledge Worker 的正式契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CHECKSUM_PATTERN = r"^[a-f0-9]{64}$"
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class StrictPocModel(BaseModel):
    """PoC 数据的严格基类，拒绝隐式字段作为生成输入。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class SourceLocatorV1(StrictPocModel):
    """原始 Markdown 中一段内容的稳定位置。"""

    path: str = Field(min_length=1, max_length=512)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_line_order(self) -> "SourceLocatorV1":
        """结束行不能位于开始行之前。"""
        if self.end_line < self.start_line:
            raise ValueError("end_line 不能小于 start_line")
        return self


class ParsedBlockV1(StrictPocModel):
    """解析后的一个连续 Markdown 块，保留内容、顺序和可追溯位置。"""

    block_id: str = Field(pattern=r"^block_[a-f0-9]{16}$")
    ordinal: int = Field(ge=0)
    heading: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=100_000)
    source_locator: SourceLocatorV1


class ParsedDocumentV1(StrictPocModel):
    """PoC 的规范化 Markdown 文档，不含原始绝对路径和外部引用。"""

    schema_version: Literal["muye.ai/poc-parsed-document/v1"]
    document_id: str = Field(pattern=r"^doc_[a-f0-9]{16}$")
    title: str = Field(min_length=1, max_length=256)
    source_path: str = Field(min_length=1, max_length=512)
    source_checksum: str = Field(pattern=CHECKSUM_PATTERN)
    content_checksum: str = Field(pattern=CHECKSUM_PATTERN)
    blocks: list[ParsedBlockV1] = Field(min_length=1, max_length=10_000)

    @field_validator("source_path")
    @classmethod
    def validate_relative_source_path(cls, value: str) -> str:
        """来源只记录 workspace 内的相对路径，防止泄露宿主机目录。"""
        if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("source_path 必须是安全的相对路径")
        return value

    @model_validator(mode="after")
    def validate_block_order(self) -> "ParsedDocumentV1":
        """块序号和原始行号必须单调递增，供引用和确定性 checksum 使用。"""
        ordinals = [block.ordinal for block in self.blocks]
        if ordinals != list(range(len(self.blocks))):
            raise ValueError("blocks 的 ordinal 必须从 0 连续递增")
        line_numbers = [block.source_locator.start_line for block in self.blocks]
        if line_numbers != sorted(line_numbers):
            raise ValueError("blocks 必须按原始文档顺序排列")
        return self


class AgentProfileProposalV1(StrictPocModel):
    """进入 PoC 目录渲染前必须确认的受限 Agent 描述。"""

    schema_version: Literal["muye.ai/agent-profile-proposal/v1"]
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    supported_intents: list[str] = Field(min_length=1, max_length=20)
    instructions: str = Field(min_length=1, max_length=8000)
    do_not_use_when: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("supported_intents", "do_not_use_when")
    @classmethod
    def validate_text_items(cls, values: list[str]) -> list[str]:
        """拒绝空白或重复的展示文本，避免生成不稳定 Prompt。"""
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 256 for value in normalized):
            raise ValueError("列表项必须是 1 至 256 个字符")
        if len(set(normalized)) != len(normalized):
            raise ValueError("列表项不能重复")
        return normalized


class Phase1PocConfigV1(StrictPocModel):
    """PoC 使用的逻辑资源与固定检索范围，不允许物理 Collection 进入 Agent 输入。"""

    schema_version: Literal["muye.ai/phase1-poc-config/v1"]
    agent_slug: str = Field(min_length=1, max_length=63, pattern=SLUG_PATTERN)
    resource_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    resource_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./@:-]{0,255}$")
    model_alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    retrieval_pipeline: Literal["hybrid"] = "hybrid"
    scope_field: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
    scope_value: str = Field(min_length=1, max_length=256)
