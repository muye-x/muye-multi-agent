"""阶段 2 Draft 变更影响分析。

该模块只比较不可变 Revision 与当前 Draft 的逻辑输入，不读取 Artifact、Milvus 或
外部模型。结果供 UI 在冻结新 Revision 前解释知识复用范围，最终构建仍以服务端
冻结的 Revision 为准。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .service import RevisionRecord


@dataclass(frozen=True, slots=True)
class DraftImpact:
    """当前 Draft 相对最新 Revision 的可复用范围。"""

    mode: str
    base_revision_id: str | None
    added_asset_ids: tuple[str, ...]
    removed_asset_ids: tuple[str, ...]
    reusable_asset_ids: tuple[str, ...]
    evaluation_required: bool
    reasons: tuple[str, ...]


def analyze_draft_impact(
    *,
    draft_config: Mapping[str, object],
    draft_asset_ids: Sequence[str],
    base_revision: RevisionRecord | None,
) -> DraftImpact:
    """区分完全复用、仅追加资料和必须全量重建三种影响。"""

    current_assets = set(draft_asset_ids)
    if base_revision is None:
        return DraftImpact(
            mode="FULL_REBUILD",
            base_revision_id=None,
            added_asset_ids=tuple(sorted(current_assets)),
            removed_asset_ids=(),
            reusable_asset_ids=(),
            evaluation_required=True,
            reasons=("INITIAL_REVISION",),
        )

    base_assets = {asset.asset_id for asset in base_revision.spec.source_assets}
    added = current_assets - base_assets
    removed = base_assets - current_assets
    reusable = current_assets & base_assets
    base_config = base_revision.spec.model_dump(
        mode="json",
        exclude={"schema_version", "agent_id", "revision_id", "revision_number", "source_assets"},
    )
    changed_fields = {
        field
        for field in sorted(set(base_config) | set(draft_config))
        if base_config.get(field) != draft_config.get(field)
    }
    knowledge_fields = {"model", "retrieval"}
    reasons: list[str] = []
    if removed:
        reasons.append("SOURCE_REMOVED")
    if changed_fields & knowledge_fields:
        reasons.append("KNOWLEDGE_CONFIG_CHANGED")
    if added:
        reasons.append("SOURCE_ADDED")
    if changed_fields - knowledge_fields:
        reasons.append("AGENT_CONFIG_CHANGED")

    if removed or changed_fields & knowledge_fields:
        mode = "FULL_REBUILD"
        reusable = set()
    elif added:
        mode = "INCREMENTAL"
    else:
        mode = "REUSE"
    return DraftImpact(
        mode=mode,
        base_revision_id=base_revision.revision_id,
        added_asset_ids=tuple(sorted(added)),
        removed_asset_ids=tuple(sorted(removed)),
        reusable_asset_ids=tuple(sorted(reusable)),
        evaluation_required=bool(added or removed or changed_fields),
        reasons=tuple(reasons or ("NO_LOGICAL_CHANGE",)),
    )
