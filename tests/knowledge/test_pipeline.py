"""阶段 4 知识构建、评测与 Job 状态的离线回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from typing import Sequence
from types import ModuleType, SimpleNamespace
from zipfile import ZipFile

import pytest

from contracts.models import ResourceSnapshotV1, SchemaProposalV1
from tools.knowledge_pipeline.chunking import chunk_documents
from tools.knowledge_pipeline.checksums import canonical_checksum
from tools.knowledge_pipeline.embeddings import MuyeLLMEmbedder
from tools.knowledge_pipeline.errors import (
    ApprovalRequiredError,
    DependencyUnavailableError,
    OcrRequiredError,
    ParserFailedError,
)
from tools.knowledge_pipeline.milvus_publisher import MilvusPublisher
from tools.knowledge_pipeline.evaluation import ResourceSnapshotIdentity
from tools.knowledge_pipeline.models import KnowledgeChunkV1, RetrievedEvaluationHitV1
from tools.knowledge_pipeline.parsers import _convert_docling, _validate_docx_archive, parse_document
from tools.knowledge_pipeline.planning import build_collection_index_plan
from tools.knowledge_pipeline.worker import KnowledgeWorker


class _FakeEmbedder:
    """用固定向量替代网络 Embedding，保证测试与模型无关。"""

    def embed(self, texts: Sequence[str], *, model: str, dimensions: int, trace_id: str) -> list[list[float]]:
        assert model == "embed-v1"
        assert trace_id.startswith("job_")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _FakePublisher:
    """记录不可变发布请求，不访问真实 Milvus。"""

    def __init__(self) -> None:
        self.collection_name = ""
        self.chunk_ids: list[str] = []

    def publish(self, *, plan: object, chunks: Sequence[object], embeddings: Sequence[Sequence[float]]) -> None:
        self.collection_name = getattr(plan, "collection_name")
        self.chunk_ids = [getattr(chunk, "chunk_id") for chunk in chunks]
        assert len(chunks) == len(embeddings)
        assert all(len(item) == 4 for item in embeddings)


class _IdentityRunner:
    """显式模拟隔离 muye-data 已加载的 candidate Snapshot 身份。"""

    def __init__(self, identity: ResourceSnapshotIdentity) -> None:
        self._identity = identity

    def snapshot_identity(self, *, resource_id: str) -> ResourceSnapshotIdentity:
        assert resource_id == self._identity.resource_id
        return self._identity


class _PassingRunner(_IdentityRunner):
    """每个 pipeline 返回固定相关命中与 citation。"""

    def retrieve(
        self,
        *,
        resource_id: str,
        query: str,
        pipeline: str,
        top_k: int,
        trace_id: str,
    ) -> list[RetrievedEvaluationHitV1]:
        assert resource_id == "kb.product_handbook"
        assert query
        assert pipeline in {"dense", "keyword", "hybrid"}
        assert top_k == 3
        assert trace_id.startswith("evaluation-refund-")
        return [
            RetrievedEvaluationHitV1(
                chunk_id="chunk_refund_policy",
                citation_id="citation_refund_policy",
            )
        ]


class _FailingRunner(_IdentityRunner):
    """模拟空召回，验证未达标时不会激活 Resource Snapshot。"""

    def retrieve(self, **_kwargs: object) -> list[RetrievedEvaluationHitV1]:
        return []


def _write_source_config(workspace: Path) -> None:
    source = workspace / "config" / "knowledge-sources" / "product-handbook.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(
            [
                "schema_version: muye.ai/knowledge-source-config/v1",
                "knowledge_id: kb.product_handbook",
                "resource_id: kb.product_handbook",
                "slug: product-handbook",
                "display_name: 产品手册",
                "sources:",
                "  - path: product",
                "    include: [\"**/*.md\", \"**/*.txt\"]",
                "parser_profile: deterministic-text-v1",
                "embedding_alias: embed-v1",
                "embedding_dimensions: 4",
                "connection: milvus_default",
                "chunking:",
                "  max_characters: 200",
                "  overlap_characters: 20",
                "  min_characters: 20",
                "default_pipeline: hybrid",
                "evaluation_set_ref: knowledge-evaluations/product-handbook.yaml",
                "",
            ]
        ),
        encoding="utf-8",
    )
    evaluation = workspace / "config" / "knowledge-evaluations" / "product-handbook.yaml"
    evaluation.parent.mkdir(parents=True)
    evaluation_payload = {
        "schema_version": "muye.ai/evaluation-set/v1",
        "evaluation_set_id": "product_handbook_eval",
        "revision": "evaluation/20260730a",
        "recall_at_k": 3,
        "min_recall": 1.0,
        "min_mrr": 1.0,
        "min_citation_coverage": 1.0,
        "cases": [
            {
                "case_id": "refund",
                "query": "退款政策是什么？",
                "relevant_chunk_ids": ["chunk_refund_policy"],
                "required_citation_ids": ["citation_refund_policy"],
            }
        ],
    }
    evaluation_payload["checksum"] = canonical_checksum(evaluation_payload)
    evaluation.write_text(json.dumps(evaluation_payload, ensure_ascii=False), encoding="utf-8")


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_source_config(workspace)
    import_root = tmp_path / "imports"
    product = import_root / "product"
    product.mkdir(parents=True)
    (product / "handbook.md").write_text(
        "# 产品手册\n\n退款政策允许用户在购买后十四天内申请退款。\n",
        encoding="utf-8",
    )
    return workspace, import_root


def _candidate_identity(workspace: Path, manifest: object) -> ResourceSnapshotIdentity:
    """从 Worker 写入的 candidate 文件构造 fake runner 必须返回的运行时证明。"""
    snapshot = ResourceSnapshotV1.model_validate_json(
        (workspace / "config" / "generated" / "resource-snapshot.candidate.json").read_text(encoding="utf-8")
    )
    return ResourceSnapshotIdentity(
        snapshot_revision=snapshot.snapshot_revision,
        snapshot_checksum=snapshot.snapshot_checksum,
        resource_id=getattr(manifest, "resource_id"),
        resource_revision=getattr(manifest, "resource_revision"),
        resource_checksum=getattr(manifest, "resource_checksum"),
        knowledge_version_id=getattr(manifest, "knowledge_version_id"),
        collection_plan_checksum=getattr(manifest, "collection_plan_checksum"),
    )


def _approved_build(worker: KnowledgeWorker, import_root: Path):
    """生成并确认当前 Proposal 后构建一个可用于评测的 candidate。"""
    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)
    worker.approve_schema(
        slug="product-handbook",
        checksum=proposal.proposal.proposal_checksum,
        approved_by="reviewer",
    )
    build = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=_FakePublisher(),
    )
    assert build.manifest is not None
    return build


def test_worker_requires_exact_schema_approval_then_builds_immutable_manifest(tmp_path: Path) -> None:
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)

    rejected = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=_FakePublisher(),
    )
    assert rejected.manifest is None
    rejected_status = worker.status(rejected.job_id)
    assert rejected_status["status"] == "FAILED"
    assert rejected_status["error_code"] == "APPROVAL_REQUIRED"

    approval_path = worker.approve_schema(
        slug="product-handbook",
        checksum=proposal.proposal.proposal_checksum,
        approved_by="reviewer",
    )
    assert approval_path.is_file()
    publisher = _FakePublisher()
    result = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=publisher,
    )

    assert result.manifest is not None
    assert result.manifest.target == publisher.collection_name
    assert publisher.chunk_ids
    assert worker.status(result.job_id)["status"] == "SUCCEEDED"
    assert result.report_path is not None
    assert json.loads(result.report_path.read_text(encoding="utf-8"))["published"] is False
    assert (workspace / "config" / "generated" / "resource-snapshot.candidate.json").is_file()


def test_failed_evaluation_does_not_publish_and_passing_evaluation_activates_snapshot(tmp_path: Path) -> None:
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)
    worker.approve_schema(
        slug="product-handbook",
        checksum=proposal.proposal.proposal_checksum,
        approved_by="reviewer",
    )
    build = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=_FakePublisher(),
    )
    assert build.manifest is not None
    identity = _candidate_identity(workspace, build.manifest)

    failed = worker.evaluate(slug="product-handbook", runner=_FailingRunner(identity))
    assert worker.status(failed.job_id)["status"] == "FAILED"
    assert not (workspace / "config" / "generated" / "resource-snapshot.json").exists()

    passed = worker.evaluate(slug="product-handbook", runner=_PassingRunner(identity))
    assert worker.status(passed.job_id)["status"] == "SUCCEEDED"
    snapshot = ResourceSnapshotV1.model_validate_json(
        (workspace / "config" / "generated" / "resource-snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot.resources["kb.product_handbook"].resource_checksum == build.manifest.resource_checksum


def test_evaluation_rejects_runner_that_did_not_load_candidate_snapshot(tmp_path: Path) -> None:
    """命中内容正确不足以通过门禁，运行时 Snapshot 身份必须精确匹配 candidate。"""
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    build = _approved_build(worker, import_root)
    expected = _candidate_identity(workspace, build.manifest)
    wrong_identity = ResourceSnapshotIdentity(
        snapshot_revision=expected.snapshot_revision,
        snapshot_checksum="f" * 64,
        resource_id=expected.resource_id,
        resource_revision=expected.resource_revision,
        resource_checksum=expected.resource_checksum,
        knowledge_version_id=expected.knowledge_version_id,
        collection_plan_checksum=expected.collection_plan_checksum,
    )

    rejected = worker.evaluate(slug="product-handbook", runner=_PassingRunner(wrong_identity))

    assert worker.status(rejected.job_id)["status"] == "FAILED"
    assert not (workspace / "config" / "generated" / "resource-snapshot.json").exists()


class _ChangingCandidateRunner(_PassingRunner):
    """在评测期间替换 candidate 文件，模拟另一个构建进程的竞争发布。"""

    def __init__(self, identity: ResourceSnapshotIdentity, candidate_path: Path) -> None:
        super().__init__(identity)
        self._candidate_path = candidate_path
        self._changed = False

    def retrieve(self, **kwargs: object) -> list[RetrievedEvaluationHitV1]:
        if not self._changed:
            self._changed = True
            payload = json.loads(self._candidate_path.read_text(encoding="utf-8"))
            payload["snapshot_revision"] = "snapshot/kv_changed"
            payload.pop("snapshot_checksum")
            payload["snapshot_checksum"] = canonical_checksum(payload)
            self._candidate_path.write_text(json.dumps(payload), encoding="utf-8")
        return super().retrieve(**kwargs)


def test_evaluation_rejects_candidate_snapshot_changed_during_gate(tmp_path: Path) -> None:
    """评测结束时须再次读取 candidate，防止先评测 A 后发布 B。"""
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    build = _approved_build(worker, import_root)
    identity = _candidate_identity(workspace, build.manifest)
    runner = _ChangingCandidateRunner(
        identity,
        workspace / "config" / "generated" / "resource-snapshot.candidate.json",
    )

    rejected = worker.evaluate(slug="product-handbook", runner=runner)

    assert worker.status(rejected.job_id)["status"] == "FAILED"
    assert not (workspace / "config" / "generated" / "resource-snapshot.json").exists()


class _CancellationRaceWorker(KnowledgeWorker):
    """在 active Snapshot 写入入口发起并发取消，验证发布锁的顺序语义。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.job_id = ""
        self.cancel_error: Exception | None = None
        self.cancel_started = threading.Event()
        self.cancel_thread: threading.Thread | None = None

    def _activate_manifest(self, manifest: object) -> Path:
        def cancel() -> None:
            self.cancel_started.set()
            try:
                self.cancel(self.job_id)
            except Exception as exc:  # 取消已完成 Job 的拒绝是此竞争的预期结果。
                self.cancel_error = exc

        self.cancel_thread = threading.Thread(target=cancel)
        self.cancel_thread.start()
        assert self.cancel_started.wait(timeout=1)
        return super()._activate_manifest(manifest)  # type: ignore[arg-type]


def test_cancel_cannot_win_after_evaluation_enters_publication_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消要么在发布前成功，要么在发布后被拒绝，不能留下 CANCELLED + active Snapshot。"""
    workspace, import_root = _workspace(tmp_path)
    worker = _CancellationRaceWorker(workspace)
    build = _approved_build(worker, import_root)
    identity = _candidate_identity(workspace, build.manifest)
    original_create = worker._jobs.create

    def capture_job(**kwargs: object):
        job = original_create(**kwargs)
        worker.job_id = job.job_id
        return job

    monkeypatch.setattr(worker._jobs, "create", capture_job)
    result = worker.evaluate(slug="product-handbook", runner=_PassingRunner(identity))
    assert worker.cancel_thread is not None
    worker.cancel_thread.join(timeout=1)

    assert not worker.cancel_thread.is_alive()
    assert worker.status(result.job_id)["status"] == "SUCCEEDED"
    assert (workspace / "config" / "generated" / "resource-snapshot.json").is_file()
    assert isinstance(worker.cancel_error, ValueError)


def test_worker_recomputes_proposal_manifest_and_evaluation_checksums(tmp_path: Path) -> None:
    """带 checksum 的可提交 artifact 被篡改后必须在读取边界立即失败。"""
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)
    proposal_path = workspace / "config" / "generated" / "knowledge-proposals" / "product-handbook" / "current.json"
    proposal_payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal_payload["embedding_alias"] = "tampered-model"
    proposal_path.write_text(json.dumps(proposal_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Schema Proposal checksum"):
        worker._load_current_proposal("product-handbook")

    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)
    worker.approve_schema(slug="product-handbook", checksum=proposal.proposal.proposal_checksum, approved_by="reviewer")
    build = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=_FakePublisher(),
    )
    assert build.manifest is not None
    manifest_path = workspace / "config" / "generated" / "knowledge-manifests" / "product-handbook" / f"{build.manifest.knowledge_version_id}.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["target"] = "tampered_collection"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Manifest checksum"):
        worker.evaluate(slug="product-handbook", runner=_PassingRunner(_candidate_identity(workspace, build.manifest)))

    manifest_path.write_text(build.manifest.model_dump_json(), encoding="utf-8")
    evaluation_path = workspace / "config" / "knowledge-evaluations" / "product-handbook.yaml"
    evaluation_payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation_payload["min_recall"] = 0.0
    evaluation_path.write_text(json.dumps(evaluation_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Evaluation Set checksum"):
        worker.evaluate(slug="product-handbook", runner=_PassingRunner(_candidate_identity(workspace, build.manifest)))


def test_parser_rejects_source_symlink_outside_explicit_import_root(tmp_path: Path) -> None:
    workspace, import_root = _workspace(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("不应导入", encoding="utf-8")
    (import_root / "product" / "unsafe.md").symlink_to(outside)
    worker = KnowledgeWorker(workspace)

    with pytest.raises(ValueError, match="符号链接"):
        worker.propose_schema(slug="product-handbook", import_root=import_root)


def test_parser_keeps_distinct_file_and_chunk_ids_for_identical_content(tmp_path: Path) -> None:
    """相同正文处于不同文件时，引用与 Milvus 主键仍必须保持唯一。"""
    workspace, import_root = _workspace(tmp_path)
    duplicate = import_root / "product" / "handbook-copy.md"
    duplicate.write_text(
        "# 产品手册\n\n退款政策允许用户在购买后十四天内申请退款。\n",
        encoding="utf-8",
    )
    worker = KnowledgeWorker(workspace)
    config = worker._load_source_config("product-handbook")

    documents = worker._parse_current_documents(config, import_root=import_root, ocr_available=False)
    chunks = chunk_documents(documents, policy=config.chunking)

    assert len({document.source_file_id for document in documents}) == 2
    assert len({document.document_id for document in documents}) == 2
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("embedding_alias", "embed-v2"), ("embedding_revision", "r2")],
)
def test_embedding_identity_changes_create_a_new_knowledge_version(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    """Embedding alias 或 revision 改变时，绝不复用旧物理 Collection。"""
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    first = worker.propose_schema(slug="product-handbook", import_root=import_root)
    source_path = workspace / "config" / "knowledge-sources" / "product-handbook.yaml"
    content = source_path.read_text(encoding="utf-8")
    if field == "embedding_alias":
        source_path.write_text(content.replace("embedding_alias: embed-v1", f"embedding_alias: {replacement}"), encoding="utf-8")
    else:
        source_path.write_text(content.replace("embedding_dimensions: 4", f"embedding_revision: {replacement}\nembedding_dimensions: 4"), encoding="utf-8")

    second = worker.propose_schema(slug="product-handbook", import_root=import_root)
    first_plan = build_collection_index_plan(first.proposal)
    second_plan = build_collection_index_plan(second.proposal)

    assert second.proposal.knowledge_version_id != first.proposal.knowledge_version_id
    assert second_plan.collection_name != first_plan.collection_name
    assert second_plan.plan_checksum != first_plan.plan_checksum


def test_worker_enforces_source_parse_and_chunk_budgets(tmp_path: Path) -> None:
    """受控导入仍必须受文件、字节、block、chunk 和字符预算限制。"""
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    config = worker._load_source_config("product-handbook")
    duplicate = import_root / "product" / "copy.md"
    duplicate.write_text("# 副本\n\n额外段落。\n", encoding="utf-8")

    with pytest.raises(ValueError, match="文件数量"):
        worker._parse_current_documents(config.model_copy(update={"max_source_files": 1}), import_root=import_root, ocr_available=False)
    with pytest.raises(ValueError, match="总字节"):
        worker._parse_current_documents(config.model_copy(update={"max_total_source_bytes": 1}), import_root=import_root, ocr_available=False)

    documents = worker._parse_current_documents(config, import_root=import_root, ocr_available=False)
    with pytest.raises(ValueError, match="block"):
        worker._parse_current_documents(config.model_copy(update={"max_parsed_blocks": 1}), import_root=import_root, ocr_available=False)
    chunks = chunk_documents(documents, policy=config.chunking)
    with pytest.raises(ValueError, match="chunk 数量"):
        worker._validate_chunk_budget(chunks * 2, config.model_copy(update={"max_chunks": 1}))
    with pytest.raises(ValueError, match="总字符"):
        worker._validate_chunk_budget(chunks, config.model_copy(update={"max_total_chunk_characters": 1}))


class _RecordingEmbedder(_FakeEmbedder):
    """记录批次边界，证明 Worker 不会构造不受限的一次性 Embedding 请求。"""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed(self, texts: Sequence[str], *, model: str, dimensions: int, trace_id: str) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return super().embed(texts, model=model, dimensions=dimensions, trace_id=trace_id)


def test_worker_embeddings_are_batched(tmp_path: Path) -> None:
    """每个 Embedding 请求不超过 source config 声明的 batch size。"""
    workspace, import_root = _workspace(tmp_path)
    source_path = workspace / "config" / "knowledge-sources" / "product-handbook.yaml"
    source_path.write_text(
        source_path.read_text(encoding="utf-8").replace("embedding_dimensions: 4", "embedding_dimensions: 4\nembedding_batch_size: 1"),
        encoding="utf-8",
    )
    worker = KnowledgeWorker(workspace)
    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)
    worker.approve_schema(slug="product-handbook", checksum=proposal.proposal.proposal_checksum, approved_by="reviewer")
    embedder = _RecordingEmbedder()

    result = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=embedder,
        publisher=_FakePublisher(),
    )

    assert result.manifest is not None
    assert len(embedder.batch_sizes) > 1
    assert set(embedder.batch_sizes) == {1}


def test_docling_adapter_normalizes_pdf_and_docx_to_the_same_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docling 适配器对 PDF/DOCX 只消费文本产物，并保留各自相对来源定位。"""
    workspace, import_root = _workspace(tmp_path)
    config = KnowledgeWorker(workspace)._load_source_config("product-handbook").model_copy(
        update={"parser_profile": "docling-default-v1"}
    )
    pdf_path = import_root / "product" / "manual.pdf"
    docx_path = import_root / "product" / "manual.docx"
    pdf_path.write_bytes(b"%PDF-stage4-contract")
    docx_path.write_bytes(b"PK-stage4-contract")
    ocr_flags: list[bool] = []

    def fake_convert(_path: Path, *, enable_ocr: bool, **_kwargs: object) -> str:
        ocr_flags.append(enable_ocr)
        return "第一段\n\n第二段"

    monkeypatch.setattr("tools.knowledge_pipeline.parsers._convert_docling", fake_convert)
    monkeypatch.setattr("tools.knowledge_pipeline.parsers._validate_docx_archive", lambda *_args, **_kwargs: None)
    parsed_pdf = parse_document(
        pdf_path,
        import_root=import_root,
        config=config,
        knowledge_version_id="kv_0123456789abcdef",
    )
    parsed_docx = parse_document(
        docx_path,
        import_root=import_root,
        config=config,
        knowledge_version_id="kv_0123456789abcdef",
    )

    assert ocr_flags == [False, False]
    assert [block.content for block in parsed_pdf.blocks] == ["第一段", "第二段"]
    assert [block.locator.source_path for block in parsed_docx.blocks] == ["product/manual.docx"] * 2


def _install_fake_docling(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """提供最小 Docling module tree，隔离测试固定的 OCR adapter 配置。"""
    captured_options: list[object] = []

    class FakePipelineOptions:
        def __init__(self) -> None:
            self.do_ocr = True
            self.ocr_options: object | None = None

    class FakePaddleOcrOptions:
        pass

    class FakePdfFormatOption:
        def __init__(self, *, pipeline_options: object) -> None:
            captured_options.append(pipeline_options)

    class FakeDocumentConverter:
        def __init__(self, *, format_options: object) -> None:
            self.format_options = format_options

        def convert(self, _path: str) -> object:
            return SimpleNamespace(document=SimpleNamespace(pages={1: object()}, export_to_markdown=lambda: "parsed text"))

    docling = ModuleType("docling")
    docling.__path__ = []  # type: ignore[attr-defined]
    datamodel = ModuleType("docling.datamodel")
    datamodel.__path__ = []  # type: ignore[attr-defined]
    base_models = ModuleType("docling.datamodel.base_models")
    base_models.InputFormat = SimpleNamespace(PDF="pdf")
    pipeline_options = ModuleType("docling.datamodel.pipeline_options")
    pipeline_options.PdfPipelineOptions = FakePipelineOptions
    pipeline_options.PaddleOcrOptions = FakePaddleOcrOptions
    converter = ModuleType("docling.document_converter")
    converter.DocumentConverter = FakeDocumentConverter
    converter.PdfFormatOption = FakePdfFormatOption
    for name, module in {
        "docling": docling,
        "docling.datamodel": datamodel,
        "docling.datamodel.base_models": base_models,
        "docling.datamodel.pipeline_options": pipeline_options,
        "docling.document_converter": converter,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return captured_options


def test_docling_adapter_explicitly_disables_or_selects_paddle_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF 的 OCR 开关和 backend 不能依赖 Docling 的版本默认值。"""
    captured = _install_fake_docling(monkeypatch)
    monkeypatch.setitem(sys.modules, "paddle", ModuleType("paddle"))
    monkeypatch.setitem(sys.modules, "paddleocr", ModuleType("paddleocr"))
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"%PDF-fake")

    assert _convert_docling(path, enable_ocr=False) == "parsed text"
    assert _convert_docling(path, enable_ocr=True) == "parsed text"

    disabled, enabled = captured
    assert getattr(disabled, "do_ocr") is False
    assert getattr(disabled, "ocr_options") is None
    assert getattr(enabled, "do_ocr") is True
    assert type(getattr(enabled, "ocr_options")).__name__ == "FakePaddleOcrOptions"


def test_docling_parser_enforces_pdf_page_and_docx_archive_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """页数和 ZIP 元数据在解析前后均受 source config 的确定性预算约束。"""
    _install_fake_docling(monkeypatch)
    pdf_path = tmp_path / "manual.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    with pytest.raises(ParserFailedError, match="max_pdf_pages"):
        _convert_docling(pdf_path, enable_ocr=False, max_pages=0)

    docx_path = tmp_path / "manual.docx"
    with ZipFile(docx_path, "w") as archive:
        archive.writestr("word/document.xml", "x" * 64)
    with pytest.raises(ParserFailedError, match="解压后字节数"):
        _validate_docx_archive(
            docx_path,
            "manual.docx",
            max_entries=10,
            max_uncompressed_bytes=1,
        )


def test_docling_adapter_requires_paddle_dependencies_for_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声明 ocr:paddle 但没有运行时依赖时必须显式失败，不能回退到其他 backend。"""
    _install_fake_docling(monkeypatch)
    monkeypatch.delitem(sys.modules, "paddle", raising=False)
    monkeypatch.delitem(sys.modules, "paddleocr", raising=False)
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"%PDF-fake")

    with pytest.raises(DependencyUnavailableError, match="PaddleOCR"):
        _convert_docling(path, enable_ocr=True)


def test_scanned_pdf_without_ocr_capability_is_explicitly_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描 PDF 不能以空文档成功构建，必须要求独立 OCR capability。"""
    workspace, import_root = _workspace(tmp_path)
    config = KnowledgeWorker(workspace)._load_source_config("product-handbook").model_copy(
        update={"parser_profile": "docling-default-v1"}
    )
    pdf_path = import_root / "product" / "scanned.pdf"
    pdf_path.write_bytes(b"%PDF-scanned-contract")
    monkeypatch.setattr("tools.knowledge_pipeline.parsers._convert_docling", lambda *_args, **_kwargs: "")

    with pytest.raises(OcrRequiredError, match="ocr"):
        parse_document(
            pdf_path,
            import_root=import_root,
            config=config,
            knowledge_version_id="kv_0123456789abcdef",
        )


class _SourceChangingWorker(KnowledgeWorker):
    """在 approval 后、二次解析前模拟受控导入根发生竞争写入。"""

    def __init__(self, workspace_root: Path, source_path: Path) -> None:
        super().__init__(workspace_root)
        self._source_path = source_path

    def _require_schema_approval(self, slug: str, proposal: SchemaProposalV1) -> None:
        super()._require_schema_approval(slug, proposal)
        self._source_path.write_text("# 已变更\n\n审批后修改的正文。\n", encoding="utf-8")


def test_build_rejects_source_changes_after_schema_approval(tmp_path: Path) -> None:
    """审批绑定解析集，不能让构建窗口内的新正文复用旧 Proposal。"""
    workspace, import_root = _workspace(tmp_path)
    initial_worker = KnowledgeWorker(workspace)
    proposal = initial_worker.propose_schema(slug="product-handbook", import_root=import_root)
    initial_worker.approve_schema(
        slug="product-handbook",
        checksum=proposal.proposal.proposal_checksum,
        approved_by="reviewer",
    )
    publisher = _FakePublisher()
    worker = _SourceChangingWorker(workspace, import_root / "product" / "handbook.md")

    result = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=publisher,
    )

    assert result.manifest is None
    assert publisher.chunk_ids == []
    assert worker.status(result.job_id)["error_code"] == ApprovalRequiredError.code


class _FailingPublisher:
    """模拟可恢复的 Milvus 发布故障，供同步 retry 审计回归使用。"""

    def publish(self, **_kwargs: object) -> None:
        raise RuntimeError("temporary publisher failure")


def test_worker_retry_executes_new_attempt_without_leaving_queued_job(tmp_path: Path) -> None:
    """retry 只能同步重放已失败输入，不能创建没有执行者的 QUEUED Job。"""
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)
    worker.approve_schema(
        slug="product-handbook",
        checksum=proposal.proposal.proposal_checksum,
        approved_by="reviewer",
    )
    failed = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=_FailingPublisher(),
    )
    assert worker.status(failed.job_id)["status"] == "FAILED"

    retried = worker.retry_job(
        failed.job_id,
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=_FakePublisher(),
    )

    assert worker.status(failed.job_id)["status"] == "FAILED"
    assert worker.status(retried.job_id)["status"] == "SUCCEEDED"
    assert worker.status(retried.job_id)["attempt"] == 2
    jobs = list((workspace / "config" / "generated" / "knowledge-jobs").glob("job_*.json"))
    assert all(json.loads(path.read_text(encoding="utf-8"))["status"] != "QUEUED" for path in jobs)


def test_worker_retry_rejects_changed_input_without_creating_new_job(tmp_path: Path) -> None:
    """原 Job 的输入 checksum 不能被新源文件静默复用。"""
    workspace, import_root = _workspace(tmp_path)
    worker = KnowledgeWorker(workspace)
    proposal = worker.propose_schema(slug="product-handbook", import_root=import_root)
    worker.approve_schema(
        slug="product-handbook",
        checksum=proposal.proposal.proposal_checksum,
        approved_by="reviewer",
    )
    failed = worker.build(
        slug="product-handbook",
        import_root=import_root,
        embedder=_FakeEmbedder(),
        publisher=_FailingPublisher(),
    )
    (import_root / "product" / "handbook.md").write_text("# 已修改\n\n不同输入。\n", encoding="utf-8")

    with pytest.raises(ValueError, match="重试输入已变化"):
        worker.retry_job(
            failed.job_id,
            import_root=import_root,
            embedder=_FakeEmbedder(),
            publisher=_FakePublisher(),
        )

    jobs = list((workspace / "config" / "generated" / "knowledge-jobs").glob("job_*.json"))
    assert len(jobs) == 1


class _FakeSchema:
    def __init__(self) -> None:
        self.fields: list[dict[str, object]] = []
        self.functions: list[object] = []
        self.description = ""

    def add_field(self, **kwargs: object) -> None:
        self.fields.append(kwargs)

    def add_function(self, function: object) -> None:
        self.functions.append(function)


class _FakeIndexes:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def add_index(self, **kwargs: object) -> None:
        self.items.append(kwargs)


class _FakeMilvusClient:
    def __init__(self) -> None:
        self.schema = _FakeSchema()
        self.indexes = _FakeIndexes()
        self.records: list[dict[str, object]] = []
        self.collection_name = ""
        self.collection_exists = False

    def has_collection(self, **_kwargs: object) -> bool:
        return self.collection_exists

    def create_schema(self, **kwargs: object) -> _FakeSchema:
        self.schema.description = str(kwargs["description"])
        return self.schema

    def prepare_index_params(self) -> _FakeIndexes:
        return self.indexes

    def create_collection(self, *, collection_name: str, **_kwargs: object) -> None:
        self.collection_name = collection_name
        self.collection_exists = True

    def insert(self, *, data: list[dict[str, object]], **_kwargs: object) -> None:
        self.records.extend(data)

    def flush(self, **_kwargs: object) -> None:
        return None

    def load_collection(self, **_kwargs: object) -> None:
        return None

    def describe_collection(self, **_kwargs: object) -> dict[str, object]:
        return {
            "description": self.schema.description,
            "fields": [
                {
                    "name": item["field_name"],
                    "type": item["datatype"],
                    "is_primary": item["is_primary"],
                    "params": {
                        key: str(value)
                        for key, value in item.items()
                        if key in {"max_length", "dim", "enable_analyzer"}
                    },
                }
                for item in self.schema.fields
            ],
            "functions": [function.to_dict() for function in self.schema.functions],
        }

    def list_indexes(self, *, field_name: str, **_kwargs: object) -> list[str]:
        return [field_name] if any(item["field_name"] == field_name for item in self.indexes.items) else []

    def describe_index(self, *, index_name: str, **_kwargs: object) -> dict[str, object]:
        return next(item for item in self.indexes.items if item["field_name"] == index_name)

    def get(self, *, ids: list[str], output_fields: list[str], **_kwargs: object) -> list[dict[str, object]]:
        return [
            {name: record[name] for name in output_fields if name in record}
            for record in self.records
            if record["chunk_id"] in ids
        ]

    def query(self, *, output_fields: list[str], limit: int, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {name: record[name] for name in output_fields if name in record}
            for record in self.records[:limit]
        ]


def test_milvus_publisher_creates_fixed_bm25_schema_and_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, import_root = _workspace(tmp_path)
    proposal = KnowledgeWorker(workspace).propose_schema(slug="product-handbook", import_root=import_root)
    plan = build_collection_index_plan(proposal.proposal)
    chunk = KnowledgeChunkV1(
        chunk_id="chunk_0123456789abcdef",
        knowledge_version_id=plan.knowledge_version_id,
        document_id="doc_0123456789abcdef",
        source_file_id="file_0123456789abcdef",
        content="退款政策允许购买后十四天内退款。",
        title="handbook",
        citation_id="citation_0123456789abcdef",
        source_locators=[{"source_path": "product/handbook.md", "kind": "line", "start": 1, "end": 1}],
        block_ids=["block_0123456789abcdef"],
        chunk_index=0,
        content_hash="b" * 64,
    )
    client = _FakeMilvusClient()
    publisher = MilvusPublisher(uri="http://127.0.0.1:19530")
    monkeypatch.setattr(publisher, "_client", lambda: client)

    publisher.publish(plan=plan, chunks=[chunk], embeddings=[[1.0, 0.0, 0.0, 0.0]])

    assert client.collection_name == plan.collection_name
    assert client.schema.functions[0].to_dict()["name"] == "bm25_content"  # type: ignore[union-attr]
    assert {item["index_type"] for item in client.indexes.items} == {"FLAT", "SPARSE_INVERTED_INDEX"}
    content_field = next(item for item in client.schema.fields if item["field_name"] == "content")
    assert content_field["analyzer_params"] == {"tokenizer": "jieba"}
    assert client.records[0]["citation_id"] == chunk.citation_id

    publisher.publish(plan=plan, chunks=[chunk], embeddings=[[1.0, 0.0, 0.0, 0.0]])
    assert len(client.records) == 1

    client.records.append({**client.records[0], "chunk_id": "chunk_fedcba9876543210"})
    with pytest.raises(ParserFailedError, match="chunk 与当前 KnowledgeVersion 不匹配"):
        publisher.publish(plan=plan, chunks=[chunk], embeddings=[[1.0, 0.0, 0.0, 0.0]])

    with pytest.raises(ParserFailedError, match="Embedding 维度"):
        publisher.publish(plan=plan, chunks=[chunk], embeddings=[[float("nan"), 0.0, 0.0, 0.0]])


class _EmbeddingResponse:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self._payload = payload or {
            "success": True,
            "data": {"dimensions": 2, "embeddings": [[float("nan"), 0.0]]},
        }

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _EmbeddingClient:
    def __init__(self, *, response: _EmbeddingResponse | None = None, **_kwargs: object) -> None:
        self._response = response or _EmbeddingResponse()

    def __enter__(self) -> "_EmbeddingClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, *_args: object, **_kwargs: object) -> _EmbeddingResponse:
        return self._response


def test_embedder_rejects_non_finite_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络边界不能把 JSON 中的 NaN/Infinity 传给 Milvus。"""
    monkeypatch.setattr("tools.knowledge_pipeline.embeddings.httpx.Client", _EmbeddingClient)
    embedder = MuyeLLMEmbedder(base_url="http://muye-llm.test")

    with pytest.raises(DependencyUnavailableError, match="非有限"):
        embedder.embed(["测试"], model="embed-v1", dimensions=2, trace_id="job_test")


def test_embedder_reports_gateway_embedding_dependency_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """网关的固定 502 失败体必须保留为可操作的知识构建错误。"""
    response = _EmbeddingResponse(
        {"success": False, "code": 502, "message": "Embedding 服务调用失败", "data": {}}
    )
    monkeypatch.setattr(
        "tools.knowledge_pipeline.embeddings.httpx.Client",
        lambda **_kwargs: _EmbeddingClient(response=response),
    )
    embedder = MuyeLLMEmbedder(base_url="http://muye-llm.test")

    with pytest.raises(DependencyUnavailableError, match="上游连通性"):
        embedder.embed(["测试"], model="embed-v1", dimensions=2, trace_id="job_test")
