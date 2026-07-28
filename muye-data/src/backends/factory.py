"""根据版本化配置构造数据库适配器。"""

from __future__ import annotations

from collections.abc import Mapping

from src.backends.base import RetrievalBackend
from src.backends.milvus import MilvusBackend
from src.backends.opensearch import OpenSearchBackend
from src.config import (
    DataConfig,
    MilvusConnectionConfig,
    OpenSearchConnectionConfig,
    require_environment_value,
)


def build_backends(
    config: DataConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, RetrievalBackend]:
    """仅为资源实际引用的 connection 构造惰性客户端。"""
    backends: dict[str, RetrievalBackend] = {}
    referenced_connections = {resource.connection for resource in config.resources.values()}
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
        elif isinstance(connection, OpenSearchConnectionConfig):
            username = (
                require_environment_value(connection.username_env, environ)
                if connection.username_env
                else None
            )
            password = (
                require_environment_value(connection.password_env, environ)
                if connection.password_env
                else None
            )
            backends[name] = OpenSearchBackend(
                hosts=connection.hosts,
                username=username,
                password=password,
                verify_certs=connection.verify_certs,
                ca_certs=connection.ca_certs,
            )
        else:  # pragma: no cover - Pydantic discriminator makes this unreachable.
            raise TypeError(f"不支持的 connection 类型：{type(connection).__name__}")
    return backends
