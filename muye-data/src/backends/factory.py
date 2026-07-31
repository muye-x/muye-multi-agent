"""根据版本化配置构造数据库适配器。"""

from __future__ import annotations

from collections.abc import Mapping

from src.backends.base import RetrievalBackend
from src.backends.milvus import MilvusBackend
from src.config import (
    DataConfig,
    MilvusConnectionConfig,
    require_environment_value,
)


def build_backends(
    config: DataConfig,
    *,
    environ: Mapping[str, str] | None = None,
    connection_names: set[str] | None = None,
) -> dict[str, RetrievalBackend]:
    """为资源实际引用或调用方指定的已声明 connection 构造惰性客户端。"""
    backends: dict[str, RetrievalBackend] = {}
    referenced_connections = (
        {resource.connection for resource in config.resources.values()}
        if connection_names is None
        else set(connection_names)
    )
    unknown_connections = referenced_connections - set(config.connections)
    if unknown_connections:
        raise ValueError("请求构造未声明的数据库 connection")
    for name, connection in config.connections.items():
        if name not in referenced_connections:
            continue
        if isinstance(connection, MilvusConnectionConfig):
            token = (
                require_environment_value(connection.token_env, environ)
                if connection.token_env
                else None
            )
            backends[name] = MilvusBackend(
                uri=connection.uri,
                token=token,
                database=connection.database,
            )
        else:  # pragma: no cover - DataConfig 只允许 Milvus。
            raise TypeError(f"不支持的 connection 类型：{type(connection).__name__}")
    return backends
