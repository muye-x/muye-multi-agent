"""Control 的 active Catalog、grant 与 citation 授权领域逻辑。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol

from contracts.models import AgentCatalogSnapshotV1
from contracts.catalog import build_catalog_snapshot, canonical_checksum, validate_catalog_snapshot_checksum

from .health import AgentHealthCollector, CatalogCandidateError
from .models import (
    AgentObservationRequest,
    AgentObservationResponse,
    AuthorizationResolveResponse,
    CatalogCandidateRequest,
    CatalogCandidateResponse,
    CitationRecordRequest,
)


_USER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")
logger = logging.getLogger(__name__)


class GrantStore(Protocol):
    """阶段 5 授权存储协议；阶段 6 可用 PostgreSQL 实现替换文件实现。"""

    def allowed_agent_ids(self, user_id: str) -> frozenset[str]:
        """返回稳定 agent_id 集合；读取或校验失败必须抛错。"""


class FileGrantStore:
    """仅供本地 Control 的严格 JSON grant 投影，每次解析都重新读取以支持即时撤权。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def allowed_agent_ids(self, user_id: str) -> frozenset[str]:
        if _USER_PATTERN.fullmatch(user_id) is None:
            raise ValueError("user_id 格式无效")
        if self._path.is_symlink():
            raise ValueError("grant 文件不能是符号链接")
        if not self._path.exists():
            return frozenset()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("无法读取 grant 文件") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != "muye.ai/user-agent-grants/v1":
            raise ValueError("grant 文件 schema 无效")
        grants = payload.get("grants")
        if not isinstance(grants, list):
            raise ValueError("grant 文件缺少 grants 数组")
        pairs: set[tuple[str, str]] = set()
        for grant in grants:
            if not isinstance(grant, dict) or set(grant) != {"user_id", "agent_id"}:
                raise ValueError("grant 项字段无效")
            grant_user = grant.get("user_id")
            agent_id = grant.get("agent_id")
            if not isinstance(grant_user, str) or _USER_PATTERN.fullmatch(grant_user) is None:
                raise ValueError("grant user_id 格式无效")
            if not isinstance(agent_id, str) or re.fullmatch(r"agent_[a-z0-9][a-z0-9_-]{2,63}", agent_id) is None:
                raise ValueError("grant agent_id 格式无效")
            pair = (grant_user, agent_id)
            if pair in pairs:
                raise ValueError("grant 不能重复")
            pairs.add(pair)
        return frozenset(agent_id for grant_user, agent_id in pairs if grant_user == user_id)


@dataclass(frozen=True, slots=True)
class CitationRecord:
    """服务端保存的 citation 身份，客户端不能覆盖其中任一授权字段。"""

    citation_id: str
    user_id: str
    agent_id: str
    agent_version: str
    knowledge_version_id: str
    locator: dict[str, object]


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    fingerprint: str
    response: CatalogCandidateResponse


class CatalogProjection:
    """原子维护 active Catalog，并把 candidate 健康校验与幂等提交串行化。"""

    def __init__(
        self,
        *,
        health_collector: AgentHealthCollector,
        grant_store: GrantStore,
        active_path: Path | None = None,
    ) -> None:
        self._health_collector = health_collector
        self._grant_store = grant_store
        self._active_path = active_path
        self._active = self._load_active()
        self._lock = asyncio.Lock()
        self._idempotency: dict[str, _IdempotencyRecord] = {}
        self._acks: dict[tuple[str, str], bool] = {}
        self._pending: AgentCatalogSnapshotV1 | None = None
        self._citations: dict[str, CitationRecord] = {}
        self._snapshots: dict[tuple[str, str], AgentCatalogSnapshotV1] = {
            (self._active.catalog_revision, self._active.catalog_checksum): self._active
        }

    @property
    def active(self) -> AgentCatalogSnapshotV1:
        """返回不可变 Pydantic snapshot；切换只发生于加锁临界区。"""
        return self._active

    @property
    def catalog_for_main(self) -> AgentCatalogSnapshotV1:
        """Main 优先读取已验证 pending；授权仍只基于 active。"""
        return self._pending or self._active

    async def submit_candidate(self, request: CatalogCandidateRequest) -> CatalogCandidateResponse:
        """验证 checksum、并发前置条件和所有 Agent 后一次性激活 candidate。"""
        async with self._lock:
            fingerprint = canonical_checksum(
                {
                    "expected_active_checksum": request.expected_active_checksum,
                    "snapshot": request.snapshot.model_dump(mode="json"),
                }
            )
            previous = self._idempotency.get(request.idempotency_key)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ValueError("idempotency key 已绑定到不同 Catalog candidate")
                return previous.response
            if self._pending is not None:
                raise ValueError("已有 Catalog candidate 等待 Main ACK")
            if request.expected_active_checksum != self._active.catalog_checksum:
                raise ValueError("expected active Catalog checksum 不匹配")
            validate_catalog_snapshot_checksum(request.snapshot)
            pending = await self._health_collector.validate_candidate(request.snapshot)
            self._pending = pending
            response = CatalogCandidateResponse(
                status="PENDING_MAIN_ACK",
                catalog_revision=pending.catalog_revision,
                catalog_checksum=pending.catalog_checksum,
            )
            self._idempotency[request.idempotency_key] = _IdempotencyRecord(fingerprint, response)
            return response

    async def record_observation(self, request: AgentObservationRequest) -> AgentObservationResponse:
        """把 Collector 观察应用到确切 active identity，并原子发布状态变化。"""
        async with self._lock:
            if request.catalog_checksum != self._active.catalog_checksum:
                raise ValueError("健康观察的 Catalog checksum 已失效")
            entry = next((item for item in self._active.agents if item.agent_id == request.agent_id), None)
            if entry is None or entry.agent_version != request.agent_version:
                raise ValueError("健康观察的 Agent identity 不匹配")
            observation = self._health_collector.observe(
                agent_id=entry.agent_id,
                current_status=entry.status,
                healthy=request.healthy,
            )
            if observation.changed:
                entries = [
                    item.model_copy(update={"status": observation.status})
                    if item.agent_id == entry.agent_id
                    else item
                    for item in self._active.agents
                ]
                updated = build_catalog_snapshot(entries)
                self._persist_active(updated)
                self._active = updated
                self._snapshots[(updated.catalog_revision, updated.catalog_checksum)] = updated
            return AgentObservationResponse(
                agent_id=entry.agent_id,
                status=observation.status,
                changed=observation.changed,
                consecutive_successes=observation.consecutive_successes,
                consecutive_failures=observation.consecutive_failures,
                catalog_revision=self._active.catalog_revision,
                catalog_checksum=self._active.catalog_checksum,
            )

    async def collect_health_once(self) -> tuple[AgentObservationResponse, ...]:
        """主动探测当前可运行 Agent，并把结果送入同一个连续阈值状态机。

        探测期间 Catalog 可能被部署 ACK 或另一个状态变化替换；这类陈旧观察会被
        `record_observation` 的 checksum 前置条件拒绝，本轮跳过并留待下次采集。
        """
        identities = tuple(
            (entry.agent_id, entry.agent_version)
            for entry in self._active.agents
            if entry.status in {"ACTIVE", "DEGRADED"}
        )
        observations: list[AgentObservationResponse] = []
        for agent_id, agent_version in identities:
            snapshot = self._active
            entry = next(
                (
                    item
                    for item in snapshot.agents
                    if item.agent_id == agent_id
                    and item.agent_version == agent_version
                    and item.status in {"ACTIVE", "DEGRADED"}
                ),
                None,
            )
            if entry is None:
                continue
            try:
                await self._health_collector.probe(entry)
                healthy = True
                error_code = None
            except CatalogCandidateError as exc:
                # probe 的具体依赖异常只进入服务日志；Catalog 只保存稳定错误分类。
                healthy = False
                error_code = "DEPENDENCY_UNAVAILABLE"
                logger.warning(
                    "Agent health probe failed agent_id=%s error_type=%s",
                    agent_id,
                    type(exc).__name__,
                )
            try:
                observation = await self.record_observation(
                    AgentObservationRequest(
                        catalog_checksum=snapshot.catalog_checksum,
                        agent_id=agent_id,
                        agent_version=agent_version,
                        healthy=healthy,
                        error_code=error_code,
                    )
                )
            except ValueError:
                logger.info("Discarded stale Agent health observation agent_id=%s", agent_id)
                continue
            observations.append(observation)
        return tuple(observations)

    def resolve_authorization(self, user_id: str) -> AuthorizationResolveResponse:
        """按当前 active Snapshot 与最新 grant 做交集；grant 读取失败向上传播。"""
        allowed = self._grant_store.allowed_agent_ids(user_id)
        active_ids = {entry.agent_id for entry in self._active.agents if entry.status == "ACTIVE"}
        return AuthorizationResolveResponse(
            user_id=user_id,
            catalog_revision=self._active.catalog_revision,
            catalog_checksum=self._active.catalog_checksum,
            allowed_agent_ids=sorted(active_ids & allowed),
        )

    def record_ack(self, *, revision: str, checksum: str, accepted: bool) -> None:
        """接受 pending 后才切换 active；拒绝候选时旧 Catalog 保持不变。"""
        pending = self._pending
        if pending is not None and revision == pending.catalog_revision and checksum == pending.catalog_checksum:
            self._acks[(revision, checksum)] = accepted
            if accepted:
                self._persist_active(pending)
                self._active = pending
                self._snapshots[(pending.catalog_revision, pending.catalog_checksum)] = pending
            self._pending = None
            return
        if revision == self._active.catalog_revision and checksum == self._active.catalog_checksum and accepted:
            self._acks[(revision, checksum)] = True
            return
        raise ValueError("Main ACK 与 pending/active Catalog 不匹配")

    def is_acked(self, *, revision: str, checksum: str) -> bool:
        return self._acks.get((revision, checksum), False)

    def record_citation(self, request: CitationRecordRequest) -> CitationRecord:
        """只接受历史有效 Catalog 中匹配 Agent identity 的 Main 可信调用记录。"""
        snapshot = self._snapshots.get((request.catalog_revision, request.catalog_checksum))
        if snapshot is None:
            raise ValueError("citation 的 Catalog identity 不存在")
        entry = next((item for item in snapshot.agents if item.agent_id == request.agent_id), None)
        if entry is None or entry.agent_version != request.agent_version or entry.status != "ACTIVE":
            raise ValueError("citation 的 Agent identity 未在请求 Catalog 中激活")
        record = CitationRecord(
            citation_id=request.citation_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
            agent_version=request.agent_version,
            knowledge_version_id=request.knowledge_version_id,
            locator=request.locator.model_dump(mode="json"),
        )
        previous = self._citations.get(record.citation_id)
        if previous is not None and previous != record:
            raise ValueError("citation_id 已绑定到不同调用身份")
        self._citations[record.citation_id] = record
        return record

    def resolve_citation(self, *, citation_id: str, user_id: str) -> CitationRecord:
        """同时复核原调用用户、当前 grant 和 active Agent，再返回 locator。"""
        try:
            record = self._citations[citation_id]
        except KeyError as exc:
            raise ValueError("citation 不存在或不可访问") from exc
        allowed = self.resolve_authorization(user_id).allowed_agent_ids
        if record.user_id != user_id or record.agent_id not in allowed:
            raise ValueError("citation 不存在或不可访问")
        return record

    def _load_active(self) -> AgentCatalogSnapshotV1:
        if self._active_path is None or not self._active_path.exists():
            return build_catalog_snapshot([])
        if self._active_path.is_symlink() or not self._active_path.is_file():
            raise ValueError("active Catalog 文件必须是普通文件")
        try:
            payload = json.loads(self._active_path.read_text(encoding="utf-8"))
            snapshot = AgentCatalogSnapshotV1.model_validate(payload)
        except Exception as exc:
            raise ValueError("active Catalog 文件无效") from exc
        validate_catalog_snapshot_checksum(snapshot)
        return snapshot

    def _persist_active(self, snapshot: AgentCatalogSnapshotV1) -> None:
        if self._active_path is None:
            return
        if self._active_path.is_symlink():
            raise ValueError("active Catalog 文件不能是符号链接")
        self._active_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._active_path.name}.", suffix=".tmp", dir=self._active_path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temporary_path, self._active_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
