"""过滤编译与 RRF 纯逻辑测试。"""

from __future__ import annotations

import pytest

from src.backends.base import BackendHit
from src.backends.filters import compile_milvus_filter, compile_opensearch_filter
from src.contracts import FilterExpression
from src.errors import InvalidRequestError
from src.retrieval.fusion import rank_single_channel, weighted_rrf


def _filter() -> FilterExpression:
    return FilterExpression.model_validate(
        {
            "op": "and",
            "conditions": [
                {"op": "eq", "field": "enabled", "value": True},
                {"op": "in", "field": "category", "values": ["faq", "policy"]},
            ],
        }
    )


def test_milvus_filter_uses_only_mapped_physical_fields() -> None:
    expression = compile_milvus_filter(
        _filter(),
        {"enabled": "is_enabled", "category": "document_category"},
    )

    assert expression == '(is_enabled == true && document_category in ["faq", "policy"])'


def test_opensearch_filter_builds_structured_dsl() -> None:
    expression = compile_opensearch_filter(
        _filter(),
        {"enabled": "metadata.enabled", "category": "metadata.category"},
    )

    assert expression == {
        "bool": {
            "filter": [
                {"term": {"metadata.enabled": True}},
                {"terms": {"metadata.category": ["faq", "policy"]}},
            ]
        }
    }


def test_filter_rejects_non_allowlisted_logical_field() -> None:
    expression = FilterExpression(op="eq", field="tenant", value="a")

    with pytest.raises(InvalidRequestError):
        compile_milvus_filter(expression, {"category": "category"})


def test_single_channel_deduplicates_and_sorts_stably() -> None:
    hits = [
        BackendHit("b", "B", 0.5),
        BackendHit("a", "A", 0.5),
        BackendHit("a", "A newer", 0.8),
    ]

    ranked = rank_single_channel(hits)

    assert [(item.id, item.score) for item in ranked] == [("a", 0.8), ("b", 0.5)]


def test_weighted_rrf_merges_ids_and_fields() -> None:
    dense = [
        BackendHit("a", "A", 0.9, {"title": "A"}),
        BackendHit("b", "B", 0.8),
    ]
    keyword = [
        BackendHit("b", "B", 5.0, {"source": "faq"}),
        BackendHit("a", "A", 4.0),
    ]

    fused = weighted_rrf([(dense, 1.0), (keyword, 1.0)], rank_constant=60)

    assert [item.id for item in fused] == ["a", "b"]
    assert fused[1].fields == {"source": "faq"}
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)

