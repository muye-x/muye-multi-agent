"""Knowledge Worker 对 muye-llm Embedding API 的最小同步适配。"""

from __future__ import annotations

from collections.abc import Sequence
import math
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .errors import DependencyUnavailableError


class Embedder(Protocol):
    """将有序 chunk 文本转为固定维度向量的可替换边界。"""

    def embed(self, texts: Sequence[str], *, model: str, dimensions: int, trace_id: str) -> list[list[float]]:
        """返回与输入一一对应的有限浮点向量，否则抛出明确依赖错误。"""


class MuyeLLMEmbedder:
    """调用受信任 muye-llm `/api/v2/embed`，不接受调用方传入任意 URL。"""

    def __init__(self, *, base_url: str, timeout_seconds: float = 30.0) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("muye-llm base URL 必须是不含凭据的 HTTP(S) URL")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def embed(self, texts: Sequence[str], *, model: str, dimensions: int, trace_id: str) -> list[list[float]]:
        """批量请求 Embedding 并严格校验数目、维度与数值有限性。"""
        if not texts:
            return []
        if len(texts) > 256 or sum(len(text) for text in texts) > 1_000_000:
            raise DependencyUnavailableError("单次 Embedding 请求超过数量或字符预算")
        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout_seconds) as client:
                response = client.post(
                    "/api/v2/embed",
                    json={"texts": list(texts), "model": model, "trace_id": trace_id},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError("muye-llm Embedding 服务不可用") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise DependencyUnavailableError("muye-llm Embedding 响应无效")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("dimensions") != dimensions:
            raise DependencyUnavailableError("muye-llm Embedding 维度不匹配")
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise DependencyUnavailableError("muye-llm Embedding 数量不匹配")
        normalized: list[list[float]] = []
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != dimensions:
                raise DependencyUnavailableError("muye-llm Embedding 向量维度不匹配")
            try:
                converted = [float(value) for value in vector]
            except (TypeError, ValueError) as exc:
                raise DependencyUnavailableError("muye-llm Embedding 向量包含非法数值") from exc
            if not all(math.isfinite(value) for value in converted):
                raise DependencyUnavailableError("muye-llm Embedding 向量包含非有限数值")
            normalized.append(converted)
        return normalized
