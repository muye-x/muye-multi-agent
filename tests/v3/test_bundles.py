"""阶段 2 Runtime Bundle 的评测门禁与篡改检测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.v3 import AgentRevisionSpecV1, RuntimeResourceBindingV1
from muye_core.bundles import build_bundle, verify_bundle
from muye_core.service import DomainError


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
