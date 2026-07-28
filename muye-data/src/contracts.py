"""muye-data 的公共 HTTP 契约与过滤表达式。

过滤器使用受限 AST，而不是数据库原生查询字符串。调用方只能引用资源配置公开的
逻辑字段；物理字段映射和后端语法转换均在服务内部完成。
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


RESOURCE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
TRACE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
FIELD_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$"
FILTER_OPERATORS = (
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "and",
    "or",
    "not",
)
MAX_FILTER_DEPTH = 8
MAX_FILTER_CONDITIONS = 50
MAX_FILTER_SET_VALUES = 100

ScalarValue = str | int | float | bool


class StrictModel(BaseModel):
    """拒绝未知字段和隐式类型转换的公共模型基类。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class FilterExpression(StrictModel):
    """结构化过滤表达式。

    ``eq/ne/gt/gte/lt/lte`` 使用 ``field`` 与 ``value``；``in/not_in`` 使用
    ``field`` 与 ``values``；``and/or`` 使用 ``conditions``；``not`` 使用
    ``condition``。模型校验保证每个操作符只能携带对应字段。
    """

    op: Literal[
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "and",
        "or",
        "not",
    ]
    field: str | None = Field(default=None, pattern=FIELD_NAME_PATTERN)
    value: ScalarValue | None = None
    values: list[ScalarValue] | None = Field(default=None, min_length=1, max_length=MAX_FILTER_SET_VALUES)
    conditions: list["FilterExpression"] | None = Field(default=None, min_length=1)
    condition: "FilterExpression | None" = None

    @model_validator(mode="after")
    def validate_shape(self) -> "FilterExpression":
        """按操作符拒绝歧义字段组合，避免适配器猜测调用方意图。"""
        comparison = self.op in {"eq", "ne", "gt", "gte", "lt", "lte"}
        membership = self.op in {"in", "not_in"}
        logical = self.op in {"and", "or"}

        if comparison:
            valid = self.field is not None and self.value is not None
            valid = (
                valid
                and self.values is None
                and self.conditions is None
                and self.condition is None
            )
        elif membership:
            valid = self.field is not None and self.values is not None
            valid = (
                valid
                and self.value is None
                and self.conditions is None
                and self.condition is None
            )
        elif logical:
            valid = self.conditions is not None
            valid = (
                valid
                and self.field is None
                and self.value is None
                and self.values is None
                and self.condition is None
            )
        else:
            valid = self.condition is not None
            valid = (
                valid
                and self.field is None
                and self.value is None
                and self.values is None
                and self.conditions is None
            )

        if not valid:
            raise ValueError(f"过滤操作符 {self.op!r} 的字段组合无效")
        scalar_values = [self.value] if self.value is not None else list(self.values or [])
        for scalar in scalar_values:
            if isinstance(scalar, float) and not math.isfinite(scalar):
                raise ValueError("filter 数值必须是有限值")
            if isinstance(scalar, str) and len(scalar) > 4096:
                raise ValueError("filter 字符串不能超过 4096 字符")
        return self


def _filter_size(expression: FilterExpression, depth: int = 1) -> tuple[int, int]:
    """返回过滤 AST 的最大深度与节点数。"""
    children: list[FilterExpression] = []
    if expression.conditions:
        children.extend(expression.conditions)
    if expression.condition:
        children.append(expression.condition)
    if not children:
        return depth, 1

    child_stats = [_filter_size(child, depth + 1) for child in children]
    return max(item[0] for item in child_stats), 1 + sum(item[1] for item in child_stats)


def iter_filter_fields(expression: FilterExpression | None) -> set[str]:
    """提取 AST 中的逻辑字段名，供资源 allowlist 校验使用。"""
    if expression is None:
        return set()
    fields = {expression.field} if expression.field else set()
    for child in expression.conditions or []:
        fields.update(iter_filter_fields(child))
    if expression.condition:
        fields.update(iter_filter_fields(expression.condition))
    return fields


class RetrieveRequest(StrictModel):
    """完整召回请求；不会接受物理库表名或数据库查询字符串。"""

    resource: str = Field(pattern=RESOURCE_NAME_PATTERN)
    query: str = Field(min_length=1, max_length=8000)
    top_k: int = Field(default=5, ge=1, le=100)
    pipeline: str | None = Field(default=None, pattern=RESOURCE_NAME_PATTERN)
    filter: FilterExpression | None = None
    return_fields: list[str] | None = Field(default=None, max_length=50)
    trace_id: str = Field(default="", max_length=128)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """拒绝纯空白查询，同时保留正文中的有效空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        """限制 trace ID 字符集，避免日志注入。"""
        normalized = value.strip()
        if normalized and re.fullmatch(TRACE_ID_PATTERN, normalized) is None:
            raise ValueError("trace_id 格式无效")
        return normalized

    @field_validator("return_fields")
    @classmethod
    def validate_return_fields(cls, value: list[str] | None) -> list[str] | None:
        """规范化返回字段并拒绝重复或非法逻辑名。"""
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("return_fields 不能重复")
        for field_name in value:
            if re.fullmatch(FIELD_NAME_PATTERN, field_name) is None:
                raise ValueError("return_fields 包含非法字段名")
        return value

    @model_validator(mode="after")
    def validate_filter_budget(self) -> "RetrieveRequest":
        """限制递归过滤器规模，防止构造高成本数据库查询。"""
        if self.filter is None:
            return self
        depth, count = _filter_size(self.filter)
        if depth > MAX_FILTER_DEPTH:
            raise ValueError(f"filter 最大嵌套深度为 {MAX_FILTER_DEPTH}")
        if count > MAX_FILTER_CONDITIONS:
            raise ValueError(f"filter 最多包含 {MAX_FILTER_CONDITIONS} 个节点")
        return self

    def resolved_trace_id(self) -> str:
        """返回调用方 trace ID；未提供时生成不可预测的本地关联 ID。"""
        return self.trace_id or uuid.uuid4().hex


class RetrievalHit(StrictModel):
    """统一召回结果；score 仅在当前响应内保证越高越相关。"""

    id: str
    content: str
    score: float
    fields: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("score")
    @classmethod
    def validate_finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score 必须是有限数值")
        return value


class RetrievalResponse(StrictModel):
    """完整召回响应。"""

    resource: str
    pipeline: str
    trace_id: str
    took_ms: int = Field(ge=0)
    partial: bool
    warnings: list[str]
    hits: list[RetrievalHit]


class PipelineCapability(StrictModel):
    """资源公开的单个 pipeline 能力，不包含物理库表信息。"""

    name: str
    type: Literal["dense", "keyword", "hybrid"]
    rerank: bool


class ResourceCapabilities(StrictModel):
    """资源的可选 pipeline、逻辑字段和公共限制。"""

    resource: str
    default_pipeline: str
    pipelines: list[PipelineCapability]
    returnable_fields: list[str]
    filterable_fields: list[str]
    filter_operators: list[str]
    max_top_k: int


class ErrorResponse(StrictModel):
    """所有 HTTP 错误的稳定公开结构。"""

    error_code: str
    message: str
    recoverable: bool
    trace_id: str


class HealthResponse(StrictModel):
    """不探测外部依赖的进程存活响应。"""

    status: Literal["ok"]
    service: Literal["muye-data"]
    uptime: float = Field(ge=0, allow_inf_nan=False)


class ResourceReadiness(StrictModel):
    """单个逻辑资源的数据库与模型能力就绪状态。"""

    status: Literal["ready", "degraded", "unavailable"]
    backend: Literal["ready", "unavailable"]
    llm: Literal["ready", "degraded", "unavailable"]


class ReadinessResponse(StrictModel):
    """服务依赖就绪报告，不包含物理目标或数据库连接信息。"""

    status: Literal["ready", "degraded", "not_ready"]
    service: Literal["muye-data"]
    resources: dict[str, ResourceReadiness]


def normalize_json_value(value: Any) -> JsonValue:
    """将常见数据库标量安全转换为 JSON 值。

    未知对象转换为字符串，避免 FastAPI 序列化异常；适配器只会对资源 allowlist
    中的字段执行该转换。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json_value(item) for item in value]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)
