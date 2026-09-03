"""阶段 1 Core API 的可观察行为测试。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

import httpx
import pytest

import muye_core.service as service_module
from contracts.models import AGENT_ID_PATTERN
from muye_core.api import create_app
from muye_core.service import InMemoryCoreStore
from muye_core.storage import ArtifactStore, AssetValidationError


@pytest.fixture
def client(tmp_path: Path):
    """每个测试隔离 API 状态与 Artifact 根目录。"""

    store = InMemoryCoreStore()
    application = create_app(store=store, artifact_root=tmp_path / "artifacts")
    transport = httpx.ASGITransport(app=application)
    result = httpx.AsyncClient(transport=transport, base_url="http://test")
    result._core_store = store
    return result


async def _admin(client: httpx.AsyncClient) -> dict[str, str]:
    client._core_store.bootstrap_admin("admin", "correct-horse-battery")
    login = await client.post("/api/v3/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.anyio
async def test_agent_creation_is_idempotent_and_slug_is_a_tombstone(client: httpx.AsyncClient) -> None:
    headers = await _admin(client)
    request = {"slug": "hotel-help", "display_name": "酒店助手", "description": "回答制度问题", "config": {"objective": "help"}}
    headers["Idempotency-Key"] = "create-agent-0001"

    first = await client.post("/api/v3/agents", json=request, headers=headers)
    repeated = await client.post("/api/v3/agents", json=request, headers=headers)

    assert first.status_code == repeated.status_code == 201
    assert first.json() == repeated.json()
    agent_id = first.json()["agent_id"]
    headers["Idempotency-Key"] = "archive-agent-0001"
    assert (await client.post(f"/api/v3/agents/{agent_id}/archive", headers=headers)).status_code == 200
    headers["Idempotency-Key"] = "create-agent-0002"
    assert (await client.post("/api/v3/agents", json=request, headers=headers)).status_code == 409


@pytest.mark.anyio
async def test_draft_version_conflict_and_suspension_block_updates(client: httpx.AsyncClient) -> None:
    headers = await _admin(client)
    headers["Idempotency-Key"] = "create-agent-0003"
    created = await client.post("/api/v3/agents", json={"slug": "policy-help", "display_name": "政策助手", "description": "回答政策", "config": {}}, headers=headers)
    agent_id = created.json()["agent_id"]
    headers["Idempotency-Key"] = "patch-draft-0001"
    assert (await client.patch(f"/api/v3/agents/{agent_id}/draft", json={"version": 1, "config": {"a": 1}}, headers=headers)).status_code == 200
    headers["Idempotency-Key"] = "patch-draft-0002"
    assert (await client.patch(f"/api/v3/agents/{agent_id}/draft", json={"version": 1, "config": {"a": 2}}, headers=headers)).status_code == 409
    headers["Idempotency-Key"] = "stop-agent-00001"
    assert (await client.post(f"/api/v3/agents/{agent_id}/stop", headers=headers)).status_code == 200
    headers["Idempotency-Key"] = "patch-draft-0003"
    assert (await client.patch(f"/api/v3/agents/{agent_id}/draft", json={"version": 2, "config": {}}, headers=headers)).status_code == 409


def test_artifact_store_reuses_content_and_rejects_unsafe_names(tmp_path: Path) -> None:
    import io

    store = ArtifactStore(tmp_path / "artifacts", max_bytes=4)
    first = store.store(io.BytesIO(b"test"), filename="handbook.md")
    second = store.store(io.BytesIO(b"test"), filename="copy.md")

    assert first.reused is False
    assert second.reused is True
    with pytest.raises(AssetValidationError, match="文件名非法"):
        store.store(io.BytesIO(b"x"), filename="../escape.md")
    with pytest.raises(AssetValidationError, match="大小上限"):
        store.store(io.BytesIO(b"large"), filename="large.md")
    root_target = tmp_path / "target"
    root_target.mkdir()
    linked_root = tmp_path / "linked-artifacts"
    linked_root.symlink_to(root_target, target_is_directory=True)
    with pytest.raises(AssetValidationError, match="普通目录"):
        ArtifactStore(linked_root).readiness()


def test_agent_id_generation_never_uses_urlsafe_prefix_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL-safe token 可以以 '-' 或 '_' 开头，但 Agent 契约不允许该形式。"""

    monkeypatch.setattr(service_module, "token_urlsafe", lambda _size: "_invalid-prefix")
    store = InMemoryCoreStore()
    admin = store.bootstrap_admin("admin", "correct-horse-battery")
    agent, _ = store.create_agent(admin, slug="stable-agent-id", display_name="助手", description="测试", config={})
    assert re.fullmatch(AGENT_ID_PATTERN, agent.agent_id)


@pytest.mark.anyio
async def test_validation_error_is_normalized_and_upload_binds_draft(client: httpx.AsyncClient) -> None:
    invalid = await client.post("/api/v3/auth/login", json={})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"
    assert invalid.json()["request_id"].startswith("request_")
    headers = await _admin(client)
    headers["Idempotency-Key"] = "create-agent-0004"
    created = await client.post("/api/v3/agents", json={"slug": "upload-help", "display_name": "上传助手", "description": "回答资料", "config": {}}, headers=headers)
    headers["Idempotency-Key"] = "upload-source-0001"
    uploaded = await client.post(f"/api/v3/agents/{created.json()['agent_id']}/sources", headers=headers, files={"file": ("handbook.md", b"# handbook", "text/markdown")})
    assert uploaded.status_code == 201
    assert uploaded.json()["asset_id"].startswith("asset_")
    assert client._core_store._draft_sources[created.json()["agent_id"]][0]["asset_id"] == uploaded.json()["asset_id"]


@pytest.mark.anyio
async def test_revision_freeze_snapshots_draft_and_requires_matching_checksum(client: httpx.AsyncClient) -> None:
    """冻结版本不能跟随 Draft 修改，审批必须确认同一个逻辑输入 checksum。"""

    headers = await _admin(client)
    source = b"# handbook\nannual leave requires approval"
    asset_id = f"asset_{sha256(source).hexdigest()[:16]}"
    revision_config = {
        "display_name": "员工助手",
        "objective": "只根据已批准资料回答员工制度问题。",
        "instructions": "只使用资料，无法确认时明确说明。",
        "prohibited_actions": ["不得伪造资料结论"],
        "examples": ["年假如何申请？"],
        "model": {"chat_alias": "chat_default", "embedding_alias": "embedding_default", "temperature": 0.2},
        "retrieval": {"pipeline": "hybrid", "top_k": 8, "rerank_alias": None, "minimum_score": 0.0},
        "budgets": {"output_tokens": 1024, "tool_calls": 2, "timeout_seconds": 30},
        "evaluation": {
            "cases": [{"case_id": "annual_leave", "question": "年假如何申请？", "expected_source_asset_ids": [asset_id]}],
            "minimum_pass_rate": 0.8,
            "citation_required": True,
        },
    }
    headers["Idempotency-Key"] = "create-agent-revision-01"
    created = await client.post(
        "/api/v3/agents",
        json={"slug": "revision-help", "display_name": "员工助手", "description": "制度问答", "config": revision_config},
        headers=headers,
    )
    agent_id = created.json()["agent_id"]
    headers["Idempotency-Key"] = "upload-revision-source-01"
    uploaded = await client.post(
        f"/api/v3/agents/{agent_id}/sources",
        headers=headers,
        files={"file": ("handbook.md", source, "text/markdown")},
    )
    assert uploaded.status_code == 201

    headers["Idempotency-Key"] = "freeze-revision-0001"
    frozen = await client.post(
        f"/api/v3/agents/{agent_id}/revisions",
        json={"draft_version": 2},
        headers=headers,
    )
    assert frozen.status_code == 201
    revision = frozen.json()
    assert revision["status"] == "REVIEW_REQUIRED"
    assert revision["spec"]["source_assets"] == [{"asset_id": asset_id, "sha256": sha256(source).hexdigest(), "display_name": "handbook.md"}]

    headers["Idempotency-Key"] = "patch-draft-revision-01"
    assert (await client.patch(f"/api/v3/agents/{agent_id}/draft", json={"version": 2, "config": {**revision_config, "objective": "已修改"}}, headers=headers)).status_code == 200
    detail = await client.get(f"/api/v3/revisions/{revision['revision_id']}", headers=headers)
    assert detail.json()["spec"]["objective"] == revision_config["objective"]

    headers["Idempotency-Key"] = "approve-revision-0001"
    rejected = await client.post(
        f"/api/v3/revisions/{revision['revision_id']}/approve",
        json={"checksum": "0" * 64},
        headers=headers,
    )
    assert rejected.status_code == 409
    approved = await client.post(
        f"/api/v3/revisions/{revision['revision_id']}/approve",
        json={"checksum": revision["checksum"]},
        headers={**headers, "Idempotency-Key": "approve-revision-0002"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    headers["Idempotency-Key"] = "build-revision-0001"
    job = await client.post(
        f"/api/v3/revisions/{revision['revision_id']}/jobs",
        json={"job_type": "BUILD"},
        headers=headers,
    )
    assert job.status_code == 201
    repeated_job = await client.post(
        f"/api/v3/revisions/{revision['revision_id']}/jobs",
        json={"job_type": "BUILD"},
        headers=headers,
    )
    assert repeated_job.json() == job.json()
    claimed = client._core_store.claim_job(worker_id="worker_1")
    assert claimed is not None and claimed.job_id == job.json()["job_id"]

    headers["Idempotency-Key"] = "cancel-revision-job-01"
    cancelled = await client.post(f"/api/v3/jobs/{claimed.job_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCEL_REQUESTED"
    assert client._core_store.complete_job(worker_id="worker_1", job_id=claimed.job_id, status="CANCELLED").status == "CANCELLED"
    resumed_events = await client.get(f"/api/v3/jobs/{claimed.job_id}/events", headers={**headers, "Last-Event-ID": "0"})
    assert resumed_events.status_code == 200
    assert "event: cancelled" in resumed_events.text


@pytest.mark.anyio
async def test_draft_impact_is_computed_from_server_facts(client: httpx.AsyncClient) -> None:
    headers = await _admin(client)
    config = {
        "display_name": "Impact assistant", "objective": "Answer", "instructions": "Use sources",
        "prohibited_actions": ["Do not guess"], "examples": ["Question"],
        "model": {"chat_alias": "chat_default", "embedding_alias": "embedding_default", "temperature": 0.0},
        "retrieval": {"pipeline": "hybrid", "top_k": 8, "rerank_alias": None, "minimum_score": 0.0},
        "budgets": {"output_tokens": 512, "tool_calls": 1, "timeout_seconds": 30},
        "evaluation": {"cases": [], "minimum_pass_rate": 1.0, "citation_required": True},
    }
    headers["Idempotency-Key"] = "impact-create-01"
    created = await client.post("/api/v3/agents", json={"slug": "impact-help", "display_name": "Impact", "description": "Impact", "config": config}, headers=headers)
    agent_id = created.json()["agent_id"]
    headers["Idempotency-Key"] = "impact-upload-01"
    first = await client.post(f"/api/v3/agents/{agent_id}/sources", headers=headers, files={"file": ("first.md", b"first", "text/markdown")})
    asset_id = first.json()["asset_id"]
    config["evaluation"]["cases"] = [{"case_id": "case", "question": "Question", "expected_source_asset_ids": [asset_id]}]
    headers["Idempotency-Key"] = "impact-patch-01"
    await client.patch(f"/api/v3/agents/{agent_id}/draft", json={"version": 2, "config": config}, headers=headers)
    headers["Idempotency-Key"] = "impact-freeze-01"
    frozen = await client.post(f"/api/v3/agents/{agent_id}/revisions", json={"draft_version": 3}, headers=headers)
    assert frozen.status_code == 201
    impact = await client.get(f"/api/v3/agents/{agent_id}/draft/impact", headers=headers)
    assert impact.json()["mode"] == "REUSE"
    assert impact.json()["evaluation_required"] is False

    headers["Idempotency-Key"] = "impact-upload-02"
    await client.post(f"/api/v3/agents/{agent_id}/sources", headers=headers, files={"file": ("second.md", b"second", "text/markdown")})
    impact = await client.get(f"/api/v3/agents/{agent_id}/draft/impact", headers=headers)
    assert impact.json()["mode"] == "INCREMENTAL"
    assert impact.json()["reusable_asset_ids"] == [asset_id]

    headers["Idempotency-Key"] = "impact-remove-01"
    removed = await client.delete(f"/api/v3/agents/{agent_id}/sources/{asset_id}", headers=headers)
    assert removed.status_code == 204
    impact = await client.get(f"/api/v3/agents/{agent_id}/draft/impact", headers=headers)
    assert impact.json()["mode"] == "FULL_REBUILD"
    assert impact.json()["removed_asset_ids"] == [asset_id]


@pytest.mark.anyio
async def test_standard_build_and_retry_routes_preserve_attempt_history(client: httpx.AsyncClient) -> None:
    headers = await _admin(client)
    store = client._core_store
    source = b"source"
    asset_id = f"asset_{sha256(source).hexdigest()[:16]}"
    config = {
        "display_name": "Build assistant", "objective": "Answer", "instructions": "Use sources",
        "prohibited_actions": ["Do not guess"], "examples": ["Question"],
        "model": {"chat_alias": "chat_default", "embedding_alias": "embedding_default", "temperature": 0.0},
        "retrieval": {"pipeline": "hybrid", "top_k": 8, "rerank_alias": None, "minimum_score": 0.0},
        "budgets": {"output_tokens": 512, "tool_calls": 1, "timeout_seconds": 30},
        "evaluation": {"cases": [{"case_id": "case", "question": "Question", "expected_source_asset_ids": [asset_id]}], "minimum_pass_rate": 1.0, "citation_required": True},
    }
    agent, _draft = store.create_agent(store.principal(headers["Authorization"].removeprefix("Bearer ")), slug="build-help", display_name="Build", description="Build", config=config)
    store.attach_asset(store.principal(headers["Authorization"].removeprefix("Bearer ")), agent.agent_id, sha256=sha256(source).hexdigest(), size_bytes=len(source), media_type="text/plain", storage_key="assets/test", display_name="source.txt")
    revision = store.freeze_revision(store.principal(headers["Authorization"].removeprefix("Bearer ")), agent.agent_id, 2)
    store.approve_revision(store.principal(headers["Authorization"].removeprefix("Bearer ")), revision.revision_id, revision.checksum)
    headers["Idempotency-Key"] = "standard-build-01"
    response = await client.post(f"/api/v3/revisions/{revision.revision_id}/build", headers=headers)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    store.claim_job(worker_id="worker_1")
    store.complete_job(worker_id="worker_1", job_id=job_id, status="FAILED", error_code="DEPENDENCY_UNAVAILABLE")
    headers["Idempotency-Key"] = "standard-retry-01"
    retried = await client.post(f"/api/v3/jobs/{job_id}/retry", headers=headers)
    assert retried.status_code == 201
    assert retried.json()["attempt"] == 2


@pytest.mark.anyio
async def test_profile_proposal_api_creates_pending_job(client: httpx.AsyncClient) -> None:
    headers = await _admin(client)
    headers["Idempotency-Key"] = "create-proposal-agent"
    created = await client.post(
        "/api/v3/agents",
        json={
            "slug": "proposal-api",
            "display_name": "Proposal assistant",
            "description": "Answers policy",
            "config": {"objective": "Answer policy"},
        },
        headers=headers,
    )
    agent_id = created.json()["agent_id"]
    headers["Idempotency-Key"] = "proposal-api-source"
    uploaded = await client.post(
        f"/api/v3/agents/{agent_id}/sources",
        headers=headers,
        files={"file": ("policy.md", b"# Policy\n\nUse approved evidence.", "text/markdown")},
    )
    assert uploaded.status_code == 201

    headers["Idempotency-Key"] = "proposal-api-job"
    submitted = await client.post(f"/api/v3/agents/{agent_id}/profile-proposals", headers=headers)
    assert submitted.status_code == 202
    assert submitted.json()["job_type"] == "PROFILE_PROPOSAL"
    assert submitted.json()["status"] == "PENDING"

    result = await client.get(f"/api/v3/profile-proposals/{submitted.json()['job_id']}", headers=headers)
    assert result.status_code == 200
    assert result.json() == {"job_id": submitted.json()["job_id"], "status": "PENDING", "proposal": None}
