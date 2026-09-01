"""阶段 2 Runtime Bundle 的评测门禁与篡改检测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.v3 import AgentRevisionSpecV1, RuntimeResourceBindingV1
from muye_core.bundles import build_bundle, bundle_artifact_members, verify_bundle
from muye_core.service import DomainError
from muye_core.storage import ArtifactStore


def _spec() -> AgentRevisionSpecV1:
    fixture = Path("contracts/fixtures/agent-revision-v1.valid.json")
    return AgentRevisionSpecV1.model_validate(json.loads(fixture.read_text(encoding="utf-8")))


def test_bundle_requires_passing_evaluation_and_detects_member_tampering() -> None:
    """未通过评测或成员被篡改的 Bundle 都不能交给 Runtime。"""

    resource = RuntimeResourceBindingV1(
        resource_id="kb.hotel_employee",
        collection_name="kb_hotel_employee_revision_2",
        collection_checksum="5" * 64,
        embedding_alias="embedding_default",
    )
    with pytest.raises(DomainError, match="评测未通过"):
        build_bundle(spec=_spec(), build_id="build_hotel_revision_2", resources=[resource], evaluation_summary={"passed": False})

    manifest, members = build_bundle(
        spec=_spec(),
        build_id="build_hotel_revision_2",
        resources=[resource],
        evaluation_summary={"passed": True, "pass_rate": 1.0},
    )
    verify_bundle(manifest, members)
    with pytest.raises(DomainError, match="checksum"):
        verify_bundle(manifest, {**members, "revision.json": b"{}\n"})


def test_bundle_artifact_is_atomically_reusable(tmp_path: Path) -> None:
    """同一 Bundle checksum 只能重放到同一个受控 Artifact 目录。"""

    resource = RuntimeResourceBindingV1(resource_id="kb.hotel_employee", collection_name="kb_hotel_employee_revision_2", collection_checksum="5" * 64, embedding_alias="embedding_default")
    manifest, members = build_bundle(spec=_spec(), build_id="build_hotel_revision_2", resources=[resource], evaluation_summary={"passed": True, "pass_rate": 1.0})
    store = ArtifactStore(tmp_path / "artifacts")
    artifact_members = bundle_artifact_members(manifest, members)
    key = store.store_bundle(agent_id=manifest.agent_id, revision_id=manifest.revision_id, bundle_checksum=manifest.bundle_checksum, members=artifact_members)
    assert store.read_bytes(f"{key}/manifest.json") == artifact_members["manifest.json"]
    assert store.store_bundle(agent_id=manifest.agent_id, revision_id=manifest.revision_id, bundle_checksum=manifest.bundle_checksum, members=artifact_members) == key
