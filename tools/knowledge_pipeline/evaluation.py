"""固定 RAG 评测、citation coverage 计算和 Resource 发布门禁。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from contracts.models import EvaluationSetV1, KnowledgeResourceManifestV1

from .models import RetrievedEvaluationHitV1


@dataclass(frozen=True)
class ResourceSnapshotIdentity:
    """隔离 muye-data 实例实际加载的单个 Resource 身份证明。"""

    snapshot_revision: str
    snapshot_checksum: str
    resource_id: str
    resource_revision: str
    resource_checksum: str
    knowledge_version_id: str
    collection_plan_checksum: str


class RetrievalRunner(Protocol):
    """评测对只读检索面的最小依赖，便于使用 fake 复现固定排名。"""

    def retrieve(
        self,
        *,
        resource_id: str,
        query: str,
        pipeline: str,
        top_k: int,
        trace_id: str,
    ) -> list[RetrievedEvaluationHitV1]:
        """返回有序命中；任何依赖错误由实现抛出，不允许伪造空成功。"""

    def snapshot_identity(self, *, resource_id: str) -> ResourceSnapshotIdentity:
        """返回当前服务加载的 Resource 身份，用于证明评测针对 candidate Snapshot。"""


class MuyeDataRetrievalRunner:
    """通过 muye-data 只读 API 运行评测，不直接操作 Milvus 或写入服务。"""

    def __init__(self, *, base_url: str, timeout_seconds: float = 30.0) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("muye-data URL 必须是不含凭据的 HTTP(S) URL")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def retrieve(
        self,
        *,
        resource_id: str,
        query: str,
        pipeline: str,
        top_k: int,
        trace_id: str,
    ) -> list[RetrievedEvaluationHitV1]:
        """请求当前候选 Resource 的已配置 pipeline，并提取已公开 citation 字段。"""
        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout_seconds) as client:
                response = client.post(
                    "/api/v1/retrieve",
                    json={
                        "resource": resource_id,
                        "query": query,
                        "pipeline": pipeline,
                        "top_k": top_k,
                        "return_fields": ["citation_id"],
                        "trace_id": trace_id,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError("muye-data 评测请求失败") from exc
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            raise RuntimeError("muye-data 评测响应不包含 hits")
        normalized: list[RetrievedEvaluationHitV1] = []
        for hit in hits:
            if not isinstance(hit, dict) or not isinstance(hit.get("id"), str):
                raise RuntimeError("muye-data 评测响应包含非法 hit")
            fields = hit.get("fields")
            citation_id = fields.get("citation_id") if isinstance(fields, dict) else None
            normalized.append(
                RetrievedEvaluationHitV1(chunk_id=hit["id"], citation_id=citation_id))
        return normalized

    def snapshot_identity(self, *, resource_id: str) -> ResourceSnapshotIdentity:
        """读取只读运行时身份投影，拒绝静态配置或其他 Snapshot 伪装为 candidate。"""
        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout_seconds) as client:
                response = client.get("/api/v1/snapshot-identity")
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError("muye-data Snapshot 身份请求失败") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("resources"), dict):
            raise RuntimeError("muye-data Snapshot 身份响应无效")
        resource = payload["resources"].get(resource_id)
        if not isinstance(resource, dict):
            raise RuntimeError("muye-data Snapshot 未加载待评测 Resource")
        values = {
            "snapshot_revision": payload.get("snapshot_revision"),
            "snapshot_checksum": payload.get("snapshot_checksum"),
            "resource_id": resource.get("resource_id"),
            "resource_revision": resource.get("resource_revision"),
            "resource_checksum": resource.get("resource_checksum"),
            "knowledge_version_id": resource.get("knowledge_version_id"),
            "collection_plan_checksum": resource.get("collection_plan_checksum"),
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            raise RuntimeError("muye-data Snapshot 身份响应字段无效")
        return ResourceSnapshotIdentity(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PipelineEvaluation:
    """单个 pipeline 的固定评测聚合指标。"""

    recall: float
    mrr: float
    citation_coverage: float
    cases: int
    passed: bool


@dataclass(frozen=True)
class EvaluationResult:
    """全部必需 pipeline 的指标与最终发布决定。"""

    pipelines: dict[str, PipelineEvaluation]
    passed: bool

    def as_report(self) -> dict[str, object]:
        """返回稳定的 JSON 兼容报告内容。"""
        return {
            "passed": self.passed,
            "pipelines": {
                name: {
                    "recall": metrics.recall,
                    "mrr": metrics.mrr,
                    "citation_coverage": metrics.citation_coverage,
                    "cases": metrics.cases,
                    "passed": metrics.passed,
                }
                for name, metrics in sorted(self.pipelines.items())
            },
        }


def evaluate_resource(
    *,
    evaluation_set: EvaluationSetV1,
    manifest: KnowledgeResourceManifestV1,
    runner: RetrievalRunner,
) -> EvaluationResult:
    """评测 Dense、BM25、Hybrid 和可选 Hybrid+Rerank，并执行数值发布门禁。"""
    required = {"dense", "keyword", "hybrid"}
    available = set(manifest.pipelines)
    missing = required - available
    if missing:
        raise ValueError(f"Resource Manifest 缺少必需评测 pipeline：{sorted(missing)}")
    pipeline_names = sorted(available)
    metrics: dict[str, PipelineEvaluation] = {}
    for pipeline in pipeline_names:
        metrics[pipeline] = _evaluate_pipeline(
            evaluation_set=evaluation_set,
            manifest=manifest,
            runner=runner,
            pipeline=pipeline,
        )
    return EvaluationResult(pipelines=metrics, passed=all(item.passed for item in metrics.values()))


def _evaluate_pipeline(
    *,
    evaluation_set: EvaluationSetV1,
    manifest: KnowledgeResourceManifestV1,
    runner: RetrievalRunner,
    pipeline: str,
) -> PipelineEvaluation:
    """以固定 case 顺序计算 macro Recall@K、MRR 与 citation coverage。"""
    recall_total = 0.0
    reciprocal_rank_total = 0.0
    citation_total = 0.0
    for case in evaluation_set.cases:
        hits = runner.retrieve(
            resource_id=manifest.resource_id,
            query=case.query,
            pipeline=pipeline,
            top_k=evaluation_set.recall_at_k,
            trace_id=f"evaluation-{case.case_id}-{pipeline}",
        )
        recall_total += _recall_at_k(hits, case.relevant_chunk_ids)
        reciprocal_rank_total += _reciprocal_rank(hits, case.relevant_chunk_ids)
        citation_total += _citation_coverage(hits, case.required_citation_ids)
    case_count = len(evaluation_set.cases)
    recall = recall_total / case_count
    mrr = reciprocal_rank_total / case_count
    citation_coverage = citation_total / case_count
    passed = (
        recall >= evaluation_set.min_recall
        and mrr >= evaluation_set.min_mrr
        and citation_coverage >= evaluation_set.min_citation_coverage
    )
    return PipelineEvaluation(
        recall=recall,
        mrr=mrr,
        citation_coverage=citation_coverage,
        cases=case_count,
        passed=passed,
    )


def _recall_at_k(hits: Sequence[RetrievedEvaluationHitV1], relevant: Sequence[str]) -> float:
    """返回单 case 的相关 chunk 覆盖率，而非只看首个命中。"""
    expected = set(relevant)
    return len(expected.intersection(hit.chunk_id for hit in hits)) / len(expected)


def _reciprocal_rank(hits: Sequence[RetrievedEvaluationHitV1], relevant: Sequence[str]) -> float:
    """返回第一个相关 chunk 的倒数排名；未命中则为零。"""
    expected = set(relevant)
    for rank, hit in enumerate(hits, start=1):
        if hit.chunk_id in expected:
            return 1.0 / rank
    return 0.0


def _citation_coverage(hits: Sequence[RetrievedEvaluationHitV1], required: Sequence[str]) -> float:
    """验证命中能携带每个要求 citation，而非仅返回内容片段。"""
    expected = set(required)
    if not expected:
        return 1.0
    actual = {hit.citation_id for hit in hits if hit.citation_id is not None}
    return len(expected.intersection(actual)) / len(expected)
