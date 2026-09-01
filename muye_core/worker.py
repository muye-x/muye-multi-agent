"""阶段 2 Core Knowledge Job Worker。"""

from __future__ import annotations

from typing import Protocol

from .jobs import JobRecord
from .knowledge import KnowledgeBackend, build_and_evaluate
from .service import DomainError, RevisionRecord
from .storage import ArtifactStore


class KnowledgeJobStore(Protocol):
    """Knowledge Worker 所需的受限仓储操作，避免接受无类型的通用对象。"""

    def claim_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        job_types: frozenset[str] | None = None,
    ) -> JobRecord | None: ...

    def job_detail(self, job_id: str) -> JobRecord: ...

    def revision_detail(self, revision_id: str) -> RevisionRecord: ...

    def complete_job(
        self,
        *,
        worker_id: str,
        job_id: str,
        status: str,
        error_code: str | None = None,
    ) -> JobRecord: ...

    def publish_revision_ready(
        self,
        *,
        worker_id: str,
        job_id: str,
        revision_id: str,
        build_id: str,
        bundle_checksum: str,
        report_ref: str,
        storage_key: str,
        collection_name: str,
        collection_checksum: str,
        pass_rate: float,
    ) -> RevisionRecord: ...


class KnowledgeJobWorker:
    """领取一个 BUILD Job 并将通过评测的不可变 Revision 交给仓储完成发布。

    仓储负责事务性持久化 Build、Evaluation、Bundle 与 Revision 状态；Worker 不接收
    来自 API 的路径、镜像、命令或任意执行参数。
    """

    def __init__(self, *, store: KnowledgeJobStore, backend: KnowledgeBackend, artifact_store: ArtifactStore, worker_id: str) -> None:
        if not worker_id:
            raise ValueError("worker_id 不能为空")
        self._store = store
        self._backend = backend
        self._artifact_store = artifact_store
        self._worker_id = worker_id

    def run_once(self) -> str | None:
        """执行一个可领取的构建 Job，返回 Job ID；无工作时返回 ``None``。"""

        job = self._store.claim_job(worker_id=self._worker_id, job_types=frozenset({"BUILD"}))
        if job is None:
            return None
        try:
            revision = self._store.revision_detail(job.revision_id)
            if revision.status != "APPROVED":
                raise DomainError("CONFLICT", "Job 对应 Revision 当前不可构建")
            result = build_and_evaluate(revision.spec, self._backend)
            if len(result.resources) != 1:
                raise DomainError("VALIDATION_ERROR", "当前 Revision 只能发布一个知识资源", status_code=422)
            if self._store.job_detail(job.job_id).status == "CANCEL_REQUESTED":
                self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="CANCELLED")
                return job.job_id
            resource = result.resources[0]
            storage_key = self._artifact_store.store_bundle(agent_id=revision.agent_id, revision_id=revision.revision_id, bundle_checksum=result.bundle_checksum, members=result.bundle_members)
            self._store.publish_revision_ready(worker_id=self._worker_id, job_id=job.job_id, revision_id=revision.revision_id, build_id=result.build_id, bundle_checksum=result.bundle_checksum, report_ref=result.report_ref, storage_key=storage_key, collection_name=resource.collection_name, collection_checksum=resource.collection_checksum, pass_rate=result.pass_rate)
        except DomainError as exc:
            status = self._store.job_detail(job.job_id).status
            if status == "CANCEL_REQUESTED":
                self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="CANCELLED")
            elif status not in {"CANCELLED", "SUCCEEDED", "FAILED"}:
                self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="FAILED", error_code=exc.code)
        except Exception:
            status = self._store.job_detail(job.job_id).status
            if status == "CANCEL_REQUESTED":
                self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="CANCELLED")
            elif status not in {"CANCELLED", "SUCCEEDED", "FAILED"}:
                self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="FAILED", error_code="DEPENDENCY_UNAVAILABLE")
        return job.job_id
