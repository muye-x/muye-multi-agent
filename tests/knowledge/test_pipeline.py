"""阶段 4 知识构建、评测与 Job 状态的离线回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from contracts.models import ResourceSnapshotV1, SchemaProposalV1
from tools.knowledge_pipeline.chunking import chunk_documents
from tools.knowledge_pipeline.embeddings import MuyeLLMEmbedder
from tools.knowledge_pipeline.errors import (
    ApprovalRequiredError,
    DependencyUnavailableError,
    OcrRequiredError,
    ParserFailedError,
)
from tools.knowledge_pipeline.jobs import JobStore
from tools.knowledge_pipeline.milvus_publisher import MilvusPublisher
from tools.knowledge_pipeline.models import KnowledgeChunkV1, RetrievedEvaluationHitV1
from tools.knowledge_pipeline.parsers import parse_document
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


class _PassingRunner:
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


class _FailingRunner:
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
    evaluation.write_text(
        "\n".join(
            [
                "schema_version: muye.ai/evaluation-set/v1",
                "evaluation_set_id: product_handbook_eval",
                "revision: evaluation/20260730a",
                "checksum: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "recall_at_k: 3",
                "min_recall: 1.0",
                "min_mrr: 1.0",
                "min_citation_coverage: 1.0",
                "cases:",
                "  - case_id: refund",
                "    query: 退款政策是什么？",
                "    relevant_chunk_ids: [chunk_refund_policy]",
                "    required_citation_ids: [citation_refund_policy]",
                "",
            ]
        ),
        encoding="utf-8",
    )


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

    failed = worker.evaluate(slug="product-handbook", runner=_FailingRunner())
    assert worker.status(failed.job_id)["status"] == "FAILED"
    assert not (workspace / "config" / "generated" / "resource-snapshot.json").exists()

    passed = worker.evaluate(slug="product-handbook", runner=_PassingRunner())
    assert worker.status(passed.job_id)["status"] == "SUCCEEDED"
    snapshot = ResourceSnapshotV1.model_validate_json(
        (workspace / "config" / "generated" / "resource-snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot.resources["kb.product_handbook"].resource_checksum == build.manifest.resource_checksum


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

    def fake_convert(_path: Path, *, enable_ocr: bool) -> str:
        ocr_flags.append(enable_ocr)
        return "第一段\n\n第二段"

    monkeypatch.setattr("tools.knowledge_pipeline.parsers._convert_docling", fake_convert)
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


def test_job_cancel_and_retry_preserve_original_audit_record(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = store.create(kind="build", knowledge_slug="product-handbook", input_checksum="a" * 64)
    cancelled = store.cancel(job.job_id)

    retry = store.retry(cancelled.job_id)

    assert store.load(job.job_id).status == "CANCELLED"
    assert retry.status == "QUEUED"
    assert retry.attempt == 2
    assert retry.job_id != job.job_id


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
    assert client.records[0]["citation_id"] == chunk.citation_id

    publisher.publish(plan=plan, chunks=[chunk], embeddings=[[1.0, 0.0, 0.0, 0.0]])
    assert len(client.records) == 1

    with pytest.raises(ParserFailedError, match="Embedding 维度"):
        publisher.publish(plan=plan, chunks=[chunk], embeddings=[[float("nan"), 0.0, 0.0, 0.0]])


class _EmbeddingResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"success": True, "data": {"dimensions": 2, "embeddings": [[float("nan"), 0.0]]}}


class _EmbeddingClient:
    def __init__(self, **_kwargs: object) -> None:
        return None

    def __enter__(self) -> "_EmbeddingClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, *_args: object, **_kwargs: object) -> _EmbeddingResponse:
        return _EmbeddingResponse()


def test_embedder_rejects_non_finite_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络边界不能把 JSON 中的 NaN/Infinity 传给 Milvus。"""
    monkeypatch.setattr("tools.knowledge_pipeline.embeddings.httpx.Client", _EmbeddingClient)
    embedder = MuyeLLMEmbedder(base_url="http://muye-llm.test")

    with pytest.raises(DependencyUnavailableError, match="非有限"):
        embedder.embed(["测试"], model="embed-v1", dimensions=2, trace_id="job_test")
