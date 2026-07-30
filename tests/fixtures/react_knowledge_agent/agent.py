"""react-knowledge/v1 的独立运行 fixture。"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from muye_multi_agent_sdk import AgentIdentity, AgentMetadata, ReActAgent, load_yaml_config
from muye_multi_agent_sdk.integrations.muye_data import DataAccessContext
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
        super().__init__()
        self._descriptor = load_yaml_config(Path(__file__).with_name("agent.yaml"), _FixtureDescriptor)

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
                return_fields=["title", "source", "citation_id", "source_locator"],
            )
        ]
