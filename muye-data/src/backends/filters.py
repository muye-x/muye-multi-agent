"""受限过滤 AST 到 Milvus 表达式和 OpenSearch DSL 的转换。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from src.contracts import FilterExpression, ScalarValue
from src.errors import InvalidRequestError


def _physical_field(logical_name: str | None, fields: Mapping[str, str]) -> str:
    if logical_name is None or logical_name not in fields:
        raise InvalidRequestError("filter 包含未公开字段")
    return fields[logical_name]


def _milvus_scalar(value: ScalarValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def compile_milvus_filter(
    expression: FilterExpression | None,
    fields: Mapping[str, str],
) -> str:
    """将受限 AST 编译为 Milvus filter expression。"""
    if expression is None:
        return ""
    field_name = _physical_field(expression.field, fields) if expression.field else ""
    operator_map = {"eq": "==", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    if expression.op in operator_map:
        return f"{field_name} {operator_map[expression.op]} {_milvus_scalar(expression.value)}"
    if expression.op in {"in", "not_in"}:
        values = ", ".join(_milvus_scalar(value) for value in expression.values or [])
        operator = "in" if expression.op == "in" else "not in"
        return f"{field_name} {operator} [{values}]"
    if expression.op in {"and", "or"}:
        operator = " && " if expression.op == "and" else " || "
        parts = [compile_milvus_filter(child, fields) for child in expression.conditions or []]
        return "(" + operator.join(parts) + ")"
    return f"not ({compile_milvus_filter(expression.condition, fields)})"


def compile_opensearch_filter(
    expression: FilterExpression | None,
    fields: Mapping[str, str],
) -> dict[str, Any] | None:
    """将受限 AST 编译为 OpenSearch query DSL object。"""
    if expression is None:
        return None
    field_name = _physical_field(expression.field, fields) if expression.field else ""
    if expression.op == "eq":
        return {"term": {field_name: expression.value}}
    if expression.op == "ne":
        return {"bool": {"must_not": [{"term": {field_name: expression.value}}]}}
    if expression.op in {"gt", "gte", "lt", "lte"}:
        return {"range": {field_name: {expression.op: expression.value}}}
    if expression.op == "in":
        return {"terms": {field_name: expression.values}}
    if expression.op == "not_in":
        return {"bool": {"must_not": [{"terms": {field_name: expression.values}}]}}
    if expression.op in {"and", "or"}:
        compiled = [compile_opensearch_filter(child, fields) for child in expression.conditions or []]
        bool_key = "filter" if expression.op == "and" else "should"
        body: dict[str, Any] = {bool_key: compiled}
        if expression.op == "or":
            body["minimum_should_match"] = 1
        return {"bool": body}
    return {"bool": {"must_not": [compile_opensearch_filter(expression.condition, fields)]}}
