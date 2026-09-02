"""knowledge-agent-runtime 容器入口。"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from contracts.v3 import RuntimeCitationV1

import uvicorn

from .app import create_app
from .bundle import load_bundle
from .service import RetrievalEvidence, RuntimeBackend, RuntimeService


class HttpCoreRuntimeBackend(RuntimeBackend):
    """Runtime 仅通过 Core 私有网络边界获取检索和生成能力。"""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self.ready = bool(self._base_url.startswith(("http://", "https://")))

    async def is_ready(self) -> bool:
        if not self.ready:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self._base_url}/ready")
            return response.is_success
        except httpx.HTTPError:
            return False

    async def retrieve(self, *, resource_id: str, query: str, top_k: int, pipeline: str) -> list[RetrievalEvidence]:
        if not self.ready:
            raise RuntimeError("Core Runtime backend 尚未配置")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self._base_url}/internal/v1/runtime/retrieve", json={"resource_id": resource_id, "query": query, "top_k": top_k, "pipeline": pipeline})
            response.raise_for_status()
        evidence = response.json().get("evidence")
        if not isinstance(evidence, list):
            raise ValueError("Core Runtime retrieve 响应格式非法")
        return [
            RetrievalEvidence(
                citation=RuntimeCitationV1.model_validate(item["citation"]),
                content=item["content"],
                score=item["score"],
            )
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("content"), str) and isinstance(item.get("score"), (int, float))
        ]

    async def answer(self, *, system_instruction: str, task: str, evidence: list[RetrievalEvidence], max_tokens: int) -> str:
        if not self.ready:
            raise RuntimeError("Core Runtime backend 尚未配置")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self._base_url}/internal/v1/runtime/answer", json={"system_instruction": system_instruction, "task": task, "evidence": [item_to_json(item) for item in evidence], "max_tokens": max_tokens})
            response.raise_for_status()
        content = response.json().get("content")
        if not isinstance(content, str):
            raise ValueError("Core Runtime answer 响应格式非法")
        return content


def item_to_json(item: RetrievalEvidence) -> dict[str, object]:
    return {"citation": item.citation.model_dump(mode="json"), "content": item.content, "score": item.score}


def main() -> None:
    """仅从平台注入的 Bundle 挂载点启动 Runtime。"""

    bundle_directory = Path(os.environ.get("MUYE_RUNTIME_BUNDLE_DIR", "/run/muye/bundle"))
    bundle = load_bundle(bundle_directory)
    core_base_url = os.environ.get("MUYE_RUNTIME_CORE_BASE_URL", "").strip()
    app = create_app(RuntimeService(bundle, HttpCoreRuntimeBackend(core_base_url)))
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":  # pragma: no cover
    main()
