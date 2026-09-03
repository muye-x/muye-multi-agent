"""阶段 2 Core Knowledge Job Worker。"""

from __future__ import annotations

from contextlib import AbstractContextManager
import logging
from threading import Event, Thread
from typing import Protocol

from tools.knowledge_pipeline.errors import KnowledgePipelineError

from .jobs import JobRecord
from .knowledge import KnowledgeBackend, build_and_evaluate
from .proposals import ProfileProposalBackend
from .service import DomainError, RevisionRecord
from .storage import ArtifactStore


logger = logging.getLogger(__name__)


class KnowledgeJobStore(Protocol):
    """Knowledge Worker 所需的受限仓储操作，避免接受无类型的通用对象。"""

    def claim_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        job_types: frozenset[str] | None = None,
    ) -> JobRecord | None: ...

    def renew_job_lease(
        self,
        *,
        worker_id: str,
        job_id: str,
        lease_seconds: int = 60,
    ) -> JobRecord: ...

    def record_job_progress(
        self,
        *,
        worker_id: str,
        job_id: str,
        stage: str,
        current: int,
        total: int,
    ) -> None: ...

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
    """领取一个知识 Job 并将通过评测的不可变 Revision 交给仓储完成发布。

    仓储负责事务性持久化 Build、Evaluation、Bundle 与 Revision 状态；Worker 不接收
    来自 API 的路径、镜像、命令或任意执行参数。
    """

    def __init__(
        self,
        *,
        store: KnowledgeJobStore,
        backend: KnowledgeBackend,
        artifact_store: ArtifactStore,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id 不能为空")
        if lease_seconds < 3:
            raise ValueError("lease_seconds 不能小于 3")
        self._store = store
        self._backend = backend
        self._artifact_store = artifact_store
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def run_once(self) -> str | None:
        """执行一个可领取的构建 Job，返回 Job ID；无工作时返回 ``None``。"""

        job = self._store.claim_job(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            job_types=frozenset({"BUILD", "EVALUATE"}),
        )
        if job is None:
            return None
        try:
            with _LeaseHeartbeat(
                store=self._store,
                worker_id=self._worker_id,
                job_id=job.job_id,
                lease_seconds=self._lease_seconds,
            ) as heartbeat:
                if self._store.job_detail(job.job_id).status == "CANCEL_REQUESTED":
                    self._store.complete_job(
                        worker_id=self._worker_id,
                        job_id=job.job_id,
                        status="CANCELLED",
                    )
                    return job.job_id
                if job.revision_id is None:
                    raise DomainError("CONFLICT", "知识 Job 缺少 Revision")
                revision = self._store.revision_detail(job.revision_id)
                if revision.status != "APPROVED":
                    raise DomainError("CONFLICT", "Job 对应 Revision 当前不可构建")
                self._progress(job.job_id, "revision_validated", 1)
                result = build_and_evaluate(revision.spec, self._backend)
                heartbeat.check()
                self._progress(job.job_id, "knowledge_evaluated", 2)
                if len(result.resources) != 1:
                    raise DomainError("VALIDATION_ERROR", "当前 Revision 只能发布一个知识资源", status_code=422)
                if self._store.job_detail(job.job_id).status == "CANCEL_REQUESTED":
                    self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="CANCELLED")
                    return job.job_id
                resource = result.resources[0]
                storage_key = self._artifact_store.store_bundle(
                    agent_id=revision.agent_id,
                    revision_id=revision.revision_id,
                    bundle_checksum=result.bundle_checksum,
                    members=result.bundle_members,
                )
                heartbeat.check()
                self._progress(job.job_id, "bundle_published", 3)
                self._store.renew_job_lease(
                    worker_id=self._worker_id,
                    job_id=job.job_id,
                    lease_seconds=self._lease_seconds,
                )
            self._store.publish_revision_ready(worker_id=self._worker_id, job_id=job.job_id, revision_id=revision.revision_id, build_id=result.build_id, bundle_checksum=result.bundle_checksum, report_ref=result.report_ref, storage_key=storage_key, collection_name=resource.collection_name, collection_checksum=resource.collection_checksum, pass_rate=result.pass_rate)
        except DomainError as exc:
            self._finish_failed_job(job.job_id, exc.code)
        except KnowledgePipelineError as exc:
            self._finish_failed_job(job.job_id, exc.code)
        except Exception:
            logger.exception("knowledge job failed job_id=%s", job.job_id)
            self._finish_failed_job(job.job_id, "DEPENDENCY_UNAVAILABLE")
        return job.job_id

    def _progress(self, job_id: str, stage: str, current: int) -> None:
        self._store.record_job_progress(
            worker_id=self._worker_id,
            job_id=job_id,
            stage=stage,
            current=current,
            total=4,
        )

    def _finish_failed_job(self, job_id: str, error_code: str) -> None:
        """仅由仍持有 lease 的 Worker 收敛失败；失去 lease 时交给接管者恢复。"""

        status = self._store.job_detail(job_id).status
        if status in {"CANCELLED", "SUCCEEDED", "FAILED"}:
            return
        terminal_status = "CANCELLED" if status == "CANCEL_REQUESTED" else "FAILED"
        try:
            self._store.complete_job(
                worker_id=self._worker_id,
                job_id=job_id,
                status=terminal_status,
                error_code=None if terminal_status == "CANCELLED" else error_code,
            )
        except DomainError:
            logger.warning(
                "knowledge job completion skipped after lease loss job_id=%s",
                job_id,
            )


class _LeaseHeartbeat(AbstractContextManager["_LeaseHeartbeat"]):
    """在阻塞式构建期间续租，并把后台失败同步回 Worker 主流程。"""

    def __init__(
        self,
        *,
        store: KnowledgeJobStore,
        worker_id: str,
        job_id: str,
        lease_seconds: int,
    ) -> None:
        self._store = store
        self._worker_id = worker_id
        self._job_id = job_id
        self._lease_seconds = lease_seconds
        self._stop = Event()
        self._error: BaseException | None = None
        self._thread = Thread(target=self._run, name=f"lease-{job_id}", daemon=True)

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def check(self) -> None:
        """在任何不可逆步骤前传播续租失败。"""

        if self._error is not None:
            raise DomainError("WORKER_INTERRUPTED", "Knowledge Worker 已失去 Job lease") from self._error

    def _run(self) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                self._store.renew_job_lease(
                    worker_id=self._worker_id,
                    job_id=self._job_id,
                    lease_seconds=self._lease_seconds,
                )
            except BaseException as exc:  # 线程边界必须把未知失败交回主流程处理。
                self._error = exc
                self._stop.set()
                return


class ProfileProposalJobStore(KnowledgeJobStore, Protocol):
    """Profile Proposal Worker 需要的额外事实源操作。"""

    def profile_proposal_input(self, job_id: str): ...

    def publish_profile_proposal(self, *, worker_id: str, job_id: str, proposal: object) -> None: ...


class ProfileProposalJobWorker:
    """执行受限 LLM Proposal Job，Draft 漂移或 lease 丢失时不发布结果。"""

    def __init__(
        self,
        *,
        store: ProfileProposalJobStore,
        backend: ProfileProposalBackend,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        if not worker_id or lease_seconds < 3:
            raise ValueError("Profile Proposal Worker 配置无效")
        self._store = store
        self._backend = backend
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def run_once(self) -> str | None:
        job = self._store.claim_job(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            job_types=frozenset({"PROFILE_PROPOSAL"}),
        )
        if job is None:
            return None
        try:
            with _LeaseHeartbeat(store=self._store, worker_id=self._worker_id, job_id=job.job_id, lease_seconds=self._lease_seconds) as heartbeat:
                if self._store.job_detail(job.job_id).status == "CANCEL_REQUESTED":
                    self._store.complete_job(
                        worker_id=self._worker_id,
                        job_id=job.job_id,
                        status="CANCELLED",
                    )
                    return job.job_id
                proposal_input = self._store.profile_proposal_input(job.job_id)
                self._store.record_job_progress(worker_id=self._worker_id, job_id=job.job_id, stage="sources_validated", current=1, total=2)
                proposal = self._backend.propose(proposal_input)
                heartbeat.check()
                if self._store.job_detail(job.job_id).status == "CANCEL_REQUESTED":
                    self._store.complete_job(worker_id=self._worker_id, job_id=job.job_id, status="CANCELLED")
                    return job.job_id
                self._store.renew_job_lease(worker_id=self._worker_id, job_id=job.job_id, lease_seconds=self._lease_seconds)
            self._store.publish_profile_proposal(worker_id=self._worker_id, job_id=job.job_id, proposal=proposal)
        except DomainError as exc:
            self._finish(job.job_id, exc.code)
        except KnowledgePipelineError as exc:
            self._finish(job.job_id, exc.code)
        except Exception:
            logger.exception("profile proposal job failed job_id=%s", job.job_id)
            self._finish(job.job_id, "DEPENDENCY_UNAVAILABLE")
        return job.job_id

    def _finish(self, job_id: str, error_code: str) -> None:
        status = self._store.job_detail(job_id).status
        if status in {"CANCELLED", "SUCCEEDED", "FAILED"}:
            return
        terminal = "CANCELLED" if status == "CANCEL_REQUESTED" else "FAILED"
        try:
            self._store.complete_job(worker_id=self._worker_id, job_id=job_id, status=terminal, error_code=None if terminal == "CANCELLED" else error_code)
        except DomainError:
            logger.warning("profile proposal completion skipped after lease loss job_id=%s", job_id)
