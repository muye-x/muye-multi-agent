"""阶段 2 Core Knowledge Job Worker。"""

from __future__ import annotations

from .knowledge import KnowledgeBackend, build_and_evaluate
from .service import DomainError


class KnowledgeJobWorker:
    """领取一个 BUILD Job 并将通过评测的不可变 Revision 交给仓储完成发布。

    仓储负责事务性持久化 Build、Evaluation、Bundle 与 Revision 状态；Worker 不接收
    来自 API 的路径、镜像、命令或任意执行参数。
    """

    def __init__(self, *, store: object, backend: KnowledgeBackend, worker_id: str) -> None:
        if not worker_id:
            raise ValueError("worker_id 不能为空")
        self._store = store
        self._backend = backend
        self._worker_id = worker_id

    def run_once(self) -> str | None:
        """执行一个可领取的构建 Job，返回 Job ID；无工作时返回 ``None``。"""

        job = self._store.claim_job(worker_id=self._worker_id)
        if job is None:
            return None
        if job.job_type != "BUILD":
            self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="FAILED", error_code="UNSUPPORTED_JOB")
            return job.job_id
        try:
            revision = self._store.revision_detail(job.revision_id)
            if revision.status != "APPROVED":
                raise DomainError("CONFLICT", "Job 对应 Revision 当前不可构建")
            result = build_and_evaluate(revision.spec, self._backend)
            resource = result.resources[0]
            self._store.mark_revision_ready(job.revision_id, build_id=result.build_id, bundle_checksum=result.bundle_checksum, report_ref=result.report_ref, storage_key=f"bundles/in-memory/{result.bundle_checksum}", collection_name=resource.collection_name, collection_checksum=resource.collection_checksum)
            self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="SUCCEEDED")
        except DomainError as exc:
            self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="FAILED", error_code=exc.code)
        except Exception:
            self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="FAILED", error_code="DEPENDENCY_UNAVAILABLE")
        return job.job_id
