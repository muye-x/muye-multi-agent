"""阶段 2 Profile/评测 Proposal 异步工作流测试。"""

from __future__ import annotations

from hashlib import sha256
import io
from pathlib import Path

import pytest

from muye_core.proposals import LLMProfileProposalBackend, ProfileProposalInput, ProfileProposalV1
from muye_core.service import DomainError, InMemoryCoreStore
from muye_core.storage import ArtifactStore
from muye_core.worker import ProfileProposalJobWorker
from tools.knowledge_pipeline.checksums import canonical_checksum


def _draft_with_source(store: InMemoryCoreStore, artifacts: ArtifactStore):
    admin = store.bootstrap_admin("admin", "correct-horse-battery")
    content = b"# Leave\n\nAnnual leave requires manager approval.\n"
    stored = artifacts.store(io.BytesIO(content), filename="handbook.md")
    agent, _draft = store.create_agent(
        admin,
        slug="proposal-help",
        display_name="Employee assistant",
        description="Answer policy questions",
        config={"objective": "Answer only from policy", "prohibited_actions": ["Do not guess"]},
    )
    store.attach_asset(admin, agent.agent_id, sha256=stored.sha256, size_bytes=stored.size_bytes, media_type="text/markdown", storage_key=stored.storage_key, display_name="handbook.md")
    return admin, agent


class _ProposalClient:
    def propose(self, *, project, chunks):
        assert project.objective == "Answer only from policy"
        return {
            "profile": {
                "schema_version": "muye.ai/agent-profile-proposal/v1",
                "display_name": "Employee assistant",
                "description": "Answers approved policy questions",
                "supported_intents": ["leave policy"],
                "instructions": "Only answer from approved sources.",
                "do_not_use_when": ["No supporting source"],
                "examples": ["How do I request leave?"],
            },
            "cases": [{"case_id": "leave", "query": "How do I request leave?", "relevant_chunk_ids": [chunks[0]["chunk_id"]]}],
        }


def test_llm_proposal_maps_chunk_evidence_to_draft_asset(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = InMemoryCoreStore()
    admin, agent = _draft_with_source(store, artifacts)
    job = store.create_profile_proposal_job(admin, agent.agent_id, idempotency_key="proposal-1")
    proposal_input = store.profile_proposal_input(job.job_id)
    backend = LLMProfileProposalBackend(artifact_store=artifacts, llm_base_url="http://llm.test", evaluation_case_count=1)
    backend._client = _ProposalClient()

    proposal = backend.propose(proposal_input)

    assert proposal.evaluation_cases[0].expected_source_asset_ids == [proposal_input.assets[0].asset_id]
    assert proposal.proposal_checksum == canonical_checksum(proposal.model_dump(mode="json", exclude={"proposal_checksum"}))


def test_proposal_worker_publishes_only_for_unchanged_draft(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = InMemoryCoreStore()
    admin, agent = _draft_with_source(store, artifacts)
    job = store.create_profile_proposal_job(admin, agent.agent_id, idempotency_key="proposal-2")
    proposal_input = store.profile_proposal_input(job.job_id)
    raw = {
        "schema_version": "muye.ai/profile-proposal/v1",
        "agent_id": agent.agent_id,
        "draft_version": proposal_input.draft.version,
        "profile": {
            "schema_version": "muye.ai/agent-profile-proposal/v1",
            "display_name": "Employee assistant",
            "description": "Answers policy questions",
            "supported_intents": ["leave policy"],
            "instructions": "Use sources.",
            "do_not_use_when": [],
            "examples": [],
        },
        "evaluation_cases": [{"case_id": "leave", "question": "Leave?", "expected_source_asset_ids": [proposal_input.assets[0].asset_id]}],
    }
    proposal = ProfileProposalV1.model_validate({**raw, "proposal_checksum": canonical_checksum(raw)})

    class Backend:
        def propose(self, value: ProfileProposalInput) -> ProfileProposalV1:
            assert value.job_id == job.job_id
            return proposal

    ProfileProposalJobWorker(store=store, backend=Backend(), worker_id="proposal-worker").run_once()

    assert store.job_detail(job.job_id).status == "SUCCEEDED"
    assert store.profile_proposal(job.job_id) == proposal


def test_proposal_worker_rejects_changed_draft(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = InMemoryCoreStore()
    admin, agent = _draft_with_source(store, artifacts)
    original = store.create_profile_proposal_job(admin, agent.agent_id, idempotency_key="proposal-drift")
    _agent, draft = store.agent_detail(agent.agent_id)
    assert draft is not None
    store.patch_draft(admin, agent.agent_id, draft.version, {**draft.config, "objective": "Updated objective"})

    class Backend:
        def propose(self, _value: ProfileProposalInput) -> ProfileProposalV1:
            raise AssertionError("漂移的 Draft 不应调用 Proposal Backend")

    ProfileProposalJobWorker(store=store, backend=Backend(), worker_id="proposal-worker").run_once()

    failed = store.job_detail(original.job_id)
    assert failed.status == "FAILED"
    assert failed.error_code == "DRAFT_CHANGED"
    with pytest.raises(DomainError, match="Draft 已变化"):
        store.profile_proposal_input(original.job_id)
    with pytest.raises(DomainError, match="不可重试"):
        store.retry_job(admin, original.job_id, idempotency_key="proposal-retry")


def test_proposal_worker_skips_llm_when_cancelled_before_claim_processing(tmp_path: Path) -> None:
    """领取前已取消的 Proposal Job 不得调用模型。"""

    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = InMemoryCoreStore()
    admin, agent = _draft_with_source(store, artifacts)
    job = store.create_profile_proposal_job(admin, agent.agent_id, idempotency_key="proposal-cancelled-before-run")
    store.request_job_cancel(admin, job.job_id)

    class Backend:
        def propose(self, _value: ProfileProposalInput) -> ProfileProposalV1:
            raise AssertionError("已取消 Proposal Job 不应调用模型")

    ProfileProposalJobWorker(store=store, backend=Backend(), worker_id="proposal-worker").run_once()

    assert store.job_detail(job.job_id).status == "CANCELLED"
    assert store.profile_proposal(job.job_id) is None


def test_proposal_retry_idempotency_is_scoped_to_agent(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = InMemoryCoreStore()
    admin, first_agent = _draft_with_source(store, artifacts)
    second_content = b"# Travel\n\nTravel requires approval.\n"
    second_stored = artifacts.store(io.BytesIO(second_content), filename="travel.md")
    second_agent, _draft = store.create_agent(
        admin,
        slug="travel-help",
        display_name="Travel assistant",
        description="Travel policy",
        config={"objective": "Answer travel policy"},
    )
    store.attach_asset(
        admin,
        second_agent.agent_id,
        sha256=second_stored.sha256,
        size_bytes=second_stored.size_bytes,
        media_type="text/markdown",
        storage_key=second_stored.storage_key,
        display_name="travel.md",
    )
    originals = [
        store.create_profile_proposal_job(admin, first_agent.agent_id, idempotency_key="first-proposal"),
        store.create_profile_proposal_job(admin, second_agent.agent_id, idempotency_key="second-proposal"),
    ]
    for index in range(len(originals)):
        claimed = store.claim_job(worker_id=f"worker-{index}", job_types=frozenset({"PROFILE_PROPOSAL"}))
        assert claimed is not None
        store.complete_job(worker_id=f"worker-{index}", job_id=claimed.job_id, status="FAILED", error_code="DEPENDENCY_UNAVAILABLE")

    first_retry = store.retry_job(admin, originals[0].job_id, idempotency_key="shared-retry-key")
    second_retry = store.retry_job(admin, originals[1].job_id, idempotency_key="shared-retry-key")

    assert first_retry.job_id != second_retry.job_id
    assert first_retry.attempt == second_retry.attempt == 2
    assert store.profile_proposal_input(first_retry.job_id).agent.agent_id == first_agent.agent_id
    assert store.profile_proposal_input(second_retry.job_id).agent.agent_id == second_agent.agent_id
