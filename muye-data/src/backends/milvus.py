"""Milvus 只读检索适配器。

``pymilvus`` 在首次访问时才导入，因此契约测试和未启用 Milvus 的部署无需安装或
初始化该客户端。适配器不会创建、加载或修改 collection/index。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from typing import Any

from src.backends.base import (
    BackendCapabilities,
    BackendHit,
    BackendQuery,
    DenseBackendQuery,
    KeywordBackendQuery,
)
from src.backends.filters import compile_milvus_filter
from src.contracts import normalize_json_value
from src.errors import BackendProtocolError, BackendUnavailableError, ConfigurationError, DataServiceError


logger = logging.getLogger(__name__)

_TRANSIENT_ERROR_NAMES = {
    "AioRpcError",
    "FutureTimeoutError",
    "MilvusEmbeddedUnavailableException",
    "MilvusServerUnavailableException",
    "MilvusUnavailableException",
    "RpcError",
}


class MilvusBackend:
    """将 Milvus dense/BM25 search 映射为统一只读后端协议。"""

    def __init__(
        self,
        *,
        uri: str,
        token: str | None = None,
        database: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._uri = uri
        self._token = token
        self._database = database
        self._client = client
        self._client_lock = asyncio.Lock()

    @property
    def backend_type(self) -> str:
        return "milvus"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(dense=True, keyword=True)

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """只把明确的连接、RPC 与服务不可用错误归类为可恢复故障。"""
        return (
            isinstance(exc, (ConnectionError, OSError))
            or type(exc).__name__ in _TRANSIENT_ERROR_NAMES
        )

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from pymilvus import AsyncMilvusClient
            except ImportError as exc:
                raise ConfigurationError("Milvus 资源需要安装 pymilvus") from exc
            kwargs: dict[str, Any] = {"uri": self._uri}
            if self._token:
                kwargs["token"] = self._token
            if self._database:
                kwargs["db_name"] = self._database
            self._client = AsyncMilvusClient(**kwargs)
        return self._client

    @staticmethod
    async def _call(method: Any, **kwargs: Any) -> Any:
        result = method(**kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _output_fields(query: BackendQuery) -> list[str]:
        fields = [query.id_field, query.content_field, *query.returned_fields.values()]
        return list(dict.fromkeys(fields))

    @staticmethod
    def _normalize_hits(raw_result: Any, query: BackendQuery) -> list[BackendHit]:
        if not isinstance(raw_result, list):
            raise BackendProtocolError()
        raw_hits = raw_result[0] if raw_result and isinstance(raw_result[0], list) else raw_result
        normalized: list[BackendHit] = []
        for raw_hit in raw_hits:
            if isinstance(raw_hit, dict):
                entity = raw_hit.get("entity") or {}
                raw_id = entity.get(query.id_field, raw_hit.get("id"))
                raw_content = entity.get(query.content_field)
                raw_score = raw_hit.get("distance", raw_hit.get("score"))
            else:
                entity = getattr(raw_hit, "entity", None) or {}
                raw_id = entity.get(query.id_field, getattr(raw_hit, "id", None))
                raw_content = entity.get(query.content_field)
                raw_score = getattr(raw_hit, "distance", getattr(raw_hit, "score", None))
            if raw_id is None or raw_content is None or raw_score is None:
                raise BackendProtocolError()
            try:
                score = float(raw_score)
            except (TypeError, ValueError) as exc:
                raise BackendProtocolError() from exc
            if not math.isfinite(score):
                raise BackendProtocolError()
            if isinstance(query, DenseBackendQuery) and query.metric_type == "L2":
                score = -score
            fields = {
                logical_name: normalize_json_value(entity[physical_name])
                for logical_name, physical_name in query.returned_fields.items()
                if physical_name in entity
            }
            normalized.append(
                BackendHit(
                    id=str(raw_id),
                    content=str(raw_content),
                    score=score,
                    fields=fields,
                )
            )
        return normalized

    async def search(self, query: BackendQuery) -> list[BackendHit]:
        """执行 Milvus search；keyword 字段必须由外部项目预建 BM25/sparse 索引。"""
        client = await self._get_client()
        expression = compile_milvus_filter(query.filter, query.filterable_fields)
        common: dict[str, Any] = {
            "collection_name": query.target,
            "limit": query.top_k,
            "output_fields": self._output_fields(query),
            "timeout": query.timeout_seconds,
        }
        if expression:
            common["filter"] = expression
        if isinstance(query, DenseBackendQuery):
            common.update(
                data=[list(query.vector)],
                anns_field=query.vector_field,
                search_params={"metric_type": query.metric_type, "params": {}},
            )
        elif isinstance(query, KeywordBackendQuery):
            common.update(
                data=[query.text],
                anns_field=query.keyword_field,
                search_params={"metric_type": "BM25", "params": {}},
            )
        else:
            raise TypeError(f"不支持的 Milvus 查询类型：{type(query).__name__}")

        try:
            result = await self._call(client.search, **common)
            return self._normalize_hits(result, query)
        except asyncio.CancelledError:
            raise
        except DataServiceError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise BackendUnavailableError() from exc
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning("Milvus search failed error_type=%s error=%s", error_type, exc)
            if self._is_transient_error(exc):
                raise BackendUnavailableError() from exc
            raise BackendProtocolError() from exc

    async def health(self, target: str, *, timeout_seconds: float) -> bool:
        """通过 describe_collection 只读检查目标，不自动加载 collection。"""
        try:
            client = await self._get_client()
            await asyncio.wait_for(
                self._call(client.describe_collection, collection_name=target),
                timeout=timeout_seconds,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
