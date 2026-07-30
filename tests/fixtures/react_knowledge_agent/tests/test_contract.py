"""独立 fixture 的 SDK capabilities 契约。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from muye_multi_agent_sdk import AgentIdentity, assert_agent_contract


def _fixture_agent_type():
    """以唯一模块名加载 fixture，避免覆盖 Scaffold 根目录的 main/agent 模块。"""
    agent_path = Path(__file__).resolve().parents[1] / "agent.py"
    module_name = "react_knowledge_fixture_agent"
    specification = importlib.util.spec_from_file_location(module_name, agent_path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module.FixtureKnowledgeAgent


def test_fixture_declares_deployment_identity(monkeypatch) -> None:
    monkeypatch.setenv("MUYE_AGENT_SERVICE_ID", "fixture-service")
    monkeypatch.setenv("MUYE_AGENT_DEPLOYMENT_ID", "fixture-deployment")
    monkeypatch.setenv("MUYE_AGENT_DESCRIPTOR_CHECKSUM", "a" * 64)
    monkeypatch.setenv("MUYE_AGENT_SOURCE_TREE_CHECKSUM", "b" * 64)
    agent = _fixture_agent_type()()
    assert_agent_contract(
        agent,
        expected_identity=AgentIdentity(
            agent_id="agent_fixture_knowledge",
            agent_version="1.0.0",
            descriptor_checksum="a" * 64,
            source_tree_checksum="b" * 64,
        ),
        required_features={"cancel", "citation_blocks", "sse", "trusted_deadline"},
    )
