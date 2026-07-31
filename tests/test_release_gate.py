"""发布冻结证据的离线回归测试。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from tools.agent_generator.checksums import canonical_checksum, read_source_tree
from tools.release_gate import verify_evidence


ROOT = Path(__file__).resolve().parents[1]


def _evidence() -> dict[str, object]:
    template_root = ROOT / "templates" / "agents" / "react-knowledge" / "v1"
    schema = ROOT / "contracts" / "schemas" / "agent-generation-spec-v1.schema.json"
    return {
        "schema_version": "muye.ai/release-evidence/v1",
        "release_version": "2.0.0-alpha.1",
        "stage": "alpha",
        "sdk_version": "2.0.0",
        "template": {"id": "react-knowledge", "version": "1.0.0", "source_tree_checksum": canonical_checksum(read_source_tree(template_root))},
        "schema_checksums": {schema.name: sha256(schema.read_bytes()).hexdigest()},
        "images": {"agent-main": "registry.example/muye/agent-main@sha256:" + "a" * 64},
        "catalog_checksum": "b" * 64,
        "verification_refs": ["ci://run/123"],
    }


def test_release_evidence_requires_current_frozen_inputs(tmp_path: Path) -> None:
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(_evidence(), sort_keys=False), encoding="utf-8")
    verify_evidence(path, workspace_root=ROOT)


def test_release_evidence_rejects_non_manifest_image_digest(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["images"] = {"agent-main": "sha256:" + "a" * 64}
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(evidence, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="registry repository"):
        verify_evidence(path, workspace_root=ROOT)
