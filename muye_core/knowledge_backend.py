"""阶段 2 的生产知识构建适配器。

该模块把冻结 Revision 的内容寻址 Asset 转换为不可变 Milvus Collection，并通过
独立候选 Snapshot 对 Dense、Keyword、Hybrid 三条路径执行资料级评测。所有物理
地址均来自 Core 配置，不接受 API、Draft 或模型输出提供的路径和连接参数。
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
import time
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from contracts.models import ChunkingPolicyV1, KnowledgeResourceManifestV1, ResourceSnapshotV1
from contracts.models import ParsedBlockV1, ParsedDocumentV1
from contracts.v3 import AgentRevisionSpecV1, RuntimeResourceBindingV1
from tools.knowledge_pipeline.checksums import canonical_checksum
from tools.knowledge_pipeline.checksums import stable_identifier
from tools.knowledge_pipeline.chunking import chunk_documents
from tools.knowledge_pipeline.embeddings import Embedder, MuyeLLMEmbedder
from tools.knowledge_pipeline.milvus_publisher import MilvusPublisher, MilvusPublisherProtocol
from tools.knowledge_pipeline.models import KnowledgeChunkV1, KnowledgeSourceConfigV1, KnowledgeSourceSpecV1
from tools.knowledge_pipeline.parsers import parse_documents
from tools.knowledge_pipeline.planning import build_collection_index_plan, build_resource_manifest, build_schema_proposal

from .knowledge import EvaluationOutput, KnowledgeBuildOutput
from .service import DomainError, RevisionAssetRecord
from .storage import ArtifactStore, AssetValidationError


class RevisionAssetStore(Protocol):
    """Knowledge Backend 读取冻结资料所需的最小仓储边界。"""

    def revision_assets(self, revision_id: str) -> list[RevisionAssetRecord]: ...


@dataclass(frozen=True, slots=True)
class KnowledgeBackendSettings:
    """由部署配置注入且不会进入 Revision 的基础设施参数。"""

    embedding_dimensions: int
    embedding_revision: str = "r1"
    connection_name: str = "core_milvus"
    chunk_max_characters: int = 1_200
    chunk_overlap_characters: int = 120
    chunk_min_characters: int = 80
    embedding_batch_size: int = 64
    ocr_available: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.embedding_dimensions <= 65_536:
            raise ValueError("Embedding 维度必须在 1 到 65536 之间")
        if not self.embedding_revision or len(self.embedding_revision) > 128:
            raise ValueError("Embedding revision 无效")
        if not 1 <= self.embedding_batch_size <= 256:
            raise ValueError("Embedding batch size 必须在 1 到 256 之间")


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    """Evaluator 返回给 Backend 的受控指标，不包含资料正文。"""

    passed: bool
    pass_rate: float
    report: dict[str, object]


class CandidateEvaluator(Protocol):
    """对尚未成为 Active Route 的候选 Collection 执行固定评测。"""

    def evaluate(
        self,
        *,
        spec: AgentRevisionSpecV1,
        manifest: KnowledgeResourceManifestV1,
    ) -> EvaluationSummary: ...


@dataclass(frozen=True, slots=True)
class _BuildContext:
    manifest: KnowledgeResourceManifestV1


class CoreKnowledgeBackend:
    """复用既有解析和 Milvus 组件实现 v3 ``KnowledgeBackend``。"""

    def __init__(
        self,
        *,
        store: RevisionAssetStore,
        artifact_store: ArtifactStore,
        settings: KnowledgeBackendSettings,
        embedder: Embedder,
        publisher: MilvusPublisherProtocol,
        evaluator: CandidateEvaluator,
    ) -> None:
        self._store = store
        self._artifact_store = artifact_store
        self._settings = settings
        self._embedder = embedder
        self._publisher = publisher
        self._evaluator = evaluator
        self._contexts: dict[str, _BuildContext] = {}
        self._lock = RLock()

    def build(self, spec: AgentRevisionSpecV1) -> KnowledgeBuildOutput:
        """校验并物化冻结 Asset，构建确定性 chunk 和不可变 Collection。"""

        assets = self._store.revision_assets(spec.revision_id)
        expected = {source.asset_id: source.sha256 for source in spec.source_assets}
        if {asset.asset_id: asset.sha256 for asset in assets} != expected:
            raise DomainError("ASSET_DRIFT", "Revision 资料不存在或 checksum 漂移")
        config = self._source_config(spec, assets)
        # Revision checksum 不在 spec 字段内；revision_id 已是不可变随机身份，结合资料 hash
        # 生成稳定版本可避免重试创建不同 Collection。
        knowledge_version_id = f"kv_{canonical_checksum({'revision_id': spec.revision_id, 'assets': expected})[:32]}"
        documents = [self._parsed_document(asset, config, knowledge_version_id) for asset in assets]
        chunks = chunk_documents(documents, policy=config.chunking)
        source_assets = {document.source_file_id: asset.asset_id for document, asset in zip(documents, assets, strict=True)}
        chunks = [chunk.model_copy(update={"source_asset_id": source_assets[chunk.source_file_id]}) for chunk in chunks]
        vectors = self._embeddings(spec, chunks)
        proposal = build_schema_proposal(config, documents)
        plan = build_collection_index_plan(proposal)
        self._publisher.publish(plan=plan, chunks=chunks, embeddings=vectors)
        manifest = build_resource_manifest(config, proposal, plan)
        collection_checksum = canonical_checksum(
            {
                "plan_checksum": plan.plan_checksum,
                "chunks": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "content_hash": chunk.content_hash,
                        "source_asset_id": chunk.source_asset_id,
                    }
                    for chunk in chunks
                ],
                "embeddings": vectors,
            }
        )
        build_id = f"build_{spec.revision_id.removeprefix('revision_')}"
        with self._lock:
            self._contexts[build_id] = _BuildContext(manifest)
        return KnowledgeBuildOutput(
            build_id=build_id,
            resources=[
                RuntimeResourceBindingV1(
                    resource_id=manifest.resource_id,
                    collection_name=manifest.target,
                    collection_checksum=collection_checksum,
                    embedding_alias=manifest.embedding_alias,
                )
            ],
        )

    def evaluate(self, spec: AgentRevisionSpecV1, build: KnowledgeBuildOutput) -> EvaluationOutput:
        """评测刚构建的候选资源并原子保存审计报告。"""

        with self._lock:
            context = self._contexts.pop(build.build_id, None)
        if context is None:
            raise DomainError("WORKER_INTERRUPTED", "Knowledge build context 不存在，请重试 Job")
        summary = self._evaluator.evaluate(spec=spec, manifest=context.manifest)
        report_ref = self._artifact_store.store_evaluation_report(
            revision_id=spec.revision_id,
            build_id=build.build_id,
            report=summary.report,
        )
        return EvaluationOutput(summary.passed, summary.pass_rate, report_ref)

    def _parsed_document(
        self,
        asset: RevisionAssetRecord,
        config: KnowledgeSourceConfigV1,
        knowledge_version_id: str,
    ) -> ParsedDocumentV1:
        """复用按内容寻址的解析结果，并为当前 Revision 重建派生 identity。"""

        filename = f"{asset.asset_id}_{Path(asset.display_name).name}"
        name_checksum = sha256(filename.encode("utf-8")).hexdigest()[:16]
        cache_key = f"parsed/{asset.sha256}/{config.parser_profile}/{name_checksum}.json"
        try:
            cached = self._artifact_store.read_cache_json(cache_key)
        except AssetValidationError as exc:
            raise DomainError("ARTIFACT_CORRUPT", "解析缓存不可用") from exc
        if cached is None:
            try:
                content = self._artifact_store.read_bytes(asset.storage_key)
            except AssetValidationError as exc:
                raise DomainError("ASSET_DRIFT", "Revision 资料 Artifact 不可用") from exc
            if len(content) != asset.size_bytes or sha256(content).hexdigest() != asset.sha256:
                raise DomainError("ASSET_DRIFT", "Revision 资料内容与冻结 checksum 不一致")
            with tempfile.TemporaryDirectory(prefix="muye-core-parse-") as temporary_name:
                root = Path(temporary_name)
                path = root / filename
                path.write_bytes(content)
                document = parse_documents(
                    [path],
                    import_root=root,
                    config=config,
                    knowledge_version_id=knowledge_version_id,
                    ocr_available=self._settings.ocr_available,
                )[0]
            try:
                self._artifact_store.store_cache_json(cache_key, document.model_dump(mode="json"))
            except AssetValidationError as exc:
                raise DomainError("ARTIFACT_CORRUPT", "解析缓存无法安全发布") from exc
        else:
            try:
                document = ParsedDocumentV1.model_validate(cached)
            except Exception as exc:
                raise DomainError("ARTIFACT_CORRUPT", "解析缓存不符合 ParsedDocument 契约") from exc
            if document.source_checksum != asset.sha256 or document.source_path != filename:
                raise DomainError("ARTIFACT_CORRUPT", "解析缓存 identity 与 Revision Asset 不匹配")
        return _retarget_document(document, knowledge_version_id)

    def _embeddings(self, spec: AgentRevisionSpecV1, chunks: list[KnowledgeChunkV1]) -> list[list[float]]:
        """按模型 identity 和内容 hash 复用向量，只请求缺失 batch。"""

        vectors: list[list[float] | None] = [None] * len(chunks)
        missing: list[int] = []
        model_identity = canonical_checksum(
            {
                "alias": spec.model.embedding_alias,
                "revision": self._settings.embedding_revision,
            }
        )[:16]
        for index, chunk in enumerate(chunks):
            cache_key = f"embedding-cache/{model_identity}/{self._settings.embedding_dimensions}/{chunk.content_hash}.json"
            try:
                cached = self._artifact_store.read_cache_json(cache_key)
            except AssetValidationError as exc:
                raise DomainError("ARTIFACT_CORRUPT", "Embedding 缓存不可用") from exc
            if isinstance(cached, list) and len(cached) == self._settings.embedding_dimensions:
                try:
                    vector = [float(value) for value in cached]
                except (TypeError, ValueError):
                    vector = []
                if len(vector) == self._settings.embedding_dimensions and all(value == value and abs(value) != float("inf") for value in vector):
                    vectors[index] = vector
                    continue
            elif cached is not None:
                raise DomainError("ARTIFACT_CORRUPT", "Embedding 缓存维度或格式无效")
            missing.append(index)
        for offset in range(0, len(missing), self._settings.embedding_batch_size):
            indexes = missing[offset : offset + self._settings.embedding_batch_size]
            embedded = self._embedder.embed(
                [chunks[index].content for index in indexes],
                model=spec.model.embedding_alias,
                dimensions=self._settings.embedding_dimensions,
                trace_id=f"build-{spec.revision_id}-{offset // self._settings.embedding_batch_size}",
            )
            if len(embedded) != len(indexes):
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Embedding 数量与请求不一致")
            for index, vector in zip(indexes, embedded, strict=True):
                cache_key = f"embedding-cache/{model_identity}/{self._settings.embedding_dimensions}/{chunks[index].content_hash}.json"
                try:
                    self._artifact_store.store_cache_json(cache_key, vector)
                except AssetValidationError as exc:
                    raise DomainError("ARTIFACT_CORRUPT", "Embedding 缓存无法安全发布") from exc
                vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Embedding 结果不完整")
        return [vector for vector in vectors if vector is not None]

    def _source_config(self, spec: AgentRevisionSpecV1, assets: list[RevisionAssetRecord]) -> KnowledgeSourceConfigV1:
        suffixes = {Path(asset.display_name).suffix.lower() for asset in assets}
        parser_profile = "docling-default-v1" if suffixes & {".pdf", ".docx"} else "deterministic-text-v1"
        sources = [
            KnowledgeSourceSpecV1(
                path=f"{asset.asset_id}_{Path(asset.display_name).name}",
                include=[f"**/*{Path(asset.display_name).suffix.lower()}"],
            )
            for asset in assets
        ]
        slug = spec.agent_id.removeprefix("agent_").replace("_", "-")
        return KnowledgeSourceConfigV1(
            schema_version="muye.ai/knowledge-source-config/v1",
            knowledge_id=f"kb.{slug}",
            resource_id=f"kb.{slug}",
            slug=slug,
            display_name=spec.display_name,
            sources=sources,
            parser_profile=parser_profile,
            embedding_alias=spec.model.embedding_alias,
            embedding_revision=self._settings.embedding_revision,
            embedding_dimensions=self._settings.embedding_dimensions,
            connection=self._settings.connection_name,
            chunking=ChunkingPolicyV1(
                max_characters=self._settings.chunk_max_characters,
                overlap_characters=self._settings.chunk_overlap_characters,
                min_characters=self._settings.chunk_min_characters,
            ),
            rerank_alias=spec.retrieval.rerank_alias,
            rerank_required=spec.retrieval.rerank_alias is not None,
            evaluation_set_ref=f"evaluations/{spec.revision_id}.json",
            embedding_batch_size=self._settings.embedding_batch_size,
        )


class MuyeDataCandidateEvaluator:
    """通过专用 muye-data 候选 Snapshot 评测资料命中和 citation。"""

    def __init__(self, *, base_url: str, snapshot_path: Path, reload_timeout_seconds: float = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._snapshot_path = snapshot_path.absolute()
        self._reload_timeout_seconds = reload_timeout_seconds
        parsed = urlsplit(self._base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or reload_timeout_seconds <= 0:
            raise ValueError("候选评测服务配置无效")

    def evaluate(self, *, spec: AgentRevisionSpecV1, manifest: KnowledgeResourceManifestV1) -> EvaluationSummary:
        """发布单资源候选 Snapshot，等待加载后执行固定的三路评测。"""

        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._snapshot_path.with_suffix(f"{self._snapshot_path.suffix}.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            snapshot = self._snapshot(manifest)
            self._publish_snapshot(snapshot)
            with httpx.Client(base_url=self._base_url, timeout=30, trust_env=False) as client:
                self._wait_until_loaded(client, snapshot, manifest)
                pipeline_reports: dict[str, object] = {}
                pipeline_pass_rates: list[float] = []
                for pipeline in ("dense", "keyword", "hybrid"):
                    case_results: list[dict[str, object]] = []
                    passed_cases = 0
                    for case in spec.evaluation.cases:
                        hits = self._retrieve(client, manifest.resource_id, case.question, pipeline, spec.retrieval.top_k)
                        expected = set(case.expected_source_asset_ids)
                        relevant = [hit for hit in hits if hit["source_asset_id"] in expected]
                        citation_passed = not spec.evaluation.citation_required or any(hit["citation_id"] for hit in relevant)
                        case_passed = bool(relevant) and citation_passed
                        passed_cases += int(case_passed)
                        case_results.append(
                            {
                                "case_id": case.case_id,
                                "passed": case_passed,
                                "first_relevant_rank": next((index for index, hit in enumerate(hits, 1) if hit in relevant), None),
                                "citation_passed": citation_passed,
                            }
                        )
                    pipeline_rate = passed_cases / len(spec.evaluation.cases)
                    pipeline_pass_rates.append(pipeline_rate)
                    pipeline_reports[pipeline] = {
                        "pass_rate": pipeline_rate,
                        "passed": pipeline_rate >= spec.evaluation.minimum_pass_rate,
                        "cases": case_results,
                    }
                self._require_identity(client, snapshot, manifest)
        pass_rate = min(pipeline_pass_rates)
        return EvaluationSummary(
            passed=pass_rate >= spec.evaluation.minimum_pass_rate,
            pass_rate=pass_rate,
            report={
                "schema_version": "muye.ai/revision-evaluation-report/v1",
                "revision_id": spec.revision_id,
                "resource_id": manifest.resource_id,
                "resource_checksum": manifest.resource_checksum,
                "pass_rate": pass_rate,
                "minimum_pass_rate": spec.evaluation.minimum_pass_rate,
                "passed": pass_rate >= spec.evaluation.minimum_pass_rate,
                "pipelines": pipeline_reports,
            },
        )

    @staticmethod
    def _snapshot(manifest: KnowledgeResourceManifestV1) -> ResourceSnapshotV1:
        payload = {
            "schema_version": "muye.ai/resource-snapshot/v1",
            "snapshot_revision": f"candidate/{manifest.knowledge_version_id}",
            "resources": {manifest.resource_id: manifest.model_dump(mode="json")},
        }
        return ResourceSnapshotV1.model_validate({**payload, "snapshot_checksum": canonical_checksum(payload)})

    def _publish_snapshot(self, snapshot: ResourceSnapshotV1) -> None:
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        if self._snapshot_path.parent.is_symlink() or self._snapshot_path.is_symlink():
            raise DomainError("VALIDATION_ERROR", "候选 Snapshot 路径不能包含符号链接")
        content = (json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(prefix="candidate-", dir=self._snapshot_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self._snapshot_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def _wait_until_loaded(self, client: httpx.Client, snapshot: ResourceSnapshotV1, manifest: KnowledgeResourceManifestV1) -> None:
        deadline = time.monotonic() + self._reload_timeout_seconds
        while True:
            try:
                self._require_identity(client, snapshot, manifest)
                return
            except (httpx.HTTPError, DomainError):
                if time.monotonic() >= deadline:
                    raise DomainError("DEPENDENCY_UNAVAILABLE", "候选检索 Snapshot 未在期限内加载")
                time.sleep(0.25)

    @staticmethod
    def _require_identity(client: httpx.Client, snapshot: ResourceSnapshotV1, manifest: KnowledgeResourceManifestV1) -> None:
        response = client.get("/api/v1/snapshot-identity")
        response.raise_for_status()
        payload = response.json()
        resource = payload.get("resources", {}).get(manifest.resource_id) if isinstance(payload, dict) else None
        if (
            not isinstance(resource, dict)
            or payload.get("snapshot_checksum") != snapshot.snapshot_checksum
            or resource.get("resource_checksum") != manifest.resource_checksum
            or resource.get("knowledge_version_id") != manifest.knowledge_version_id
            or resource.get("collection_plan_checksum") != manifest.collection_plan_checksum
        ):
            raise DomainError("DEPENDENCY_UNAVAILABLE", "候选检索 Snapshot identity 不匹配")

    @staticmethod
    def _retrieve(client: httpx.Client, resource_id: str, query: str, pipeline: str, top_k: int) -> list[dict[str, str | None]]:
        response = client.post(
            "/api/v1/retrieve",
            json={
                "resource": resource_id,
                "query": query,
                "pipeline": pipeline,
                "top_k": top_k,
                "return_fields": ["source_asset_id", "citation_id"],
                "trace_id": f"evaluation-{pipeline}-{sha256(query.encode()).hexdigest()[:16]}",
            },
        )
        response.raise_for_status()
        payload = response.json()
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            raise DomainError("DEPENDENCY_UNAVAILABLE", "候选检索响应不包含 hits")
        normalized: list[dict[str, str | None]] = []
        for hit in hits:
            fields = hit.get("fields") if isinstance(hit, dict) else None
            if not isinstance(fields, dict) or not isinstance(fields.get("source_asset_id"), str):
                raise DomainError("DEPENDENCY_UNAVAILABLE", "候选检索响应缺少 source_asset_id")
            citation_id = fields.get("citation_id")
            if citation_id is not None and not isinstance(citation_id, str):
                raise DomainError("DEPENDENCY_UNAVAILABLE", "候选检索 citation 格式无效")
            normalized.append({"source_asset_id": fields["source_asset_id"], "citation_id": citation_id})
        return normalized


def create_configured_knowledge_backend(*, store: RevisionAssetStore, artifact_store: ArtifactStore) -> CoreKnowledgeBackend:
    """从环境构造生产 Backend；缺少必需基础设施配置时拒绝启动 Worker。"""

    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ValueError(f"Knowledge Worker 需要 {name}")
        return value

    settings = KnowledgeBackendSettings(
        embedding_dimensions=int(required("MUYE_CORE_EMBEDDING_DIMENSIONS")),
        embedding_revision=required("MUYE_CORE_EMBEDDING_REVISION"),
        connection_name=os.environ.get("MUYE_CORE_MILVUS_CONNECTION", "core_milvus").strip() or "core_milvus",
        ocr_available=os.environ.get("MUYE_CORE_OCR_AVAILABLE", "false").lower() == "true",
    )
    return CoreKnowledgeBackend(
        store=store,
        artifact_store=artifact_store,
        settings=settings,
        embedder=MuyeLLMEmbedder(base_url=required("MUYE_CORE_LLM_BASE_URL")),
        publisher=MilvusPublisher(
            uri=required("MUYE_CORE_MILVUS_URI"),
            token=os.environ.get("MUYE_CORE_MILVUS_TOKEN", "").strip() or None,
        ),
        evaluator=MuyeDataCandidateEvaluator(
            base_url=required("MUYE_CORE_EVALUATION_DATA_BASE_URL"),
            snapshot_path=Path(required("MUYE_CORE_EVALUATION_SNAPSHOT_PATH")),
        ),
    )


def _retarget_document(document: ParsedDocumentV1, knowledge_version_id: str) -> ParsedDocumentV1:
    """将缓存的确定性解析内容绑定到当前 KnowledgeVersion。"""

    document_id = stable_identifier(
        "doc_",
        knowledge_version_id,
        document.source_path,
        document.source_checksum,
    )
    blocks = [
        ParsedBlockV1(
            block_id=stable_identifier("block_", document_id, str(block.ordinal), block.content),
            ordinal=block.ordinal,
            content=block.content,
            locator=block.locator,
        )
        for block in document.blocks
    ]
    return document.model_copy(
        update={
            "knowledge_version_id": knowledge_version_id,
            "document_id": document_id,
            "blocks": blocks,
        }
    )
