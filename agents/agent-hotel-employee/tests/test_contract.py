"""生成 Agent 的离线 SDK capabilities 契约。"""
from __future__ import annotations

from muye_multi_agent_sdk import AgentIdentity, assert_agent_contract

from agent import GeneratedHotelEmployeeAgent


def test_generated_agent_declares_deployment_identity(monkeypatch) -> None:
    """部署身份只接受部署层环境变量，Agent 身份来自 agent.yaml。"""
    monkeypatch.setenv("MUYE_AGENT_SERVICE_ID", "generated-service")
    monkeypatch.setenv("MUYE_AGENT_DEPLOYMENT_ID", "generated-deployment")
    monkeypatch.setenv("MUYE_AGENT_DESCRIPTOR_CHECKSUM", "a" * 64)
    monkeypatch.setenv("MUYE_AGENT_SOURCE_TREE_CHECKSUM", "b" * 64)
    monkeypatch.setenv("MUYE_AGENT_DATA_TOKEN", "generated-data-token")

    agent = GeneratedHotelEmployeeAgent()

    assert_agent_contract(
        agent,
        expected_identity=AgentIdentity(
            agent_id="agent_hotel_employee",
            agent_version="0.1.0",
            descriptor_checksum="a" * 64,
            source_tree_checksum="b" * 64,
        ),
        required_features={"cancel", "citation_blocks", "sse", "trusted_deadline"},
    )


def test_generated_agent_applies_descriptor_model_and_budgets(monkeypatch) -> None:
    """模型 alias、输出 token、总超时和每次请求工具上限均来自 agent.yaml。"""
    monkeypatch.setenv("MUYE_AGENT_SERVICE_ID", "generated-service")
    monkeypatch.setenv("MUYE_AGENT_DEPLOYMENT_ID", "generated-deployment")
    monkeypatch.setenv("MUYE_AGENT_DESCRIPTOR_CHECKSUM", "a" * 64)
    monkeypatch.setenv("MUYE_AGENT_SOURCE_TREE_CHECKSUM", "b" * 64)
    monkeypatch.setenv("MUYE_AGENT_DATA_TOKEN", "generated-data-token")
    monkeypatch.setenv("MUYE_SDK_MODEL", "environment-must-not-override-descriptor")
    monkeypatch.setenv("MUYE_SDK_MODEL_MAX_TOKENS", "1")
    monkeypatch.setenv("MUYE_SDK_REQUEST_TIMEOUT_SECONDS", "1")

    agent = GeneratedHotelEmployeeAgent()

    assert agent.config.model.model == "deepseek-v4-flash"
    assert agent.config.model.max_tokens == 8192
    assert agent.config.request_timeout_seconds == 30
    assert agent._tool_limiter().run_limit == 4
