"""阶段 2 Knowledge Job Worker 状态收敛测试。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from contracts.v3 import RuntimeResourceBindingV1
from muye_core.knowledge import EvaluationOutput, KnowledgeBuildOutput
from muye_core.service import InMemoryCoreStore
from muye_core.storage import ArtifactStore
from muye_core.worker import KnowledgeJobWorker
from muye_core.service import DomainError


class _Backend:
    def __init__(self, *, passed: bool) -> None:
        self._passed = passed

    def build(self, _spec):
        return KnowledgeBuildOutput("build_hotel_revision_2", [RuntimeResourceBindingV1(resource_id="kb.hotel_employee", collection_name="kb_hotel_employee_revision_2", collection_checksum="5" * 64, embedding_alias="embedding_default")])

    def evaluate(self, _spec, _build):
        return EvaluationOutput(self._passed, 1.0 if self._passed else 0.0, "evaluations/revision/report.json")


def _approved_revision(store: InMemoryCoreStore):
    admin = store.bootstrap_admin("admin", "correct-horse-battery")
    source = b"# handbook\nannual leave requires approval"
    asset_id = f"asset_{sha256(source).hexdigest()[:16]}"
    config = {
        "display_name": "员工助手", "objective": "只根据资料回答。", "instructions": "只使用资料。",
        "prohibited_actions": ["不得猜测"], "examples": ["年假如何申请？"],
        "model": {"chat_alias": "chat_default", "embedding_alias": "embedding_default", "temperature": 0.2},
        "retrieval": {"pipeline": "hybrid", "top_k": 8, "rerank_alias": None, "minimum_score": 0.0},
        "budgets": {"output_tokens": 1024, "tool_calls": 2, "timeout_seconds": 30},
        "evaluation": {"cases": [{"case_id": "leave", "question": "年假如何申请？", "expected_source_asset_ids": [asset_id]}], "minimum_pass_rate": 0.8, "citation_required": True},
    }
    agent, _ = store.create_agent(admin, slug="worker-help", display_name="员工助手", description="制度问答", config=config)
    store.attach_asset(admin, agent.agent_id, sha256=sha256(source).hexdigest(), size_bytes=len(source), media_type="text/markdown", storage_key="assets/test", display_name="handbook.md")
    revision = store.freeze_revision(admin, agent.agent_id, 2)
    return admin, store.approve_revision(admin, revision.revision_id, revision.checksum)


def test_worker_marks_ready_only_after_evaluation_passes(tmp_path: Path) -> None:
    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    job = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-1")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    KnowledgeJobWorker(store=store, backend=_Backend(passed=True), artifact_store=artifacts, worker_id="worker_1").run_once()
    assert store.job_detail(job.job_id).status == "SUCCEEDED"
    assert store.revision_detail(revision.revision_id).status == "READY"
    storage_key = store._ready_revisions[revision.revision_id]["storage_key"]
    assert artifacts.read_bytes(f"{storage_key}/manifest.json")


def test_worker_blocks_ready_when_evaluation_fails(tmp_path: Path) -> None:
    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    job = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-2")
    KnowledgeJobWorker(store=store, backend=_Backend(passed=False), artifact_store=ArtifactStore(tmp_path / "artifacts"), worker_id="worker_1").run_once()
    assert store.job_detail(job.job_id).status == "FAILED"
    assert store.revision_detail(revision.revision_id).status == "APPROVED"


def test_worker_consumes_evaluation_jobs_without_a_separate_stuck_queue(tmp_path: Path) -> None:
    """EVALUATE 使用同一幂等构建链路，不能永久停留在 PENDING。"""

    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    evaluation = store.create_job(admin, revision_id=revision.revision_id, job_type="EVALUATE", idempotency_key="evaluate-1")
    KnowledgeJobWorker(store=store, backend=_Backend(passed=True), artifact_store=ArtifactStore(tmp_path / "artifacts"), worker_id="worker_1").run_once()
    assert store.job_detail(evaluation.job_id).status == "SUCCEEDED"
    assert store.revision_detail(revision.revision_id).status == "READY"


def test_worker_cancellation_does_not_publish_ready_revision(tmp_path: Path) -> None:
    """Artifact 写入与数据库发布之间的取消不得产生 READY Revision。"""

    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    job = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-4")

    class CancellingArtifactStore(ArtifactStore):
        def store_bundle(self, **kwargs):
            store.request_job_cancel(admin, job.job_id)
            return super().store_bundle(**kwargs)

    KnowledgeJobWorker(store=store, backend=_Backend(passed=True), artifact_store=CancellingArtifactStore(tmp_path / "artifacts"), worker_id="worker_1").run_once()
    assert store.job_detail(job.job_id).status == "CANCELLED"
    assert store.revision_detail(revision.revision_id).status == "APPROVED"


def test_worker_skips_build_when_cancelled_before_claim_processing(tmp_path: Path) -> None:
    """领取前已取消的 Job 不得触发解析、Embedding 或 Milvus 副作用。"""

    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    job = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-cancelled-before-run")
    store.request_job_cancel(admin, job.job_id)

    class Backend:
        def build(self, _spec):
            raise AssertionError("已取消 Job 不应执行知识构建")

        def evaluate(self, _spec, _build):
            raise AssertionError("已取消 Job 不应执行评测")

    KnowledgeJobWorker(
        store=store,
        backend=Backend(),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        worker_id="worker_1",
    ).run_once()

    assert store.job_detail(job.job_id).status == "CANCELLED"
    assert store.revision_detail(revision.revision_id).status == "APPROVED"


def test_failed_dependency_job_can_retry_as_new_attempt(tmp_path: Path) -> None:
    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    original = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-retry-source")
    claimed = store.claim_job(worker_id="worker_1")
    assert claimed is not None
    store.complete_job(worker_id="worker_1", job_id=original.job_id, status="FAILED", error_code="DEPENDENCY_UNAVAILABLE")

    retried = store.retry_job(admin, original.job_id, idempotency_key="build-retry-target")

    assert retried.job_id != original.job_id
    assert retried.attempt == 2
    assert retried.status == "PENDING"
    assert store.retry_job(admin, original.job_id, idempotency_key="build-retry-target") == retried


def test_non_recoverable_job_and_expired_lease_are_rejected() -> None:
    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    job = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-terminal")
    claimed = store.claim_job(worker_id="worker_1")
    assert claimed is not None
    store.complete_job(worker_id="worker_1", job_id=job.job_id, status="FAILED", error_code="EVALUATION_FAILED")
    with pytest.raises(DomainError, match="不可重试"):
        store.retry_job(admin, job.job_id, idempotency_key="retry-terminal")

    second = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-expired")
    leased = store.claim_job(worker_id="worker_1")
    assert leased is not None and leased.job_id == second.job_id
    store._jobs[second.job_id] = replace(leased, lease_until=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(DomainError, match="有效 Job lease"):
        store.complete_job(worker_id="worker_1", job_id=second.job_id, status="FAILED", error_code="WORKER_INTERRUPTED")
