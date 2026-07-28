"""不启动真实上游连接的 FastAPI HTTP 契约测试。"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

from src.core.llm_client import EmbeddingResult
from src.utils.exceptions import ServiceUnavailableException


def _load_service_main() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("muye_llm_http_test_main", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _HttpFakeClient:
    async def embed_result(self, *_args: Any, **_kwargs: Any) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=((0.1, 0.2),),
            model_alias="embedding-alias",
            dimensions=2,
        )

    async def rerank(self, **_kwargs: Any) -> None:
        raise ServiceUnavailableException("Rerank 功能未启用")


def test_http_routes_preserve_status_and_response_contracts() -> None:
    service_main = _load_service_main()
    service_main.app.state.llm_client = _HttpFakeClient()

    async def call() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=service_main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://muye-llm.test",
        ) as client:
            embed_response = await client.post(
                "/api/v2/embed",
                json={"texts": ["text"], "model": "embedding-alias"},
            )
            disabled_response = await client.post(
                "/api/v2/rerank",
                json={"query": "query", "documents": ["document"], "top_n": 1},
            )
            invalid_response = await client.post(
                "/api/v2/rerank",
                json={"query": "query", "documents": ["document"], "top_n": "1"},
            )
            return embed_response, disabled_response, invalid_response

    embed_response, disabled_response, invalid_response = asyncio.run(call())

    assert embed_response.status_code == 200
    assert embed_response.json()["data"] == {
        "embeddings": [[0.1, 0.2]],
        "count": 1,
        "model": "embedding-alias",
        "dimensions": 2,
    }
    assert disabled_response.status_code == 503
    assert disabled_response.json() == {
        "success": False,
        "code": 503,
        "message": "Rerank 功能未启用",
        "data": None,
    }
    assert invalid_response.status_code == 422
