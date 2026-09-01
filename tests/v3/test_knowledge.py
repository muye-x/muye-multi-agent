"""阶段 2 知识执行编排的评测门禁测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.v3 import AgentRevisionSpecV1, RuntimeResourceBindingV1
from muye_core.knowledge import EvaluationOutput, KnowledgeBuildOutput, build_and_evaluate
from muye_core.service import DomainError


class _Backend:
    def __init__(self, *, passed: bool, pass_rate: float | None = None) -> None:
        self._passed = passed
        self._pass_rate = pass_rate

    def build(self, _spec: AgentRevisionSpecV1) -> KnowledgeBuildOutput:
        return KnowledgeBuildOutput(
            build_id="build_hotel_revision_2",
            resources=[RuntimeResourceBindingV1(resource_id="kb.hotel_employee", collection_name="kb_hotel_employee_revision_2", collection_checksum="5" * 64, embedding_alias="embedding_default")],
        )

    def evaluate(self, _spec: AgentRevisionSpecV1, _build: KnowledgeBuildOutput) -> EvaluationOutput:
        return EvaluationOutput(passed=self._passed, pass_rate=self._pass_rate if self._pass_rate is not None else (1.0 if self._passed else 0.0), report_ref="evaluations/revision/report.json")


def _spec() -> AgentRevisionSpecV1:
    return AgentRevisionSpecV1.model_validate(json.loads(Path("contracts/fixtures/agent-revision-v1.valid.json").read_text(encoding="utf-8")))


def test_knowledge_workflow_only_returns_bundle_after_passing_evaluation() -> None:
    """评测失败必须阻断 Bundle，成功结果包含可审计引用。"""

    with pytest.raises(DomainError, match="评测"):
        build_and_evaluate(_spec(), _Backend(passed=False))
    output = build_and_evaluate(_spec(), _Backend(passed=True))
    assert len(output.bundle_checksum) == 64
    assert output.build_id == "build_hotel_revision_2"


def test_knowledge_workflow_rejects_non_finite_evaluation_rate() -> None:
    """NaN 不得借由浮点比较语义绕过发布门禁。"""

    with pytest.raises(DomainError, match="有限"):
        build_and_evaluate(_spec(), _Backend(passed=True, pass_rate=float("nan")))
