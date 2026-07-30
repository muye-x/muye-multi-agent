"""可持久化、可取消和可重试的本地 Knowledge Worker Job 状态机。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

from contracts.models import KnowledgeJobV1

from .checksums import canonical_checksum
from .io import load_json_model, write_json_atomic


class JobStore:
    """将 Job 记录保存在工作区受控 artifact 根下。

    这是本地 Worker 的最小持久层：写入采用原子 replace，作业运行时反复读取状态以
    响应其他 CLI 进程的取消请求；它不声称提供多机队列或 lease 语义。
    """

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root / "config" / "generated" / "knowledge-jobs"

    def create(self, *, kind: str, knowledge_slug: str, input_checksum: str, attempt: int = 1) -> KnowledgeJobV1:
        """创建 QUEUED Job；随机 ID 不携带配置、路径或密钥。"""
        now = _timestamp()
        job = KnowledgeJobV1(
            schema_version="muye.ai/knowledge-job/v1",
            job_id=f"job_{uuid.uuid4().hex}",
            kind=kind,
            knowledge_slug=knowledge_slug,
            status="QUEUED",
            attempt=attempt,
            created_at=now,
            updated_at=now,
            input_checksum=input_checksum,
        )
        self.save(job)
        return job

    def load(self, job_id: str) -> KnowledgeJobV1:
        """读取指定 Job；命令参数只能是 Job ID，不能选择任意文件。"""
        path = self._path(job_id)
        if not path.is_file():
            raise ValueError(f"知识 Job 不存在：{job_id}")
        return load_json_model(path, KnowledgeJobV1)

    def save(self, job: KnowledgeJobV1) -> KnowledgeJobV1:
        """原子持久化 Job 的当前状态。"""
        write_json_atomic(self._path(job.job_id), job.model_dump(mode="json"))
        return job

    def transition(
        self,
        job_id: str,
        *,
        status: str,
        report_ref: str | None = None,
        error_code: str | None = None,
    ) -> KnowledgeJobV1:
        """执行有限状态转换；终态不能被后续 CLI 覆盖。"""
        current = self.load(job_id)
        allowed = {
            "QUEUED": {"RUNNING", "CANCELLED"},
            "RUNNING": {"SUCCEEDED", "FAILED", "CANCELLED"},
            "FAILED": set(),
            "SUCCEEDED": set(),
            "CANCELLED": set(),
        }
        if status not in allowed[current.status]:
            raise ValueError(f"知识 Job 不能从 {current.status} 迁移到 {status}")
        updated = current.model_copy(
            update={
                "status": status,
                "updated_at": _timestamp(),
                "report_ref": report_ref,
                "error_code": error_code,
            }
        )
        return self.save(updated)

    def cancel(self, job_id: str) -> KnowledgeJobV1:
        """请求协作式取消；已经完成的 Job 明确拒绝改变审计结果。"""
        current = self.load(job_id)
        if current.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ValueError(f"知识 Job 已处于终态，不能取消：{current.status}")
        return self.transition(job_id, status="CANCELLED")

    def retry(self, job_id: str) -> KnowledgeJobV1:
        """根据失败/取消 Job 创建新 Job，保留原记录而不改写其审计历史。"""
        previous = self.load(job_id)
        if previous.status not in {"FAILED", "CANCELLED"}:
            raise ValueError("只有 FAILED 或 CANCELLED Job 可以重试")
        return self.create(
            kind=previous.kind,
            knowledge_slug=previous.knowledge_slug,
            input_checksum=previous.input_checksum,
            attempt=previous.attempt + 1,
        )

    def is_cancelled(self, job_id: str) -> bool:
        """供 Worker 在可中断步骤间确认是否收到取消请求。"""
        return self.load(job_id).status == "CANCELLED"

    def _path(self, job_id: str) -> Path:
        """将已由模型校验的 Job ID 映射到固定 artifact 根。"""
        if not job_id.startswith("job_") or len(job_id) != 36:
            raise ValueError("知识 Job ID 格式无效")
        return self._root / f"{job_id}.json"


def compute_job_input_checksum(*, kind: str, knowledge_slug: str, payload: object) -> str:
    """将 Job 精确绑定到命令类型、slug 与输入 artifact，供 retry 审计。"""
    return canonical_checksum({"kind": kind, "knowledge_slug": knowledge_slug, "payload": payload})


def _timestamp() -> str:
    """生成秒精度 RFC 3339 审计时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
