"""react-knowledge/v1 的独立运行 fixture。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from muye_multi_agent_sdk import AgentConfig, AgentIdentity, AgentMetadata, ReActAgent, load_yaml_config
from muye_multi_agent_sdk.integrations.muye_data import DataAccessContext, DataClient
from muye_multi_agent_sdk.tools import create_scoped_data_retrieval_tool


class _ResourceBinding(BaseModel):
    """fixture 所需的最小逻辑资源绑定。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    resource_id: str = Field(min_length=1)
    skill_ref: str = Field(min_length=3)


class _FixtureDescriptor(BaseModel):
    """从 agent.yaml 读取的 template fixture 身份与资源字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    agent_id: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supported_intents: list[str]
    resources: list[_ResourceBinding] = Field(min_length=1, max_length=1)


class FixtureKnowledgeAgent(ReActAgent):
    """只读 fixture Agent，固定到一个逻辑知识资源与 knowledge_id。"""

    def __init__(self) -> None:
        config = AgentConfig.from_env(env_file=Path(__file__).with_name(".env"))
        data_token = os.environ.get("MUYE_AGENT_DATA_TOKEN", "").strip()
        if not data_token:
            raise ValueError("MUYE_AGENT_DATA_TOKEN 未配置")
        self._data_http_client = httpx.AsyncClient(
            base_url=config.data.base_url,
            timeout=config.data.timeout_seconds,
            headers={"Authorization": f"Bearer {data_token}"},
            trust_env=False,
        )
        super().__init__(config, data_client=DataClient(config.data, http_client=self._data_http_client))
        self._descriptor = load_yaml_config(Path(__file__).with_name("agent.yaml"), _FixtureDescriptor)

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            await self._data_http_client.aclose()

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            name=self._descriptor.slug,
            version=self._descriptor.version,
            description=self._descriptor.description,
            supported_intents=self._descriptor.supported_intents,
            identity=AgentIdentity(
                agent_id=self._descriptor.agent_id,
                agent_version=self._descriptor.version,
                descriptor_checksum=os.environ["MUYE_AGENT_DESCRIPTOR_CHECKSUM"],
                source_tree_checksum=os.environ["MUYE_AGENT_SOURCE_TREE_CHECKSUM"],
            ),
        )

    @property
    def instructions(self) -> str:
        return (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

    def _result_from_messages(self, messages: list[Any], request: Any) -> Any:
        result = super()._result_from_messages(messages, request)
        if result.status != "success":
            return result
        trusted_citations = self._trusted_citations(messages)
        if not trusted_citations:
            return result
        return result.model_copy(
            update={"result_data": {**(result.result_data or {}), "_muye_citations": trusted_citations}}
        )

    def _trusted_citations(self, messages: list[Any]) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for message in messages:
            payload = self._tool_payload(getattr(message, "content", None))
            if getattr(message, "type", "") != "tool" or not isinstance(payload, dict):
                continue
            public = {
                item.get("citation_id"): item
                for item in payload.get("citations", [])
                if isinstance(item, dict) and isinstance(item.get("citation_id"), str)
            }
            for hit in payload.get("hits", []):
                fields = hit.get("fields") if isinstance(hit, dict) else None
                if not isinstance(fields, dict):
                    continue
                citation_id = fields.get("citation_id")
                locators = fields.get("source_locators", fields.get("source_locator"))
                locator = locators[0] if isinstance(locators, list) and locators else locators
                citation = public.get(citation_id)
                if (
                    isinstance(citation_id, str)
                    and isinstance(fields.get("knowledge_version_id"), str)
                    and isinstance(locator, dict)
                    and isinstance(citation, dict)
                    and isinstance(citation.get("title"), str)
                    and isinstance(citation.get("source"), str)
                ):
                    records.append(
                        {
                            "citation_id": citation_id,
                            "knowledge_version_id": fields["knowledge_version_id"],
                            "locator": locator,
                            "title": citation["title"],
                            "source": citation["source"],
                        }
                    )
        return records[:50]

    @property
    def langchain_tools(self) -> list:
        return [
            create_scoped_data_retrieval_tool(
                self.data_client,
                access_context=DataAccessContext(
                    service_id=os.environ["MUYE_AGENT_SERVICE_ID"],
                    deployment_id=os.environ["MUYE_AGENT_DEPLOYMENT_ID"],
                    agent=self.metadata.identity,
                ),
                name="fixture_knowledge_retrieve",
                resource=self._descriptor.resources[0].resource_id,
                pipeline="hybrid",
                fixed_filter={"op": "eq", "field": "knowledge_id", "value": "kb.product_handbook"},
                return_fields=[
                    "title",
                    "source",
                    "citation_id",
                    "source_locator",
                    "source_locators",
                    "knowledge_version_id",
                ],
            )
        ]
