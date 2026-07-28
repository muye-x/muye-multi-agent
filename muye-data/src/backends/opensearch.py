"""OpenSearch 只读 dense/keyword 检索适配器。"""

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
from src.backends.filters import compile_opensearch_filter
from src.contracts import normalize_json_value
from src.errors import BackendProtocolError, BackendUnavailableError, ConfigurationError, DataServiceError


logger = logging.getLogger(__name__)


def _source_value(source: dict[str, Any], physical_name: str) -> Any:
    """读取 OpenSearch `_source` 的点分隔字段路径。"""
    current: Any = source
    for segment in physical_name.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


class OpenSearchBackend:
    """将 OpenSearch k-NN 与 match 查询映射为统一只读后端协议。"""

    def __init__(
        self,
        *,
        hosts: list[str],
        username: str | None = None,
        password: str | None = None,
        verify_certs: bool = True,
        ca_certs: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._hosts = list(hosts)
        self._username = username
        self._password = password
        self._verify_certs = verify_certs
        self._ca_certs = ca_certs
        self._client = client
        self._client_lock = asyncio.Lock()

    @property
    def backend_type(self) -> str:
        return "opensearch"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(dense=True, keyword=True)

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from opensearchpy import AsyncOpenSearch
            except ImportError as exc:
                raise ConfigurationError("OpenSearch 资源需要安装 opensearch-py") from exc
            kwargs: dict[str, Any] = {
                "hosts": self._hosts,
                "verify_certs": self._verify_certs,
            }
            if self._username is not None and self._password is not None:
                kwargs["http_auth"] = (self._username, self._password)
            if self._ca_certs:
                kwargs["ca_certs"] = self._ca_certs
            self._client = AsyncOpenSearch(**kwargs)
        return self._client

    @staticmethod
    async def _call(method: Any, **kwargs: Any) -> Any:
        result = method(**kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _source_fields(query: BackendQuery) -> list[str]:
        fields = [query.id_field, query.content_field, *query.returned_fields.values()]
        return list(dict.fromkeys(fields))

    @staticmethod
    def _normalize_hits(raw_result: Any, query: BackendQuery) -> list[BackendHit]:
        try:
            raw_hits = raw_result["hits"]["hits"]
        except (KeyError, TypeError) as exc:
            raise BackendProtocolError() from exc
        if not isinstance(raw_hits, list):
            raise BackendProtocolError()

        normalized: list[BackendHit] = []
        for raw_hit in raw_hits:
            if not isinstance(raw_hit, dict) or not isinstance(raw_hit.get("_source"), dict):
                raise BackendProtocolError()
            source = raw_hit["_source"]
            raw_id = _source_value(source, query.id_field)
            if raw_id is None:
                raw_id = raw_hit.get("_id")
            raw_content = _source_value(source, query.content_field)
            raw_score = raw_hit.get("_score")
            if raw_id is None or raw_content is None or raw_score is None:
                raise BackendProtocolError()
            try:
                score = float(raw_score)
            except (TypeError, ValueError) as exc:
                raise BackendProtocolError() from exc
            if not math.isfinite(score):
                raise BackendProtocolError()
            fields: dict[str, Any] = {}
            for logical_name, physical_name in query.returned_fields.items():
                raw_value = _source_value(source, physical_name)
                if raw_value is not None:
                    fields[logical_name] = normalize_json_value(raw_value)
            normalized.append(
                BackendHit(
                    id=str(raw_id),
                    content=str(raw_content),
                    score=score,
                    fields=fields,
                )
            )
        return normalized

    @staticmethod
    def _build_body(query: BackendQuery) -> dict[str, Any]:
        filter_query = compile_opensearch_filter(query.filter, query.filterable_fields)
        if isinstance(query, DenseBackendQuery):
            knn_body: dict[str, Any] = {
                "vector": list(query.vector),
                "k": query.top_k,
            }
            if filter_query is not None:
                knn_body["filter"] = filter_query
            query_body: dict[str, Any] = {"knn": {query.vector_field: knn_body}}
        elif isinstance(query, KeywordBackendQuery):
            match_query: dict[str, Any] = {"match": {query.keyword_field: {"query": query.text}}}
            if filter_query is None:
                query_body = match_query
            else:
                query_body = {"bool": {"must": [match_query], "filter": [filter_query]}}
        else:
            raise TypeError(f"不支持的 OpenSearch 查询类型：{type(query).__name__}")
        return {
            "size": query.top_k,
            "_source": OpenSearchBackend._source_fields(query),
            "query": query_body,
        }

    async def search(self, query: BackendQuery) -> list[BackendHit]:
        """执行一次 `_search` 请求；不会调用 index 管理或文档写入 API。"""
        try:
            client = await self._get_client()
            result = await self._call(
                client.search,
                index=query.target,
                body=self._build_body(query),
                request_timeout=query.timeout_seconds,
            )
            return self._normalize_hits(result, query)
        except asyncio.CancelledError:
            raise
        except DataServiceError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise BackendUnavailableError() from exc
        except Exception as exc:
            logger.warning("OpenSearch search failed error_type=%s", type(exc).__name__)
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int) and 400 <= status_code < 500 and status_code not in {
                408,
                429,
            }:
                raise BackendProtocolError() from exc
            raise BackendUnavailableError() from exc

    async def health(self, target: str, *, timeout_seconds: float) -> bool:
        """通过 indices.exists 只读检查目标 index。"""
        try:
            client = await self._get_client()
            exists = await asyncio.wait_for(
                self._call(client.indices.exists, index=target),
                timeout=timeout_seconds,
            )
            return bool(exists)
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
