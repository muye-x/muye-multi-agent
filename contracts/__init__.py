"""v2.0 跨服务公开契约。

这些模型是模板 Agent 生成、构建证明和 MainAgent Catalog 的共享边界。调用方应使用
同一组 fixture 验证 Python、JSON Schema 与后续 TypeScript DTO 的兼容性。
"""

from .models import (
    AgentBuildRecordV1,
    AgentCatalogEntryV1,
    AgentCatalogSnapshotV1,
    AgentDescriptorV1,
    AgentGenerationSpecV1,
    SourceProvenanceV1,
)

__all__ = [
    "AgentBuildRecordV1",
    "AgentCatalogEntryV1",
    "AgentCatalogSnapshotV1",
    "AgentDescriptorV1",
    "AgentGenerationSpecV1",
    "SourceProvenanceV1",
]
