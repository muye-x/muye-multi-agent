"""阶段 2 Knowledge Job Worker 状态收敛测试。"""

from __future__ import annotations

from hashlib import sha256

from contracts.v3 import RuntimeResourceBindingV1
from muye_core.knowledge import EvaluationOutput, KnowledgeBuildOutput
from muye_core.service import InMemoryCoreStore
from muye_core.worker import KnowledgeJobWorker


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


def test_worker_marks_ready_only_after_evaluation_passes() -> None:
    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    job = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-1")
    KnowledgeJobWorker(store=store, backend=_Backend(passed=True), worker_id="worker_1").run_once()
    assert store.job_detail(job.job_id).status == "SUCCEEDED"
    assert store.revision_detail(revision.revision_id).status == "READY"


def test_worker_blocks_ready_when_evaluation_fails() -> None:
    store = InMemoryCoreStore()
    admin, revision = _approved_revision(store)
    job = store.create_job(admin, revision_id=revision.revision_id, job_type="BUILD", idempotency_key="build-2")
    KnowledgeJobWorker(store=store, backend=_Backend(passed=False), worker_id="worker_1").run_once()
    assert store.job_detail(job.job_id).status == "FAILED"
    assert store.revision_detail(revision.revision_id).status == "APPROVED"
