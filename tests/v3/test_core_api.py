"""阶段 1 Core API 的可观察行为测试。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from muye_core.api import create_app
from muye_core.service import InMemoryCoreStore
from muye_core.storage import ArtifactStore, AssetValidationError


@pytest.fixture
def client(tmp_path: Path):
    """每个测试隔离 API 状态与 Artifact 根目录。"""

    application = create_app(store=InMemoryCoreStore(), artifact_root=tmp_path / "artifacts")
    transport = httpx.ASGITransport(app=application)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _admin(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/api/v3/bootstrap/admin", json={"username": "admin", "password": "correct-horse-battery"})
    assert response.status_code == 201
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
