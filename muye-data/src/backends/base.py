"""数据库无关的只读检索后端协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias, runtime_checkable

from pydantic import JsonValue

from src.contracts import FilterExpression


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """适配器自身支持的检索通道。"""

    dense: bool
    keyword: bool
    filters: bool = True


@dataclass(frozen=True, slots=True)
class BackendHit:
    """适配器返回的统一候选项，字段名均为公共逻辑名。"""

    id: str
    content: str
    score: float
    fields: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DenseBackendQuery:
    """一次稠密向量查询所需的完整只读参数。"""

    target: str
    id_field: str
    content_field: str
    vector_field: str
    vector: tuple[float, ...]
    top_k: int
    returned_fields: Mapping[str, str]
    filterable_fields: Mapping[str, str]
    filter: FilterExpression | None
    metric_type: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class KeywordBackendQuery:
    """一次关键词/BM25 查询所需的完整只读参数。"""

    target: str
    id_field: str
    content_field: str
    keyword_field: str
    text: str
    top_k: int
    returned_fields: Mapping[str, str]
    filterable_fields: Mapping[str, str]
    filter: FilterExpression | None
    timeout_seconds: float


BackendQuery: TypeAlias = DenseBackendQuery | KeywordBackendQuery


@runtime_checkable
class RetrievalBackend(Protocol):
    """数据库适配器的完整边界。

    协议刻意不包含 create、insert、upsert、update 或 delete。生产数据库账号还应
    配置为只读，从接口与权限两层约束服务职责。
    """

    @property
    def backend_type(self) -> str:
        """返回非敏感数据库类型标识。"""

    @property
    def capabilities(self) -> BackendCapabilities:
        """返回适配器支持的只读查询能力。"""

    async def search(self, query: BackendQuery) -> list[BackendHit]:
        """执行一次数据库查询并返回统一候选项。"""

    async def health(self, target: str, *, timeout_seconds: float) -> bool:
        """只读检查目标是否存在且可访问。"""

    async def aclose(self) -> None:
        """关闭适配器持有的连接资源。"""
