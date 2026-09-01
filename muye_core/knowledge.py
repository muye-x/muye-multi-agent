"""阶段 2 知识构建与评测的 Core 领域编排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from contracts.v3 import AgentRevisionSpecV1, RuntimeResourceBindingV1

from .bundles import build_bundle, verify_bundle
from .service import DomainError


@dataclass(frozen=True, slots=True)
class KnowledgeBuildOutput:
    """构建后不可变 Collection 的受控逻辑描述。"""

    build_id: str
    resources: list[RuntimeResourceBindingV1]


@dataclass(frozen=True, slots=True)
class EvaluationOutput:
    """固定评测的最小审计结果，不包含资料正文。"""

    passed: bool
    pass_rate: float
    report_ref: str


class KnowledgeBackend(Protocol):
    """Core 对解析、Embedding、Milvus 与检索评测的受控适配边界。"""

    def build(self, spec: AgentRevisionSpecV1) -> KnowledgeBuildOutput:
        """从冻结 Revision 构建不可变资源；实现必须拒绝 Draft 或任意路径输入。"""

    def evaluate(self, spec: AgentRevisionSpecV1, build: KnowledgeBuildOutput) -> EvaluationOutput:
        """执行 Revision 固定评测并返回不含全文的审计摘要。"""


@dataclass(frozen=True, slots=True)
class ReadyRevisionOutput:
    """评测通过后可以交由 Runtime 的 Bundle 及其逻辑引用。"""

    bundle_checksum: str
    build_id: str
    report_ref: str
    resources: list[RuntimeResourceBindingV1]


def build_and_evaluate(spec: AgentRevisionSpecV1, backend: KnowledgeBackend) -> ReadyRevisionOutput:
    """执行不可变构建、固定评测及 Bundle 校验。

    后端失败向上传递，调用方负责将 Job 标记为可重试失败；评测未通过以稳定业务错误
    中止，不会生成 Bundle 或把 Revision 视为可部署。
    """

    build = backend.build(spec)
    if not build.resources:
        raise DomainError("RETRIEVAL_UNAVAILABLE", "知识构建未返回可用资源")
    evaluation = backend.evaluate(spec, build)
    if not evaluation.passed or evaluation.pass_rate < spec.evaluation.minimum_pass_rate:
        raise DomainError("EVALUATION_FAILED", "固定评测未达到 Revision 门禁")
    manifest, members = build_bundle(
        spec=spec,
        build_id=build.build_id,
        resources=build.resources,
        evaluation_summary={"passed": True, "pass_rate": evaluation.pass_rate, "report_ref": evaluation.report_ref},
    )
    verify_bundle(manifest, members)
    return ReadyRevisionOutput(manifest.bundle_checksum, build.build_id, evaluation.report_ref, build.resources)
