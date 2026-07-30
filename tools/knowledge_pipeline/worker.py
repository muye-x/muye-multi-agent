"""Knowledge Pipeline 编排：解析、审批、Milvus 发布、评测与快照激活。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from contracts.models import (
    EvaluationSetV1,
    KnowledgeResourceManifestV1,
    ResourceSnapshotV1,
    SchemaProposalV1,
)

from .checksums import canonical_checksum, file_checksum
from .chunking import chunk_documents
from .embeddings import Embedder, MuyeLLMEmbedder
from .errors import ApprovalRequiredError, JobCancelledError, KnowledgePipelineError
from .evaluation import MuyeDataRetrievalRunner, RetrievalRunner, evaluate_resource
from .io import load_json_model, load_yaml_model, write_json_atomic
from .jobs import JobStore, compute_job_input_checksum
from .milvus_publisher import MilvusPublisher, MilvusPublisherProtocol
from .models import KnowledgeSourceConfigV1, SchemaApprovalV1
from .parsers import discover_source_files, parse_documents
from .planning import (
    build_collection_index_plan,
    build_resource_manifest,
    build_schema_proposal,
    document_set_checksum,
)


@dataclass(frozen=True)
class ProposalResult:
    """Schema Proposal 与对应规范化文档的受控返回值。"""

    config: KnowledgeSourceConfigV1
    proposal: SchemaProposalV1
    proposal_path: Path


@dataclass(frozen=True)
class BuildResult:
    """成功或失败构建 Job 的可审计摘要。"""

    job_id: str
    manifest: KnowledgeResourceManifestV1 | None
    report_path: Path | None


class KnowledgeWorker:
    """本地、同步运行的阶段 4 Worker。

    Worker 只接受 workspace 根和显式 import root；路径、审批、artifact 与 Collection
    均由可信代码派生。构建成功只产生待发布 Manifest，评测门禁通过后才原子替换
    `resource-snapshot.json`。
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve(strict=True)
        self._jobs = JobStore(self._workspace_root)

    def propose_schema(self, *, slug: str, import_root: Path, ocr_available: bool = False) -> ProposalResult:
        """解析当前源文件并写入当前 KnowledgeVersion 的 Schema Proposal。"""
        config = self._load_source_config(slug)
        documents = self._parse_current_documents(config, import_root=import_root, ocr_available=ocr_available)
        proposal = build_schema_proposal(config, documents)
        path = self._proposal_path(slug, proposal.knowledge_version_id)
        write_json_atomic(path, proposal.model_dump(mode="json"))
        return ProposalResult(config=config, proposal=proposal, proposal_path=path)

    def approve_schema(self, *, slug: str, checksum: str, approved_by: str) -> Path:
        """记录对当前 Proposal 的精确 checksum 确认，不影响阶段 3 Resource 审批。"""
        proposal = self._load_current_proposal(slug)
        if checksum != proposal.proposal_checksum:
            raise ApprovalRequiredError("提交的 Schema Proposal checksum 与当前 Proposal 不一致")
        approval = SchemaApprovalV1(
            schema_version="muye.ai/schema-approval/v1",
            knowledge_slug=slug,
            proposal_revision=proposal.proposal_revision,
            proposal_checksum=proposal.proposal_checksum,
            approved_by=approved_by,
            approved_at=_timestamp(),
        )
        path = self._workspace_root / "config" / "approvals" / "schema" / f"{slug}.json"
        write_json_atomic(path, approval.model_dump(mode="json"))
        return path

    def build(
        self,
        *,
        slug: str,
        import_root: Path,
        embedder: Embedder | None = None,
        publisher: MilvusPublisherProtocol | None = None,
        ocr_available: bool = False,
        llm_base_url: str | None = None,
        milvus_uri: str | None = None,
    ) -> BuildResult:
        """构建一个不可变 Collection 与待评测 Manifest，错误转换为持久化 Job 报告。"""
        proposal_result = self.propose_schema(slug=slug, import_root=import_root, ocr_available=ocr_available)
        job = self._jobs.create(
            kind="build",
            knowledge_slug=slug,
            input_checksum=compute_job_input_checksum(
                kind="build",
                knowledge_slug=slug,
                payload={
                    "proposal_checksum": proposal_result.proposal.proposal_checksum,
                    "import_root": str(import_root.resolve(strict=True)),
                },
            ),
        )
        self._jobs.transition(job.job_id, status="RUNNING")
        try:
            self._require_schema_approval(slug, proposal_result.proposal)
            self._raise_if_cancelled(job.job_id)
            documents = self._parse_current_documents(
                proposal_result.config,
                import_root=import_root,
                ocr_available=ocr_available,
            )
            if (
                any(
                    document.knowledge_version_id != proposal_result.proposal.knowledge_version_id
                    for document in documents
                )
                or document_set_checksum(documents) != proposal_result.proposal.document_set_checksum
            ):
                raise ApprovalRequiredError(
                    "知识源在 Schema Proposal 确认后发生变化，必须重新生成并确认 Proposal"
                )
            chunks = chunk_documents(documents, policy=proposal_result.config.chunking)
            self._raise_if_cancelled(job.job_id)
            selected_embedder = embedder or MuyeLLMEmbedder(
                base_url=llm_base_url or os.environ.get("MUYE_KNOWLEDGE_LLM_BASE_URL", "http://127.0.0.1:9850")
            )
            vectors = selected_embedder.embed(
                [chunk.content for chunk in chunks],
                model=proposal_result.config.embedding_alias,
                dimensions=proposal_result.config.embedding_dimensions,
                trace_id=job.job_id,
            )
            self._raise_if_cancelled(job.job_id)
            plan = build_collection_index_plan(proposal_result.proposal)
            selected_publisher = publisher or MilvusPublisher(
                uri=milvus_uri or os.environ.get("MUYE_KNOWLEDGE_MILVUS_URI", "http://127.0.0.1:19530"),
                token=os.environ.get("MUYE_KNOWLEDGE_MILVUS_TOKEN") or None,
            )
            selected_publisher.publish(plan=plan, chunks=chunks, embeddings=vectors)
            self._raise_if_cancelled(job.job_id)
            manifest = build_resource_manifest(proposal_result.config, proposal_result.proposal, plan)
            manifest_path = self._manifest_path(slug, manifest.knowledge_version_id)
            write_json_atomic(manifest_path, manifest.model_dump(mode="json"))
            candidate_snapshot_path = self._write_candidate_snapshot(manifest)
            report_path = self._write_report(
                job.job_id,
                {
                    "kind": "build",
                    "status": "SUCCEEDED",
                    "knowledge_slug": slug,
                    "proposal_checksum": proposal_result.proposal.proposal_checksum,
                    "collection_plan_checksum": plan.plan_checksum,
                    "manifest_ref": _relative_to_workspace(manifest_path, self._workspace_root),
                    "candidate_snapshot_ref": _relative_to_workspace(candidate_snapshot_path, self._workspace_root),
                    "chunk_count": len(chunks),
                    "published": False,
                },
            )
            self._jobs.transition(
                job.job_id,
                status="SUCCEEDED",
                report_ref=_relative_to_workspace(report_path, self._workspace_root),
            )
            return BuildResult(job_id=job.job_id, manifest=manifest, report_path=report_path)
        except JobCancelledError:
            return BuildResult(job_id=job.job_id, manifest=None, report_path=None)
        except Exception as exc:
            report_path = self._fail_job(job.job_id, exc)
            return BuildResult(job_id=job.job_id, manifest=None, report_path=report_path)

    def evaluate(
        self,
        *,
        slug: str,
        runner: RetrievalRunner | None = None,
        data_base_url: str | None = None,
    ) -> BuildResult:
        """执行固定评测；全部 pipeline 通过门禁后才把 Manifest 加入 active Snapshot。"""
        config = self._load_source_config(slug)
        proposal = self._load_current_proposal(slug)
        manifest = self._load_manifest(slug, proposal.knowledge_version_id)
        evaluation_set = self._load_evaluation_set(config)
        job = self._jobs.create(
            kind="evaluate",
            knowledge_slug=slug,
            input_checksum=compute_job_input_checksum(
                kind="evaluate",
                knowledge_slug=slug,
                payload={
                    "manifest_checksum": manifest.resource_checksum,
                    "evaluation_set_checksum": evaluation_set.checksum,
                },
            ),
        )
        self._jobs.transition(job.job_id, status="RUNNING")
        try:
            self._raise_if_cancelled(job.job_id)
            selected_runner = runner or MuyeDataRetrievalRunner(
                base_url=data_base_url or os.environ.get("MUYE_DATA_URL", "http://127.0.0.1:9840")
            )
            evaluation = evaluate_resource(
                evaluation_set=evaluation_set,
                manifest=manifest,
                runner=selected_runner,
            )
            self._raise_if_cancelled(job.job_id)
            if evaluation.passed:
                snapshot_path = self._activate_manifest(manifest)
                snapshot_ref: str | None = _relative_to_workspace(snapshot_path, self._workspace_root)
            else:
                snapshot_ref = None
            report_path = self._write_report(
                job.job_id,
                {
                    "kind": "evaluate",
                    "status": "SUCCEEDED" if evaluation.passed else "FAILED",
                    "knowledge_slug": slug,
                    "manifest_checksum": manifest.resource_checksum,
                    "evaluation_set_checksum": evaluation_set.checksum,
                    "snapshot_ref": snapshot_ref,
                    "evaluation": evaluation.as_report(),
                },
            )
            if not evaluation.passed:
                self._jobs.transition(
                    job.job_id,
                    status="FAILED",
                    report_ref=_relative_to_workspace(report_path, self._workspace_root),
                    error_code="EVALUATION_FAILED",
                )
                return BuildResult(job_id=job.job_id, manifest=manifest, report_path=report_path)
            self._jobs.transition(
                job.job_id,
                status="SUCCEEDED",
                report_ref=_relative_to_workspace(report_path, self._workspace_root),
            )
            return BuildResult(job_id=job.job_id, manifest=manifest, report_path=report_path)
        except JobCancelledError:
            return BuildResult(job_id=job.job_id, manifest=None, report_path=None)
        except Exception as exc:
            report_path = self._fail_job(job.job_id, exc)
            return BuildResult(job_id=job.job_id, manifest=None, report_path=report_path)

    def status(self, job_id: str) -> dict[str, Any]:
        """返回 Job 当前状态与已存在报告的受限路径引用。"""
        job = self._jobs.load(job_id)
        return job.model_dump(mode="json")

    def cancel(self, job_id: str) -> dict[str, Any]:
        """请求取消并返回新的状态记录。"""
        return self._jobs.cancel(job_id).model_dump(mode="json")

    def retry(self, job_id: str) -> dict[str, Any]:
        """创建待重试 Job；实际执行仍需 CLI 提供新的显式 import root 或服务 URL。"""
        return self._jobs.retry(job_id).model_dump(mode="json")

    def _load_source_config(self, slug: str) -> KnowledgeSourceConfigV1:
        """读取与 Generator 输入隔离的阶段 4 源知识配置。"""
        path = self._workspace_root / "config" / "knowledge-sources" / f"{slug}.yaml"
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"阶段 4 知识源配置不存在、不是普通文件或是符号链接：{path}")
        config = load_yaml_model(path, KnowledgeSourceConfigV1)
        if config.slug != slug:
            raise ValueError("知识源配置中的 slug 必须与命令参数一致")
        return config

    def _parse_current_documents(
        self,
        config: KnowledgeSourceConfigV1,
        *,
        import_root: Path,
        ocr_available: bool,
    ) -> list[Any]:
        """先由源文件 checksum 派生 version，再解析，以保持文件变更与版本一一对应。"""
        paths = discover_source_files(config, import_root=import_root)
        source_set_checksum = canonical_checksum(
            [(path.relative_to(import_root.resolve(strict=True)).as_posix(), file_checksum(path)) for path in paths]
        )
        knowledge_version_id = f"kv_{source_set_checksum[:16]}"
        return parse_documents(
            paths,
            import_root=import_root,
            config=config,
            knowledge_version_id=knowledge_version_id,
            ocr_available=ocr_available,
        )

    def _load_current_proposal(self, slug: str) -> SchemaProposalV1:
        """选取当前 source config 的唯一最新 Proposal；不接受调用方传入路径。"""
        root = self._workspace_root / "config" / "generated" / "knowledge-proposals" / slug
        path = root / "current.json"
        if not path.is_file() or path.is_symlink():
            raise ApprovalRequiredError("尚未生成 Schema Proposal；请先执行 knowledge propose-schema")
        return load_json_model(path, SchemaProposalV1)

    def _require_schema_approval(self, slug: str, proposal: SchemaProposalV1) -> None:
        """精确复核人工审批的 slug/revision/checksum，变更后必须重新审批。"""
        path = self._workspace_root / "config" / "approvals" / "schema" / f"{slug}.json"
        if not path.is_file() or path.is_symlink():
            raise ApprovalRequiredError("当前 Schema Proposal 尚未确认")
        approval = load_json_model(path, SchemaApprovalV1)
        if (
            approval.knowledge_slug != slug
            or approval.proposal_revision != proposal.proposal_revision
            or approval.proposal_checksum != proposal.proposal_checksum
        ):
            raise ApprovalRequiredError("Schema Approval 与当前 Proposal 不匹配，必须重新确认")

    def _load_manifest(self, slug: str, knowledge_version_id: str) -> KnowledgeResourceManifestV1:
        """读取当前 version 的不可变 Manifest。"""
        path = self._manifest_path(slug, knowledge_version_id)
        if not path.is_file() or path.is_symlink():
            raise ValueError("当前 KnowledgeVersion 尚未构建 Manifest")
        return load_json_model(path, KnowledgeResourceManifestV1)

    def _load_evaluation_set(self, config: KnowledgeSourceConfigV1) -> EvaluationSetV1:
        """将受限的相对 reference 解析为工作区 config 文件。"""
        path = (self._workspace_root / "config" / config.evaluation_set_ref).resolve(strict=False)
        config_root = (self._workspace_root / "config").resolve(strict=True)
        try:
            path.relative_to(config_root)
        except ValueError as exc:
            raise ValueError("evaluation_set_ref 必须位于 config 根内") from exc
        if not path.is_file() or path.is_symlink():
            raise ValueError("评测集不存在、不是普通文件或是符号链接")
        return load_yaml_model(path, EvaluationSetV1)

    def _activate_manifest(self, manifest: KnowledgeResourceManifestV1) -> Path:
        """在完整校验候选后原子替换 active Resource Snapshot，失败保留旧快照。"""
        return self._write_snapshot(manifest, filename="resource-snapshot.json")

    def _write_candidate_snapshot(self, manifest: KnowledgeResourceManifestV1) -> Path:
        """写入待评测候选快照，供隔离 muye-data 实例读取而不影响 active 快照。"""
        return self._write_snapshot(manifest, filename="resource-snapshot.candidate.json")

    def _write_snapshot(self, manifest: KnowledgeResourceManifestV1, *, filename: str) -> Path:
        """从当前 active 快照和新 Manifest 构造完整候选后原子写入指定快照文件。"""
        path = self._workspace_root / "config" / "generated" / filename
        resources: dict[str, KnowledgeResourceManifestV1] = {}
        active_path = self._workspace_root / "config" / "generated" / "resource-snapshot.json"
        if active_path.is_file() and not active_path.is_symlink():
            previous = load_json_model(active_path, ResourceSnapshotV1)
            resources.update(previous.resources)
        resources[manifest.resource_id] = manifest
        payload = {
            "schema_version": "muye.ai/resource-snapshot/v1",
            "snapshot_revision": f"snapshot/{manifest.knowledge_version_id}",
            "resources": {
                resource_id: resource.model_dump(mode="json")
                for resource_id, resource in sorted(resources.items())
            },
        }
        snapshot = ResourceSnapshotV1.model_validate(
            {**payload, "snapshot_checksum": canonical_checksum(payload)}
        )
        write_json_atomic(path, snapshot.model_dump(mode="json"))
        return path

    def _proposal_path(self, slug: str, knowledge_version_id: str) -> Path:
        """每个 slug 只保留一个当前 Proposal，变更时原子替换而不是混用历史版本。"""
        del knowledge_version_id
        return self._workspace_root / "config" / "generated" / "knowledge-proposals" / slug / "current.json"

    def _manifest_path(self, slug: str, knowledge_version_id: str) -> Path:
        """Manifest 在 version 目录中不可变保存，便于回滚和 checksum 审计。"""
        return self._workspace_root / "config" / "generated" / "knowledge-manifests" / slug / f"{knowledge_version_id}.json"

    def _write_report(self, job_id: str, payload: dict[str, Any]) -> Path:
        """写入 Job 报告；报告只存 artifact reference 和错误码，不存凭据或正文。"""
        path = self._workspace_root / "config" / "generated" / "knowledge-reports" / f"{job_id}.json"
        write_json_atomic(path, {"schema_version": "muye.ai/knowledge-job-report/v1", **payload})
        return path

    def _fail_job(self, job_id: str, exc: Exception) -> Path | None:
        """把受控失败持久化为报告；已取消 Job 不会被失败状态覆盖。"""
        current = self._jobs.load(job_id)
        if current.status == "CANCELLED":
            return None
        error_code = exc.code if isinstance(exc, KnowledgePipelineError) else "PIPELINE_FAILED"
        report_path = self._write_report(
            job_id,
            {
                "kind": current.kind,
                "status": "FAILED",
                "knowledge_slug": current.knowledge_slug,
                "error_code": error_code,
                "message": str(exc),
            },
        )
        self._jobs.transition(
            job_id,
            status="FAILED",
            report_ref=_relative_to_workspace(report_path, self._workspace_root),
            error_code=error_code,
        )
        return report_path

    def _raise_if_cancelled(self, job_id: str) -> None:
        """在解析、Embedding、发布和激活之间响应协作式取消。"""
        if self._jobs.is_cancelled(job_id):
            raise JobCancelledError("知识 Job 已取消")


def _relative_to_workspace(path: Path, workspace_root: Path) -> str:
    """生成契约允许的 artifact 相对引用。"""
    return path.relative_to(workspace_root).as_posix()


def _timestamp() -> str:
    """生成秒精度 RFC 3339 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
