"""生成可重复比较的 v2 质量基线报告。

阶段 0 使用已记录的离线观测，而非在 CI 中调用模型或 Milvus。该工具验证每条观测
包含 Dense、Keyword、Hybrid 检索、citation、拒答和资源消耗信息，再生成规范 JSON
报告；后续 v3 实现可用同一输入格式记录实际运行结果并进行比较。
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from contracts.models import IDENTIFIER_PATTERN


class _BaselineModel(BaseModel):
    """阶段 0 基线记录的严格 Pydantic 配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class RetrievalObservationV1(_BaselineModel):
    """一条固定查询在一个检索 pipeline 下的排序结果。"""

    pipeline: Literal["dense", "keyword", "hybrid"]
    returned_chunk_ids: list[str] = Field(max_length=100)

    @field_validator("returned_chunk_ids")
    @classmethod
    def validate_chunk_ids(cls, values: list[str]) -> list[str]:
        """Chunk ID 必须是稳定逻辑标识，且排序结果不允许重复。"""

        import re

        if any(re.fullmatch(IDENTIFIER_PATTERN, value) is None for value in values):
            raise ValueError("returned_chunk_ids 必须是逻辑标识")
        if len(set(values)) != len(values):
            raise ValueError("returned_chunk_ids 不能重复")
        return values


class BaselineCaseV1(_BaselineModel):
    """一条 v2 资料结构与用户行为基线。"""

    case_id: str = Field(pattern=IDENTIFIER_PATTERN)
    fixture_kind: Literal["markdown", "pdf", "docx"]
    fixture_id: str = Field(pattern=IDENTIFIER_PATTERN)
    expected_outcome: Literal["answer", "refusal"]
    actual_outcome: Literal["answer", "refusal"]
    relevant_chunk_ids: list[str] = Field(max_length=100)
    required_citation_ids: list[str] = Field(max_length=100)
    returned_citation_ids: list[str] = Field(max_length=100)
    retrieval: list[RetrievalObservationV1] = Field(min_length=1, max_length=3)
    latency_ms: int = Field(ge=0, le=300_000)
    input_tokens: int = Field(ge=0, le=1_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000)

    @field_validator("relevant_chunk_ids", "required_citation_ids", "returned_citation_ids")
    @classmethod
    def validate_identifiers(cls, values: list[str]) -> list[str]:
        """评测中的资料和 citation 身份必须可比较且不重复。"""

        import re

        if any(re.fullmatch(IDENTIFIER_PATTERN, value) is None for value in values):
            raise ValueError("基线标识必须是逻辑标识")
        if len(set(values)) != len(values):
            raise ValueError("基线标识不能重复")
        return values

    @model_validator(mode="after")
    def validate_case_shape(self) -> "BaselineCaseV1":
        """确保三类检索均被记录，且拒答不会伪造资料命中。"""

        pipelines = {item.pipeline for item in self.retrieval}
        if pipelines != {"dense", "keyword", "hybrid"}:
            raise ValueError("每个 case 必须记录 dense、keyword、hybrid 三类检索")
        if self.expected_outcome == "refusal" and (
            self.relevant_chunk_ids or self.required_citation_ids
        ):
            raise ValueError("拒答 case 不能声明相关资料或要求 citation")
        return self


class QualityBaselineInputV1(_BaselineModel):
    """阶段 0 冻结的 v2 离线质量观测。"""

    schema_version: Literal["muye.ai/v2-quality-baseline-input/v1"]
    baseline_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_revision: str = Field(pattern=IDENTIFIER_PATTERN)
    cases: list[BaselineCaseV1] = Field(min_length=3, max_length=10_000)

    @model_validator(mode="after")
    def validate_cases(self) -> "QualityBaselineInputV1":
        """基线至少覆盖三类资料结构，且 case ID 唯一。"""

        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("baseline case_id 不能重复")
        if {case.fixture_kind for case in self.cases} != {"markdown", "pdf", "docx"}:
            raise ValueError("基线必须覆盖 markdown、pdf、docx 三类资料结构")
        outcomes = {case.expected_outcome for case in self.cases}
        if outcomes != {"answer", "refusal"}:
            raise ValueError("基线必须同时覆盖 answer 与 refusal")
        return self


def _canonical_checksum(value: object) -> str:
    """对 JSON 兼容值返回确定性 SHA-256。"""

    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def _recall_at_k(returned: list[str], relevant: list[str]) -> float | None:
    """计算单 case 覆盖率；拒答 case 不参与检索质量平均。"""

    if not relevant:
        return None
    return len(set(returned).intersection(relevant)) / len(set(relevant))


def _mrr(returned: list[str], relevant: list[str]) -> float | None:
    """计算首个相关资料的倒数排名。"""

    if not relevant:
        return None
    expected = set(relevant)
    for position, chunk_id in enumerate(returned, start=1):
        if chunk_id in expected:
            return 1.0 / position
    return 0.0


def _mean(values: list[float]) -> float:
    """返回稳定的小数均值；调用方保证列表非空。"""

    return round(sum(values) / len(values), 6)


def build_report(baseline: QualityBaselineInputV1) -> dict[str, object]:
    """从冻结观测构造机器可比较的质量、延迟和 token 报告。"""

    pipeline_metrics: dict[str, dict[str, float | int]] = {}
    for pipeline in ("dense", "keyword", "hybrid"):
        recalls: list[float] = []
        mrrs: list[float] = []
        for case in baseline.cases:
            observation = next(item for item in case.retrieval if item.pipeline == pipeline)
            recall = _recall_at_k(observation.returned_chunk_ids, case.relevant_chunk_ids)
            reciprocal_rank = _mrr(observation.returned_chunk_ids, case.relevant_chunk_ids)
            if recall is not None and reciprocal_rank is not None:
                recalls.append(recall)
                mrrs.append(reciprocal_rank)
        pipeline_metrics[pipeline] = {
            "evaluated_cases": len(recalls),
            "recall": _mean(recalls),
            "mrr": _mean(mrrs),
        }

    answer_cases = [case for case in baseline.cases if case.expected_outcome == "answer"]
    citation_scores = [
        len(set(case.returned_citation_ids).intersection(case.required_citation_ids))
        / len(set(case.required_citation_ids))
        for case in answer_cases
    ]
    refusal_cases = [case for case in baseline.cases if case.expected_outcome == "refusal"]
    refusal_accuracy = _mean(
        [1.0 if case.actual_outcome == "refusal" else 0.0 for case in refusal_cases]
    )
    case_results = [
        {
            "case_id": case.case_id,
            "fixture_kind": case.fixture_kind,
            "expected_outcome": case.expected_outcome,
            "actual_outcome": case.actual_outcome,
            "latency_ms": case.latency_ms,
            "input_tokens": case.input_tokens,
            "output_tokens": case.output_tokens,
        }
        for case in baseline.cases
    ]
    report_without_checksum: dict[str, object] = {
        "schema_version": "muye.ai/v2-quality-baseline-report/v1",
        "baseline_id": baseline.baseline_id,
        "source_revision": baseline.source_revision,
        "input_checksum": _canonical_checksum(baseline.model_dump(mode="json")),
        "cases": case_results,
        "summary": {
            "case_count": len(baseline.cases),
            "pipeline_metrics": pipeline_metrics,
            "citation_coverage": _mean(citation_scores),
            "refusal_accuracy": refusal_accuracy,
            "mean_latency_ms": _mean([float(case.latency_ms) for case in baseline.cases]),
            "total_input_tokens": sum(case.input_tokens for case in baseline.cases),
            "total_output_tokens": sum(case.output_tokens for case in baseline.cases),
        },
    }
    return {
        **report_without_checksum,
        "report_checksum": _canonical_checksum(report_without_checksum),
    }


def load_baseline(path: Path) -> QualityBaselineInputV1:
    """读取并验证已审阅的离线观测文件。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取基线文件：{path}") from exc
    return QualityBaselineInputV1.model_validate(payload)


def main() -> None:
    """生成报告；写入路径必须由调用方显式指定。"""

    parser = argparse.ArgumentParser(description="生成 v2 质量基线报告")
    parser.add_argument("--input", type=Path, required=True, help="已审阅的基线观测 JSON")
    parser.add_argument("--output", type=Path, required=True, help="输出报告 JSON")
    arguments = parser.parse_args()
    try:
        report = build_report(load_baseline(arguments.input))
    except (ValidationError, ValueError) as exc:
        parser.error(str(exc))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
