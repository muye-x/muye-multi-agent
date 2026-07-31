"""Catalog 内容身份与 capabilities 身份的跨组件稳定算法。"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
import json

from .models import AgentCatalogEntryV1, AgentCatalogSnapshotV1


CATALOG_SCHEMA_VERSION = "muye.ai/agent-catalog-snapshot/v1"


def canonical_checksum(value: object) -> str:
    """对 JSON 兼容值计算不受空白和键顺序影响的 SHA-256。"""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def build_catalog_snapshot(entries: Sequence[AgentCatalogEntryV1]) -> AgentCatalogSnapshotV1:
    """按稳定身份排序构造无时间字段的 Catalog Snapshot。"""
    ordered = sorted(entries, key=lambda item: (item.agent_id, item.agent_version, item.tool_name))
    agents = [entry.model_dump(mode="json") for entry in ordered]
    input_checksum = canonical_checksum({"schema_version": CATALOG_SCHEMA_VERSION, "agents": agents})
    revision = f"catalog-{input_checksum[:24]}"
    checksum = canonical_checksum(
        {"schema_version": CATALOG_SCHEMA_VERSION, "catalog_revision": revision, "agents": agents}
    )
    return AgentCatalogSnapshotV1(
        schema_version=CATALOG_SCHEMA_VERSION,
        catalog_revision=revision,
        catalog_checksum=checksum,
        agents=ordered,
    )


def validate_catalog_snapshot_checksum(snapshot: AgentCatalogSnapshotV1) -> None:
    """重新计算 revision/checksum，拒绝内容被替换的 Catalog。"""
    expected = build_catalog_snapshot(snapshot.agents)
    if snapshot.catalog_revision != expected.catalog_revision:
        raise ValueError("Catalog revision 与 agents 内容不匹配")
    if snapshot.catalog_checksum != expected.catalog_checksum:
        raise ValueError("Catalog checksum 与 agents 内容不匹配")


def capabilities_identity_projection(
    *,
    agent_id: str,
    agent_version: str,
    descriptor_checksum: str,
    source_tree_checksum: str,
    internal_protocol_version: str,
) -> dict[str, object]:
    """提取 Control/Main 都能稳定复核的最小 capabilities 身份。"""
    return {
        "api_profiles": ["internal"],
        "identity": {
            "agent_id": agent_id,
            "agent_version": agent_version,
            "descriptor_checksum": descriptor_checksum,
            "source_tree_checksum": source_tree_checksum,
        },
        "internal_protocol_version": internal_protocol_version,
        "supports_streaming": True,
    }


def capabilities_identity_checksum(
    *,
    agent_id: str,
    agent_version: str,
    descriptor_checksum: str,
    source_tree_checksum: str,
    internal_protocol_version: str,
) -> str:
    """计算 Catalog 中不包含运行时噪声的 capabilities checksum。"""
    return canonical_checksum(
        capabilities_identity_projection(
            agent_id=agent_id,
            agent_version=agent_version,
            descriptor_checksum=descriptor_checksum,
            source_tree_checksum=source_tree_checksum,
            internal_protocol_version=internal_protocol_version,
        )
    )


def capabilities_checksum_from_response(value: object) -> str:
    """从真实 `/capabilities` 响应提取与 Catalog 相同的身份投影。"""
    if not isinstance(value, dict):
        raise ValueError("Agent capabilities 必须是 JSON 对象")
    identity = value.get("identity")
    profiles = value.get("api_profiles")
    if not isinstance(identity, dict) or not isinstance(profiles, list):
        raise ValueError("Agent capabilities 缺少 identity 或 api_profiles")
    fields = ("agent_id", "agent_version", "descriptor_checksum", "source_tree_checksum")
    if any(not isinstance(identity.get(field), str) for field in fields):
        raise ValueError("Agent capabilities identity 字段无效")
    protocol = value.get("internal_protocol_version")
    if not isinstance(protocol, str):
        raise ValueError("Agent capabilities 缺少 internal protocol")
    projection = capabilities_identity_projection(
        agent_id=identity["agent_id"],
        agent_version=identity["agent_version"],
        descriptor_checksum=identity["descriptor_checksum"],
        source_tree_checksum=identity["source_tree_checksum"],
        internal_protocol_version=protocol,
    )
    projection["api_profiles"] = sorted(item for item in profiles if isinstance(item, str))
    projection["supports_streaming"] = value.get("supports_streaming") is True
    return canonical_checksum(projection)
