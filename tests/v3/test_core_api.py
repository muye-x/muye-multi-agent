"""阶段 1 Core API 的可观察行为测试。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import httpx
import pytest

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
