"""阶段 2 生产 Knowledge Backend 的离线边界测试。"""

from __future__ import annotations

from hashlib import sha256
import io
import json
from pathlib import Path

import httpx
import pytest

import muye_core.knowledge_backend as backend_module
from muye_core.knowledge_backend import CoreKnowledgeBackend, EvaluationSummary, KnowledgeBackendSettings, MuyeDataCandidateEvaluator
from muye_core.service import DomainError, InMemoryCoreStore
from muye_core.storage import ArtifactStore


class _Embedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts, *, model, dimensions, trace_id):
        self.calls += 1
        assert texts and model == "embedding_default" and dimensions == 4 and trace_id.startswith("build-")
        return [[float(index + 1)] * dimensions for index, _text in enumerate(texts)]


class _Publisher:
    def __init__(self) -> None:
        self.chunks = []

    def publish(self, *, plan, chunks, embeddings):
        assert plan.collection_name and len(chunks) == len(embeddings)
        self.chunks = list(chunks)


class _Evaluator:
    def evaluate(self, *, spec, manifest):
        return EvaluationSummary(
            passed=True,
            pass_rate=1.0,
            report={
                "schema_version": "muye.ai/revision-evaluation-report/v1",
                "revision_id": spec.revision_id,
                "resource_id": manifest.resource_id,
                "passed": True,
                "pass_rate": 1.0,
            },
        )


def _approved_revision(store: InMemoryCoreStore, artifacts: ArtifactStore):
    admin = store.bootstrap_admin("admin", "correct-horse-battery")
    content = b"# Leave\n\nAnnual leave requires approval.\n"
    stored = artifacts.store(io.BytesIO(content), filename="handbook.md")
    asset_id = f"asset_{stored.sha256[:16]}"
    config = {
        "display_name": "Employee assistant",
        "objective": "Answer from approved sources.",
        "instructions": "Use only retrieved evidence.",
        "prohibited_actions": ["Do not guess"],
        "examples": ["How do I request leave?"],
        "model": {"chat_alias": "chat_default", "embedding_alias": "embedding_default", "temperature": 0.0},
        "retrieval": {"pipeline": "hybrid", "top_k": 8, "rerank_alias": None, "minimum_score": 0.0},
        "budgets": {"output_tokens": 512, "tool_calls": 1, "timeout_seconds": 30},
        "evaluation": {"cases": [{"case_id": "leave", "question": "How do I request leave?", "expected_source_asset_ids": [asset_id]}], "minimum_pass_rate": 1.0, "citation_required": True},
    }
    agent, _draft = store.create_agent(admin, slug="backend-help", display_name="Employee assistant", description="Policy", config=config)
    store.attach_asset(admin, agent.agent_id, sha256=stored.sha256, size_bytes=stored.size_bytes, media_type="text/markdown", storage_key=stored.storage_key, display_name="handbook.md")
    revision = store.freeze_revision(admin, agent.agent_id, 2)
    return store.approve_revision(admin, revision.revision_id, revision.checksum)


def test_core_backend_builds_from_frozen_assets_and_persists_report(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = InMemoryCoreStore()
    revision = _approved_revision(store, artifacts)
    publisher = _Publisher()
    embedder = _Embedder()
    backend = CoreKnowledgeBackend(
        store=store,
        artifact_store=artifacts,
        settings=KnowledgeBackendSettings(embedding_dimensions=4),
        embedder=embedder,
        publisher=publisher,
        evaluator=_Evaluator(),
    )

    build = backend.build(revision.spec)
    evaluation = backend.evaluate(revision.spec, build)

    assert build.resources[0].collection_name.startswith("kb_")
    assert {chunk.source_asset_id for chunk in publisher.chunks} == {revision.spec.source_assets[0].asset_id}
    assert evaluation.passed is True
    assert artifacts.read_bytes(evaluation.report_ref)

    repeated_build = backend.build(revision.spec)
    assert repeated_build.resources == build.resources
    assert embedder.calls == 1

    changed_revision_backend = CoreKnowledgeBackend(
        store=store,
        artifact_store=artifacts,
        settings=KnowledgeBackendSettings(embedding_dimensions=4, embedding_revision="r2"),
        embedder=embedder,
        publisher=publisher,
        evaluator=_Evaluator(),
    )
    changed_revision_backend.build(revision.spec)
    assert embedder.calls == 2


def test_core_backend_rejects_artifact_content_drift(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifacts = ArtifactStore(root)
    store = InMemoryCoreStore()
    revision = _approved_revision(store, artifacts)
    asset = store.revision_assets(revision.revision_id)[0]
    (root / asset.storage_key).write_bytes(b"tampered")
    backend = CoreKnowledgeBackend(
        store=store,
        artifact_store=artifacts,
        settings=KnowledgeBackendSettings(embedding_dimensions=4),
        embedder=_Embedder(),
        publisher=_Publisher(),
        evaluator=_Evaluator(),
    )

    with pytest.raises(DomainError, match="checksum"):
        backend.build(revision.spec)


def test_candidate_evaluator_runs_dense_keyword_and_hybrid_with_citations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = InMemoryCoreStore()
    revision = _approved_revision(store, artifacts)
    backend = CoreKnowledgeBackend(
        store=store,
        artifact_store=artifacts,
        settings=KnowledgeBackendSettings(embedding_dimensions=4),
        embedder=_Embedder(),
        publisher=_Publisher(),
        evaluator=_Evaluator(),
    )
    build = backend.build(revision.spec)
    manifest = backend._contexts[build.build_id].manifest
    snapshot_path = tmp_path / "candidate" / "resource-snapshot.json"
    requested_pipelines: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/snapshot-identity":
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            resource = snapshot["resources"][manifest.resource_id]
            return httpx.Response(
                200,
                json={
                    "snapshot_revision": snapshot["snapshot_revision"],
                    "snapshot_checksum": snapshot["snapshot_checksum"],
                    "resources": {
                        manifest.resource_id: {
                            "resource_id": manifest.resource_id,
                            "resource_revision": resource["resource_revision"],
                            "resource_checksum": resource["resource_checksum"],
                            "knowledge_version_id": resource["knowledge_version_id"],
                            "collection_plan_checksum": resource["collection_plan_checksum"],
                        }
                    },
                },
            )
        payload = json.loads(request.content)
        requested_pipelines.append(payload["pipeline"])
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "fields": {
                            "source_asset_id": revision.spec.source_assets[0].asset_id,
                            "citation_id": "citation_0123456789abcdef",
                        }
                    }
                ]
            },
        )

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(backend_module.httpx, "Client", client_factory)
    evaluator = MuyeDataCandidateEvaluator(base_url="http://muye-data.test", snapshot_path=snapshot_path)

    result = evaluator.evaluate(spec=revision.spec, manifest=manifest)

    assert result.passed is True
    assert result.pass_rate == 1.0
    assert requested_pipelines == ["dense", "keyword", "hybrid"]
    assert set(result.report["pipelines"]) == {"dense", "keyword", "hybrid"}
