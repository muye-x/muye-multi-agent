"""v3.0 平台在阶段 0 冻结的公开契约。

本模块只定义跨进程或跨语言边界的数据形状，不包含数据库、HTTP 或容器实现。所有
模型均拒绝未知字段，避免 UI、Runtime 或 Runner 把任意配置带入后续阶段。
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .models import (
    AGENT_ID_PATTERN,
    BLOCK_ID_PATTERN,
    BUILD_RECORD_ID_PATTERN,
    COLLECTION_NAME_PATTERN,
    IDENTIFIER_PATTERN,
    JOB_ID_PATTERN,
    SAFE_REFERENCE_PATTERN,
    SHA256_PATTERN,
    TOOL_NAME_PATTERN,
    ContractModel,
    _validate_rfc3339_timestamp,
    _validate_safe_relative_reference,
)


ASSET_ID_PATTERN = r"^asset_[a-f0-9]{16,64}$"
DEPLOYMENT_ID_PATTERN = r"^deployment_[a-f0-9]{16,64}$"
REVISION_ID_PATTERN = r"^revision_[a-f0-9]{16,64}$"
SESSION_ID_PATTERN = r"^session_[A-Za-z0-9_-]{8,128}$"
REQUEST_ID_PATTERN = r"^request_[a-f0-9]{16,64}$"
TOOL_CALL_ID_PATTERN = r"^tool_[a-f0-9]{16,64}$"


def _canonical_checksum(value: object) -> str:
    """计算不受 JSON 键顺序和空白影响的 SHA-256。"""

    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


class RevisionSourceAssetV1(ContractModel):
    """一个被冻结到 Agent Revision 的内容寻址资料引用。"""

    asset_id: str = Field(pattern=ASSET_ID_PATTERN)
    sha256: str = Field(pattern=SHA256_PATTERN)
    display_name: str = Field(min_length=1, max_length=255)


class AgentModelConfigV1(ContractModel):
    """Revision 固定的模型 alias 与采样参数，不包含 provider 凭据。"""

    chat_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    embedding_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    temperature: float = Field(ge=0, le=2)


class AgentRetrievalConfigV1(ContractModel):
    """Revision 固定的只读检索策略。"""

    pipeline: Literal["dense", "keyword", "hybrid"]
    top_k: int = Field(ge=1, le=100)
    rerank_alias: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    minimum_score: float = Field(ge=0, le=1)


class AgentBudgetV1(ContractModel):
    """单次 Runtime 调用的资源上限。"""

    output_tokens: int = Field(ge=128, le=65_536)
    tool_calls: int = Field(ge=1, le=20)
    timeout_seconds: int = Field(ge=1, le=300)


class AgentEvaluationCaseV1(ContractModel):
    """Revision 发布前必须重放的一条可审阅评测用例。"""

    case_id: str = Field(pattern=IDENTIFIER_PATTERN)
    question: str = Field(min_length=1, max_length=2_000)
    expected_source_asset_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("expected_source_asset_ids")
    @classmethod
    def validate_asset_ids(cls, values: list[str]) -> list[str]:
        """每个用例只能引用当前 Revision 中稳定的 Asset ID。"""

        if any(re.fullmatch(ASSET_ID_PATTERN, value) is None for value in values):
            raise ValueError("expected_source_asset_ids 必须是稳定的 asset_id")
        if len(set(values)) != len(values):
            raise ValueError("expected_source_asset_ids 不能重复")
        return values


class AgentEvaluationConfigV1(ContractModel):
    """检索与 citation 发布门禁；阈值不写入 Prompt。"""

    cases: list[AgentEvaluationCaseV1] = Field(min_length=1, max_length=10_000)
    minimum_pass_rate: float = Field(ge=0, le=1)
    citation_required: bool

    @model_validator(mode="after")
    def validate_case_ids(self) -> "AgentEvaluationConfigV1":
        """case_id 必须唯一，保证报告可在 Revision 间比较。"""

        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("evaluation.cases 的 case_id 不能重复")
        return self


class AgentRevisionSpecV1(ContractModel):
    """声明式知识 Agent 的完整不可变逻辑输入。

    该对象不包含数据库地址、Artifact 路径、镜像、容器参数或任何凭据。Revision
    checksum 由 ``revision_spec_checksum`` 从本对象规范化计算，写入数据库和 Bundle
    manifest，而不作为可由调用方伪造的自身字段。
    """

    schema_version: Literal["muye.ai/agent-revision/v1"]
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    revision_number: int = Field(ge=1, le=2_147_483_647)
    display_name: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=2_000)
    instructions: str = Field(min_length=1, max_length=20_000)
    prohibited_actions: list[str] = Field(min_length=1, max_length=100)
    examples: list[str] = Field(min_length=1, max_length=100)
    model: AgentModelConfigV1
    retrieval: AgentRetrievalConfigV1
    budgets: AgentBudgetV1
    source_assets: list[RevisionSourceAssetV1] = Field(min_length=1, max_length=10_000)
    evaluation: AgentEvaluationConfigV1

    @field_validator("prohibited_actions", "examples")
    @classmethod
    def validate_text_list(cls, values: list[str]) -> list[str]:
        """防止空白规则或重复示例进入冻结版本。"""

        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("列表项不能是空白文本")
        if len(set(normalized)) != len(normalized):
            raise ValueError("列表项不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_references(self) -> "AgentRevisionSpecV1":
        """评测用例必须只引用本 Revision 实际冻结的资料。"""

        asset_ids = [asset.asset_id for asset in self.source_assets]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("source_assets 的 asset_id 不能重复")
        known_assets = set(asset_ids)
        expected_assets = {
            asset_id
            for case in self.evaluation.cases
            for asset_id in case.expected_source_asset_ids
        }
        if not expected_assets.issubset(known_assets):
            raise ValueError("评测用例引用了不属于本 Revision 的资料")
        return self


def revision_spec_checksum(spec: AgentRevisionSpecV1) -> str:
    """返回冻结 Revision 逻辑输入的稳定 checksum。"""

    return _canonical_checksum(spec.model_dump(mode="json"))


class RuntimeResourceBindingV1(ContractModel):
    """Runtime 可读取的逻辑资源与不可变 Collection 映射。"""

    resource_id: str = Field(pattern=IDENTIFIER_PATTERN)
    collection_name: str = Field(pattern=COLLECTION_NAME_PATTERN)
    collection_checksum: str = Field(pattern=SHA256_PATTERN)
    embedding_alias: str = Field(pattern=IDENTIFIER_PATTERN)


class AgentRevisionBundleManifestV1(ContractModel):
    """声明式 Runtime Bundle 的无密钥 manifest。"""

    schema_version: Literal["muye.ai/agent-revision-bundle/v1"]
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    revision_checksum: str = Field(pattern=SHA256_PATTERN)
    bundle_checksum: str = Field(pattern=SHA256_PATTERN)
    build_id: str = Field(pattern=BUILD_RECORD_ID_PATTERN)
    runtime_contract_version: Literal["muye-runtime/1"]
    resources: list[RuntimeResourceBindingV1] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_resources(self) -> "AgentRevisionBundleManifestV1":
        """一个逻辑资源在 Bundle 中只能映射到一个 Collection。"""

        resource_ids = [resource.resource_id for resource in self.resources]
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("resources 的 resource_id 不能重复")
        return self


class JobEventV1(ContractModel):
    """可恢复 Job SSE 的一条严格事件。

    ``sequence`` 在同一 Job 内单调递增；消费者可以以它作为 Last-Event-ID 恢复点。
    字段按事件类型受限，避免将任意日志、路径或异常对象写入事件流。
    """

    schema_version: Literal["muye.ai/job-event/v1"]
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    sequence: int = Field(ge=0, le=2_147_483_647)
    event_type: Literal["started", "progress", "artifact", "completed", "failed", "cancelled"]
    emitted_at: str
    stage: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    message: str | None = Field(default=None, min_length=1, max_length=1_000)
    progress_current: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=1)
    artifact_ref: str | None = Field(default=None, max_length=256, pattern=SAFE_REFERENCE_PATTERN)
    error_code: str | None = Field(default=None, max_length=128, pattern=IDENTIFIER_PATTERN)

    @field_validator("emitted_at")
    @classmethod
    def validate_emitted_at(cls, value: str) -> str:
        """所有 Job 事件必须使用带时区的 RFC 3339 时间。"""

        return _validate_rfc3339_timestamp(value, "emitted_at")

    @field_validator("artifact_ref")
    @classmethod
    def validate_artifact_ref(cls, value: str | None) -> str | None:
        """Artifact 只能通过受控相对引用公开。"""

        if value is not None:
            _validate_safe_relative_reference(value, "artifact_ref")
        return value

    @model_validator(mode="after")
    def validate_event_payload(self) -> "JobEventV1":
        """为每个事件类型限定必要字段和禁止字段。"""

        has_progress = self.progress_current is not None or self.progress_total is not None
        if has_progress:
            if self.progress_current is None or self.progress_total is None:
                raise ValueError("progress 事件必须同时包含 progress_current 和 progress_total")
            if self.progress_current > self.progress_total:
                raise ValueError("progress_current 不能大于 progress_total")
        if self.event_type == "progress" and not has_progress:
            raise ValueError("progress 事件必须包含进度")
        if self.event_type == "artifact" and self.artifact_ref is None:
            raise ValueError("artifact 事件必须包含 artifact_ref")
        if self.event_type == "failed" and self.error_code is None:
            raise ValueError("failed 事件必须包含 error_code")
        if self.event_type in {"completed", "cancelled"} and self.error_code is not None:
            raise ValueError("completed/cancelled 事件不能包含 error_code")
        return self


class RuntimeCitationV1(ContractModel):
    """Runtime 返回给 Core 的最小可审计 citation。"""

    citation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_asset_id: str = Field(pattern=ASSET_ID_PATTERN)
    locator: str = Field(min_length=1, max_length=512)


class RuntimeInvokeRequestV1(ContractModel):
    """Core 发给固定 Runtime 的单次调用请求。"""

    schema_version: Literal["muye.ai/runtime-invoke-request/v1"]
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    user_id: str = Field(pattern=IDENTIFIER_PATTERN)
    task: str = Field(min_length=1, max_length=20_000)


class RuntimeInvokeResponseV1(ContractModel):
    """Runtime 的完成态响应；流式调用以同等语义的 ChatStreamEventV1 表达。"""

    schema_version: Literal["muye.ai/runtime-invoke-response/v1"]
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    status: Literal["success", "refused", "error"]
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    citations: list[RuntimeCitationV1] = Field(default_factory=list, max_length=100)
    error_code: str | None = Field(default=None, max_length=128, pattern=IDENTIFIER_PATTERN)
    error_message: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "RuntimeInvokeResponseV1":
        """成功、拒答和失败必须采用互斥且可展示的结果形态。"""

        if self.status == "success":
            if self.content is None or self.error_code is not None or self.error_message is not None:
                raise ValueError("success 响应必须包含 content 且不能包含错误")
        else:
            if self.error_code is None or self.error_message is None:
                raise ValueError("refused/error 响应必须包含 error_code 和 error_message")
            if self.content is not None or self.citations:
                raise ValueError("refused/error 响应不能包含 content 或 citations")
        return self


class RuntimeCancelRequestV1(ContractModel):
    """Core 对 Runtime 的协作式取消请求。"""

    schema_version: Literal["muye.ai/runtime-cancel-request/v1"]
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    reason: Literal["client_disconnect", "timeout", "deployment_drain", "operator"]


class RuntimeCapabilitiesV1(ContractModel):
    """固定 Runtime 在 readiness 后对 Core 声明的只读能力。"""

    schema_version: Literal["muye.ai/runtime-capabilities/v1"]
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    revision_checksum: str = Field(pattern=SHA256_PATTERN)
    runtime_contract_version: Literal["muye-runtime/1"]
    supports_streaming: Literal[True]
    supports_cancel: Literal[True]


class ChatStreamEventV1(ContractModel):
    """Core 到客户端的规范化聊天 SSE 事件。

    ``event_type`` 同时作为 SSE event 名和 JSON 数据的类型值；``sequence`` 在会话内
    单调递增。工具输入、原始模型思考和内部异常不进入该公开契约。
    """

    schema_version: Literal["muye.ai/chat-stream-event/v1"]
    event_type: Literal[
        "session_start",
        "block_delta",
        "thinking_delta",
        "tool_start",
        "tool_update",
        "tool_complete",
        "done",
        "error",
        "session_end",
    ]
    sequence: int = Field(ge=0, le=2_147_483_647)
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    block_id: str | None = Field(default=None, pattern=BLOCK_ID_PATTERN)
    delta: str | None = Field(default=None, min_length=1, max_length=20_000)
    tool_call_id: str | None = Field(default=None, pattern=TOOL_CALL_ID_PATTERN)
    tool_name: str | None = Field(default=None, pattern=TOOL_NAME_PATTERN)
    citations: list[RuntimeCitationV1] = Field(default_factory=list, max_length=100)
    error_code: str | None = Field(default=None, max_length=128, pattern=IDENTIFIER_PATTERN)
    message: str | None = Field(default=None, min_length=1, max_length=1_000)
    total_tokens: int | None = Field(default=None, ge=0, le=65_536)

    @model_validator(mode="after")
    def validate_event_payload(self) -> "ChatStreamEventV1":
        """拒绝不完整 delta、工具和错误事件，保证客户端可确定性消费。"""

        if self.event_type == "block_delta" and (self.block_id is None or self.delta is None):
            raise ValueError("block_delta 必须包含 block_id 和 delta")
        if self.event_type == "thinking_delta" and self.delta is None:
            raise ValueError("thinking_delta 必须包含 delta")
        if self.event_type in {"tool_start", "tool_update", "tool_complete"}:
            if self.tool_call_id is None or self.tool_name is None:
                raise ValueError("工具事件必须包含 tool_call_id 和 tool_name")
        if self.event_type == "error" and (self.error_code is None or self.message is None):
            raise ValueError("error 事件必须包含 error_code 和 message")
        if self.event_type == "done" and self.total_tokens is None:
            raise ValueError("done 事件必须包含 total_tokens")
        return self


V3_CONTRACT_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "agent-revision-bundle-v1": AgentRevisionBundleManifestV1,
    "agent-revision-v1": AgentRevisionSpecV1,
    "chat-stream-event-v1": ChatStreamEventV1,
    "job-event-v1": JobEventV1,
    "runtime-capabilities-v1": RuntimeCapabilitiesV1,
    "runtime-cancel-request-v1": RuntimeCancelRequestV1,
    "runtime-invoke-request-v1": RuntimeInvokeRequestV1,
    "runtime-invoke-response-v1": RuntimeInvokeResponseV1,
}
