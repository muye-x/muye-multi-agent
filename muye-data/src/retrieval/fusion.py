"""数据库无关的候选去重、稳定排序与加权 RRF。"""

from __future__ import annotations

from collections.abc import Sequence

from src.backends.base import BackendHit


def rank_single_channel(hits: Sequence[BackendHit]) -> list[BackendHit]:
    """按 ID 去重后以 score 降序、ID 升序稳定排序。"""
    by_id: dict[str, BackendHit] = {}
    for hit in hits:
        previous = by_id.get(hit.id)
        if previous is None or hit.score > previous.score:
            by_id[hit.id] = hit
        elif previous.score == hit.score:
            merged = dict(previous.fields)
            merged.update({key: value for key, value in hit.fields.items() if key not in merged})
            by_id[hit.id] = BackendHit(previous.id, previous.content, previous.score, merged)
    return sorted(by_id.values(), key=lambda item: (-item.score, item.id))


def weighted_rrf(
    channels: Sequence[tuple[Sequence[BackendHit], float]],
    *,
    rank_constant: int,
) -> list[BackendHit]:
    """融合多个有序通道；每个通道内重复 ID 只计入首次排名。"""
    scores: dict[str, float] = {}
    representatives: dict[str, BackendHit] = {}
    for raw_hits, weight in channels:
        ordered_hits = rank_single_channel(raw_hits)
        for rank, hit in enumerate(ordered_hits, start=1):
            scores[hit.id] = scores.get(hit.id, 0.0) + weight / (rank_constant + rank)
            if hit.id not in representatives:
                representatives[hit.id] = hit
            else:
                previous = representatives[hit.id]
                merged_fields = dict(previous.fields)
                merged_fields.update(
                    {key: value for key, value in hit.fields.items() if key not in merged_fields}
                )
                representatives[hit.id] = BackendHit(
                    id=previous.id,
                    content=previous.content or hit.content,
                    score=previous.score,
                    fields=merged_fields,
                )

    fused = [
        BackendHit(
            id=representatives[item_id].id,
            content=representatives[item_id].content,
            score=score,
            fields=representatives[item_id].fields,
        )
        for item_id, score in scores.items()
    ]
    return sorted(fused, key=lambda item: (-item.score, item.id))

