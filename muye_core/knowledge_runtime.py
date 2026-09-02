"""Core 持有的知识 Runtime 下游适配；Runtime 容器不接触这些地址或凭据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from contracts.v3 import RuntimeCitationV1


@dataclass(frozen=True, slots=True)
class CoreEvidence:
    citation: RuntimeCitationV1
    content: str
    score: float


class CoreKnowledgeBackend(Protocol):
    async def retrieve(self, *, resource_id: str, query: str, top_k: int, pipeline: str) -> list[CoreEvidence]: ...

    async def answer(self, *, system_instruction: str, task: str, evidence: list[CoreEvidence], max_tokens: int) -> str: ...


class HttpKnowledgeBackend:
    """Core 到既有 Data/LLM 服务的受控桥接，凭据只停留在 Core 环境。"""

    def __init__(self, *, data_base_url: str, llm_base_url: str, data_token: str = "") -> None:
        self._data_base_url = data_base_url.rstrip("/")
        self._llm_base_url = llm_base_url.rstrip("/")
        self._data_token = data_token
        self.ready = bool(self._data_base_url.startswith(("http://", "https://")) and self._llm_base_url.startswith(("http://", "https://")))

    async def retrieve(self, *, resource_id: str, query: str, top_k: int, pipeline: str) -> list[CoreEvidence]:
        if not self.ready:
            raise RuntimeError("Core 知识后端尚未配置")
        headers = {"Authorization": f"Bearer {self._data_token}"} if self._data_token else {}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self._data_base_url}/api/v1/retrieve", headers=headers, json={"resource": resource_id, "query": query, "top_k": top_k, "pipeline": pipeline, "return_fields": ["source_asset_id", "locator"]})
            response.raise_for_status()
        hits = response.json().get("hits")
        if not isinstance(hits, list):
            raise ValueError("Data 检索响应格式非法")
        evidence: list[CoreEvidence] = []
        for hit in hits:
            fields = hit.get("fields") if isinstance(hit, dict) else None
            asset_id = fields.get("source_asset_id") if isinstance(fields, dict) else None
            locator = fields.get("locator", hit.get("id")) if isinstance(fields, dict) else None
            if not isinstance(asset_id, str) or not isinstance(locator, str) or not isinstance(hit.get("content"), str):
                continue
            evidence.append(CoreEvidence(RuntimeCitationV1(citation_id=f"cite.{hit['id']}", source_asset_id=asset_id, locator=locator), hit["content"], float(hit["score"])))
        return evidence

    async def answer(self, *, system_instruction: str, task: str, evidence: list[CoreEvidence], max_tokens: int) -> str:
        if not self.ready:
            raise RuntimeError("Core 知识后端尚未配置")
        sources = "\n\n".join(f"[资料 {index + 1}]\n{item.content}" for index, item in enumerate(evidence))
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self._llm_base_url}/api/v2/chat", json={"messages": [{"role": "system", "content": system_instruction}, {"role": "user", "content": f"资料：\n{sources}\n\n问题：{task}"}], "max_tokens": max_tokens})
            response.raise_for_status()
        payload = response.json()
        content = payload.get("data", {}).get("content") if payload.get("success") is True else None
        if not isinstance(content, str):
            raise ValueError("LLM 响应格式非法")
        return content
