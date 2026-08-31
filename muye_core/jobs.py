"""阶段 2 可恢复 Job 的领域状态机。

状态转换与事件构造不依赖数据库。各仓储负责在事务内保存本模块返回的状态，以保证
Worker 崩溃后，过期 lease 可被重新领取而不会覆盖终态或重复事件序号。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from contracts.v3 import JobEventV1

from .service import DomainError


TERMINAL_STATUSES = frozenset({"CANCELLED", "SUCCEEDED", "FAILED"})


@dataclass(frozen=True, slots=True)
class JobRecord:
    """一个由 Revision 驱动的构建或评测作业。

    lease 仅代表当前 Worker 的写权限；它不会改变 Job 的业务输入。取消请求可以由
    任意管理员发起，但只能由持有 lease 的 Worker 收敛到 ``CANCELLED``。
    """

    job_id: str
    job_type: str
    revision_id: str
    idempotency_key: str
    status: str
    attempt: int
    lease_owner: str | None = None
    lease_until: datetime | None = None
    error_code: str | None = None


def claim(record: JobRecord, *, worker_id: str, now: datetime, lease_seconds: int) -> JobRecord:
    """领取待执行或租约已过期的 Job，终态及未过期租约均不可覆盖。"""

    if lease_seconds < 1:
        raise ValueError("lease_seconds 必须为正数")
    available = record.status == "PENDING" or (
        record.status in {"RUNNING", "CANCEL_REQUESTED"}
        and record.lease_until is not None
        and record.lease_until <= now
    )
    if not available:
        raise DomainError("CONFLICT", "Job 当前不可领取")
    return replace(record, status="RUNNING" if record.status != "CANCEL_REQUESTED" else "CANCEL_REQUESTED", lease_owner=worker_id, lease_until=now + timedelta(seconds=lease_seconds))


def request_cancel(record: JobRecord) -> JobRecord:
    """记录协作式取消请求，不允许修改任何已终态审计记录。"""

    if record.status in TERMINAL_STATUSES:
        raise DomainError("CONFLICT", "Job 已处于终态，不能取消")
    return replace(record, status="CANCEL_REQUESTED")


def complete(record: JobRecord, *, worker_id: str, status: str, error_code: str | None = None) -> JobRecord:
    """由持有当前 lease 的 Worker 写入唯一终态。"""

    if status not in TERMINAL_STATUSES:
        raise ValueError("Job 终态无效")
    if record.lease_owner != worker_id:
        raise DomainError("CONFLICT", "Worker 不持有 Job lease")
    if record.status in TERMINAL_STATUSES:
        raise DomainError("CONFLICT", "Job 已处于终态")
    if record.status == "CANCEL_REQUESTED" and status != "CANCELLED":
        raise DomainError("CONFLICT", "已请求取消的 Job 只能进入 CANCELLED")
    if status == "FAILED" and not error_code:
        raise ValueError("FAILED Job 必须包含 error_code")
    if status != "FAILED" and error_code is not None:
        raise ValueError("非 FAILED Job 不能包含 error_code")
    return replace(record, status=status, lease_owner=None, lease_until=None, error_code=error_code)


def new_event(
    *,
    job_id: str,
    sequence: int,
    event_type: str,
    stage: str,
    message: str | None = None,
    progress_current: int | None = None,
    progress_total: int | None = None,
    artifact_ref: str | None = None,
    error_code: str | None = None,
) -> JobEventV1:
    """构造受冻结契约验证的 JobEvent，拒绝任意日志或路径进入 SSE。"""

    payload: dict[str, object] = {
        "schema_version": "muye.ai/job-event/v1",
        "job_id": job_id,
        "sequence": sequence,
        "event_type": event_type,
        "emitted_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stage": stage,
    }
    for field, value in {
        "message": message,
        "progress_current": progress_current,
        "progress_total": progress_total,
        "artifact_ref": artifact_ref,
        "error_code": error_code,
    }.items():
        if value is not None:
            payload[field] = value
    return JobEventV1.model_validate(payload)
