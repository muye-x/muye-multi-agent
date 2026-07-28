"""muye-llm Embedding 与 Rerank 的严格异步客户端。"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from src.errors import EmbeddingUnavailableError, RerankUnavailableError


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RerankScore:
    """Rerank 返回的原始文档索引和相关度分数。"""

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class LLMModelCapabilities:
    """muye-llm 当前公开的 Embedding 维度与 Rerank alias。"""

    embedding_models: Mapping[str, int | None]
    rerank_models: frozenset[str]


class MuyeLLMClient:
    """调用 muye-llm，不执行模型供应商重试或记录正文。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | Any | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
        )

    @staticmethod
    def _response_data(response: Any, *, operation: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                recoverable = exc.response.status_code in {408, 429, 500, 502, 503, 504}
            elif isinstance(exc, ValueError):
                recoverable = False
            else:
                recoverable = True
            if operation == "embed":
                raise EmbeddingUnavailableError(recoverable=recoverable) from exc
            raise RerankUnavailableError(recoverable=recoverable) from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            if operation == "embed":
                raise EmbeddingUnavailableError(recoverable=False)
            raise RerankUnavailableError(recoverable=False)
        data = payload.get("data")
        if not isinstance(data, dict):
            if operation == "embed":
                raise EmbeddingUnavailableError(recoverable=False)
            raise RerankUnavailableError(recoverable=False)
        return data

    async def embed(
        self,
        text: str,
        *,
        model: str,
        expected_dimensions: int,
        trace_id: str,
    ) -> tuple[float, ...]:
        """生成一个查询向量并校验模型响应数量、维度和有限数值。"""
        try:
            response = await self._client.post(
                "/api/v2/embed",
                json={"texts": [text], "model": model, "trace_id": trace_id},
                timeout=self._timeout_seconds,
            )
            data = self._response_data(response, operation="embed")
        except asyncio.CancelledError:
            raise
        except (EmbeddingUnavailableError,):
            raise
        except Exception as exc:
            logger.warning("muye-llm embed failed trace_id=%s error_type=%s", trace_id, type(exc).__name__)
            raise EmbeddingUnavailableError() from exc

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != 1 or not isinstance(embeddings[0], list):
            raise EmbeddingUnavailableError(recoverable=False)
        try:
            vector = tuple(float(item) for item in embeddings[0])
        except (TypeError, ValueError) as exc:
            raise EmbeddingUnavailableError(recoverable=False) from exc
        dimensions = data.get("dimensions", len(vector))
        if dimensions != expected_dimensions or len(vector) != expected_dimensions:
            raise EmbeddingUnavailableError(recoverable=False)
        if any(not math.isfinite(item) for item in vector):
            raise EmbeddingUnavailableError(recoverable=False)
        return vector

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str,
        trace_id: str,
    ) -> list[RerankScore]:
        """调用重排并验证索引唯一、边界合法且分数有限。"""
        try:
            response = await self._client.post(
                "/api/v2/rerank",
                json={
                    "query": query,
                    "documents": documents,
                    "top_n": top_n,
                    "model": model,
                    "trace_id": trace_id,
                },
                timeout=self._timeout_seconds,
            )
            data = self._response_data(response, operation="rerank")
        except asyncio.CancelledError:
            raise
        except (RerankUnavailableError,):
            raise
        except Exception as exc:
            logger.warning("muye-llm rerank failed trace_id=%s error_type=%s", trace_id, type(exc).__name__)
            raise RerankUnavailableError() from exc

        raw_results = data.get("results")
        if not isinstance(raw_results, list) or len(raw_results) > top_n:
            raise RerankUnavailableError(recoverable=False)
        parsed: list[RerankScore] = []
        seen: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                raise RerankUnavailableError(recoverable=False)
            index = item.get("index")
            score = item.get("score")
            if type(index) is not int or index < 0 or index >= len(documents) or index in seen:
                raise RerankUnavailableError(recoverable=False)
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise RerankUnavailableError(recoverable=False)
            numeric_score = float(score)
            if not math.isfinite(numeric_score):
                raise RerankUnavailableError(recoverable=False)
            seen.add(index)
            parsed.append(RerankScore(index=index, score=numeric_score))
        if documents and not parsed:
            raise RerankUnavailableError(recoverable=False)
        return parsed

    async def health(self) -> bool:
        """检查 muye-llm 进程存活，不读取模型或业务数据。"""
        try:
            response = await self._client.get("/health", timeout=self._timeout_seconds)
            return response.status_code == 200
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _model_aliases(value: Any) -> frozenset[str]:
        """从模型列表中提取严格 alias，拒绝不完整的能力响应。"""
        if not isinstance(value, list):
            raise ValueError("模型能力列表无效")
        aliases: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("模型能力项无效")
            alias = item.get("id")
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError("模型 alias 无效")
            normalized = alias.strip()
            if normalized in aliases:
                raise ValueError("模型 alias 重复")
            aliases.add(normalized)
        return frozenset(aliases)

    @staticmethod
    def _embedding_models(value: Any) -> dict[str, int | None]:
        """解析 Embedding alias 及可选固定维度。"""
        aliases = MuyeLLMClient._model_aliases(value)
        models: dict[str, int | None] = {}
        for item in value:
            alias = item["id"].strip()
            dimensions = item.get("dimensions")
            if dimensions is not None and (
                isinstance(dimensions, bool)
                or not isinstance(dimensions, int)
                or dimensions < 1
            ):
                raise ValueError("Embedding 模型维度无效")
            models[alias] = dimensions
        if set(models) != set(aliases):  # pragma: no cover - guarded by _model_aliases.
            raise ValueError("Embedding 模型能力无效")
        return models

    async def model_capabilities(self) -> LLMModelCapabilities | None:
        """读取实际模型 alias；协议或网络失败时返回不可用而非仅检查进程存活。"""
        try:
            response = await self._client.get("/api/v2/models", timeout=self._timeout_seconds)
            data = self._response_data(response, operation="embed")
            return LLMModelCapabilities(
                embedding_models=self._embedding_models(data.get("embedding_models")),
                rerank_models=self._model_aliases(data.get("rerank_models")),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "muye-llm model capabilities unavailable error_type=%s",
                type(exc).__name__,
            )
            return None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
