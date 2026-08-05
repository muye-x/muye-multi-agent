"""由 react-knowledge/v1 渲染的只读知识 Agent。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from pydantic import BaseModel, ConfigDict, Field

from muye_multi_agent_sdk import AgentConfig, AgentIdentity, AgentMetadata, ReActAgent, load_yaml_config
from muye_multi_agent_sdk.integrations.muye_data import DataAccessContext, DataClient
from muye_multi_agent_sdk.runtime import ExecutionOptions
from muye_multi_agent_sdk.tools import create_scoped_data_retrieval_tool


class _ResourceBinding(BaseModel):
    """agent.yaml 中固定的一个逻辑 Resource 与 Skill 绑定。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    resource_id: str = Field(min_length=1)
    skill_ref: str = Field(min_length=3)


class _Runtime(BaseModel):
    """agent.yaml 中由运行时和部署生成器共同消费的固定预算。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    internal_port: int = Field(ge=1, le=65535)
    timeout_seconds: int = Field(ge=1, le=300)
    token_budget: int = Field(ge=128, le=65536)
    tool_budget: int = Field(ge=1, le=20)
    max_concurrency: int = Field(ge=1, le=128)
    memory_limit: str = Field(pattern=r"^[1-9][0-9]{0,3}[mMgG]$")


class _Deployment(BaseModel):
    """agent.yaml 中由开发者显式控制的部署开关。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class _Source(BaseModel):
    """模板和 provenance 的可重放来源字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    provenance_file: str = Field(min_length=1)


class _AgentDescriptor(BaseModel):
    """运行时只读取 agent.yaml 中的身份、能力与资源事实。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supported_intents: list[str] = Field(min_length=1)
    entrypoint: str = Field(min_length=1)
    api_profile: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    model_alias: str = Field(min_length=1)
    resources: list[_ResourceBinding] = Field(min_length=1, max_length=1)
    runtime: _Runtime
    deployment: _Deployment
    source: _Source


class GeneratedHotelEmployeeAgent(ReActAgent):
    """只调用固定逻辑资源和固定 scope 的内部知识 Agent。"""

    def __init__(self) -> None:
        descriptor = load_yaml_config(Path(__file__).with_name("agent.yaml"), _AgentDescriptor)
        config = self._sdk_config(descriptor)
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
        self._descriptor = descriptor

    async def aclose(self) -> None:
        """释放 SDK 状态与模板持有的认证 Data HTTP 连接池。"""
        try:
            await super().aclose()
        finally:
            await self._data_http_client.aclose()

    @staticmethod
    def _sdk_config(descriptor: _AgentDescriptor) -> AgentConfig:
        """保留部署连接配置，但以 descriptor 覆盖模型、输出 token 与总请求预算。"""
        environment_config = AgentConfig.from_env(env_file=Path(__file__).with_name(".env"))
        model_config = environment_config.model.model_copy(
            update={
                "model": descriptor.model_alias,
                "max_tokens": descriptor.runtime.token_budget,
            }
        )
        return environment_config.model_copy(
            update={
                "model": model_config,
                "request_timeout_seconds": descriptor.runtime.timeout_seconds,
            }
        )

    def _tool_limiter(self) -> ToolCallLimitMiddleware:
        """限制单次请求的实际工具执行次数，不让模型通过重复调用扩大检索范围。"""
        return ToolCallLimitMiddleware(run_limit=self._descriptor.runtime.tool_budget, exit_behavior="continue")

    async def _agent(self, options: ExecutionOptions) -> Any:
        """构造带逐请求工具预算的 ReAct graph；其余 SDK 生命周期保持不变。"""
        stateful = options.context_enabled and options.profile in self.config.context.enabled_profiles
        if stateful in self._agents:
            return self._agents[stateful]
        kwargs: dict[str, Any] = {
            "model": self._get_model(),
            "tools": self.langchain_tools,
            "system_prompt": self.instructions,
            "middleware": [self._tool_limiter()],
        }
        checkpointer = await self.checkpointer(options)
        if checkpointer is not None:
            kwargs["checkpointer"] = checkpointer
        self._agents[stateful] = create_agent(**kwargs)
        return self._agents[stateful]

    def _result_from_messages(self, messages: list[Any], request: Any) -> Any:
        """把实际检索命中的版本与 locator 放入仅供 Main/Control 使用的终态字段。"""
        result = super()._result_from_messages(messages, request)
        if result.status != "success":
            return result
        trusted_citations = self._trusted_citations(messages)
        if not trusted_citations:
            return result
        result_data = dict(result.result_data or {})
        result_data["_muye_citations"] = trusted_citations
        return result.model_copy(update={"result_data": result_data})

    def _trusted_citations(self, messages: list[Any]) -> list[dict[str, object]]:
        """只从真实 ToolMessage hit 字段组合引用，忽略模型正文中的同名内容。"""
        records: list[dict[str, object]] = []
        seen: set[str] = set()
        for message in messages:
            if getattr(message, "type", "") != "tool":
                continue
            payload = self._tool_payload(getattr(message, "content", None))
            if not isinstance(payload, dict):
                continue
            public_citations = {
                item.get("citation_id"): item
                for item in payload.get("citations", [])
                if isinstance(item, dict) and isinstance(item.get("citation_id"), str)
            }
            for hit in payload.get("hits", []):
                fields = hit.get("fields") if isinstance(hit, dict) else None
                if not isinstance(fields, dict):
                    continue
                citation_id = fields.get("citation_id")
                knowledge_version_id = fields.get("knowledge_version_id")
                locators = fields.get("source_locators", fields.get("source_locator"))
                locator = locators[0] if isinstance(locators, list) and locators else locators
                public = public_citations.get(citation_id)
                if (
                    not isinstance(citation_id, str)
                    or citation_id in seen
                    or not isinstance(knowledge_version_id, str)
                    or not isinstance(locator, dict)
                    or not isinstance(public, dict)
                    or not isinstance(public.get("title"), str)
                    or not isinstance(public.get("source"), str)
                ):
                    continue
                records.append(
                    {
                        "citation_id": citation_id,
                        "knowledge_version_id": knowledge_version_id,
                        "locator": locator,
                        "title": public["title"],
                        "source": public["source"],
                    }
                )
                seen.add(citation_id)
                if len(records) == 50:
                    return records
        return records

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
                name="hotel_employee_agent_retrieve",
                resource=self._descriptor.resources[0].resource_id,
                pipeline="hybrid",
                fixed_filter={"field": "knowledge_version_id","op": "eq","value": "kv_c6d8292a24bec8ba"},
                return_fields=["title","source","citation_id","source_locator","knowledge_version_id"],
            )
        ]
