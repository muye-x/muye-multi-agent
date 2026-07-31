"""由已校验 Catalog Snapshot 构造的不可变 SubAgent Registry。"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.models import AgentCatalogEntryV1, AgentCatalogSnapshotV1


@dataclass(frozen=True, slots=True)
class SubAgentDescriptor:
    """Main 调用一个 ACTIVE SubAgent 所需的最小可信投影。"""

    name: str
    url: str
    timeout_seconds: float
    profile: str = "internal"
    agent_id: str = ""
    agent_version: str = "0.0.0"
    display_name: str = ""
    description: str = ""
    supported_intents: tuple[str, ...] = ()
    service_name: str = ""
    protocol_version: str = "muye-agent-internal/3.0"
    descriptor_checksum: str = ""
    source_tree_checksum: str = ""
    capabilities_checksum: str = ""
    catalog_revision: str = ""
    catalog_checksum: str = ""
    max_concurrency: int = 8

    def __post_init__(self) -> None:
        """为旧测试构造保留安全默认；Catalog 路径始终提供完整字段。"""
        if not self.agent_id:
            object.__setattr__(self, "agent_id", f"agent_{self.name.replace('-', '_')}")
        if not self.display_name:
            object.__setattr__(self, "display_name", self.name)
        if not self.description:
            object.__setattr__(self, "description", f"调用 {self.display_name} 完成其声明的任务。")
        if not self.service_name:
            object.__setattr__(self, "service_name", self.url.split("//", 1)[-1].split(":", 1)[0])

    @classmethod
    def from_catalog(
        cls,
        entry: AgentCatalogEntryV1,
        *,
        catalog_revision: str,
        catalog_checksum: str,
    ) -> "SubAgentDescriptor":
        """只接受 ACTIVE 且方向固定的 Catalog entry。"""
        if entry.status != "ACTIVE":
            raise ValueError(f"非 ACTIVE Agent 不能进入 Main Registry：{entry.agent_id}")
        if entry.caller != "agent-main" or entry.target_type != "sub_agent":
            raise ValueError(f"Catalog 调用方向无效：{entry.agent_id}")
        return cls(
            name=entry.tool_name,
            url=entry.base_url,
            timeout_seconds=float(entry.timeout_seconds),
            profile=entry.api_profile,
            agent_id=entry.agent_id,
            agent_version=entry.agent_version,
            display_name=entry.display_name,
            description=entry.description,
            supported_intents=tuple(entry.supported_intents),
            service_name=entry.service_name,
            protocol_version=entry.internal_protocol_version,
            descriptor_checksum=entry.descriptor_checksum,
            source_tree_checksum=entry.source_tree_checksum,
            capabilities_checksum=entry.capabilities_checksum,
            catalog_revision=catalog_revision,
            catalog_checksum=catalog_checksum,
            max_concurrency=entry.max_concurrency,
        )


class SubAgentRegistry:
    """工具名与 agent_id 均唯一的不可变 ACTIVE Registry。"""

    def __init__(self, descriptors: list[SubAgentDescriptor] | tuple[SubAgentDescriptor, ...]) -> None:
        by_name = {descriptor.name: descriptor for descriptor in descriptors}
        by_id = {descriptor.agent_id: descriptor for descriptor in descriptors}
        if len(by_name) != len(descriptors) or len(by_id) != len(descriptors):
            raise ValueError("SubAgent Registry 的 tool name 与 agent_id 必须唯一")
        self._descriptors = tuple(sorted(descriptors, key=lambda descriptor: descriptor.agent_id))
        self._by_name = by_name
        self._by_id = by_id

    @classmethod
    def from_snapshot(cls, snapshot: AgentCatalogSnapshotV1) -> "SubAgentRegistry":
        return cls(
            [
                SubAgentDescriptor.from_catalog(
                    entry,
                    catalog_revision=snapshot.catalog_revision,
                    catalog_checksum=snapshot.catalog_checksum,
                )
                for entry in snapshot.agents
                if entry.status == "ACTIVE"
            ]
        )

    def get(self, name: str) -> SubAgentDescriptor:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ValueError(f"未注册子 Agent 工具: {name}") from exc

    def get_by_agent_id(self, agent_id: str) -> SubAgentDescriptor:
        try:
            return self._by_id[agent_id]
        except KeyError as exc:
            raise ValueError(f"未注册子 Agent: {agent_id}") from exc

    def values(self) -> tuple[SubAgentDescriptor, ...]:
        return self._descriptors

    def select(self, agent_ids: set[str] | frozenset[str]) -> "SubAgentRegistry":
        return SubAgentRegistry([descriptor for descriptor in self._descriptors if descriptor.agent_id in agent_ids])


def build_default_registry() -> SubAgentRegistry:
    """阶段 5 默认允许空 Catalog 启动，不再从 Travel/Order URL 环境变量发现 Agent。"""
    return SubAgentRegistry([])
