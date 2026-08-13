"""阶段 2 标准 ReAct 知识模板与独立 fixture 的静态契约测试。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import Request
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIRECTORY = PROJECT_ROOT / "templates" / "agents" / "react-knowledge" / "v1"
FIXTURE_DIRECTORY = PROJECT_ROOT / "tests" / "fixtures" / "react_knowledge_agent"


def test_react_knowledge_template_declares_a_pinned_sdk_and_digest_only_base_image() -> None:
    manifest = yaml.safe_load((TEMPLATE_DIRECTORY / "template-manifest.yaml").read_text(encoding="utf-8"))
    dockerfile = (TEMPLATE_DIRECTORY / "Dockerfile").read_text(encoding="utf-8")

    assert manifest == {
        "template_id": "react-knowledge",
        "template_version": "1.0.2",
        "sdk_version": "2.1.0",
        "sdk_version_specifier": "==2.1.0",
        "base_image_build_arg": "MUYE_AGENT_BASE_IMAGE",
        "api_profile": "internal",
    }
    assert (TEMPLATE_DIRECTORY / "requirements.txt.tmpl").read_text(encoding="utf-8") == (
        "muye-multi-agent-sdk @ https://github.com/muye-x/muye-multi-agent-sdk/archive/refs/tags/v2.1.0.tar.gz\n"
        "langchain==1.3.14\n"
        "httpx==0.28.1\n"
    )
    assert dockerfile.startswith("ARG MUYE_AGENT_BASE_IMAGE\nFROM ${MUYE_AGENT_BASE_IMAGE}\n")
    assert "USER 10001" in dockerfile
    dockerignore = (TEMPLATE_DIRECTORY / ".dockerignore").read_text(encoding="utf-8")
    for ignored_pattern in (".env*", ".git/", "*.pem", "*.key", "__pycache__/"):
        assert ignored_pattern in dockerignore
    environment = (TEMPLATE_DIRECTORY / ".env.example").read_text(encoding="utf-8")
    for name in (
        "MUYE_LLM_BASE_URL",
        "MUYE_SDK_DATA_BASE_URL",
        "MUYE_AGENT_MAIN_TOKEN",
        "MUYE_AGENT_CONTROL_TOKEN",
        "MUYE_AGENT_DATA_TOKEN",
    ):
        assert f"{name}=" in environment


def test_react_knowledge_template_uses_only_scoped_retrieval_and_trusted_runtime_identity() -> None:
    source = (TEMPLATE_DIRECTORY / "agent.py.tmpl").read_text(encoding="utf-8")

    assert "create_scoped_data_retrieval_tool" in source
    assert "create_data_retrieval_tool(" not in source
    for environment_name in (
        "MUYE_AGENT_SERVICE_ID",
        "MUYE_AGENT_DEPLOYMENT_ID",
        "MUYE_AGENT_DESCRIPTOR_CHECKSUM",
        "MUYE_AGENT_SOURCE_TREE_CHECKSUM",
        "MUYE_AGENT_DATA_TOKEN",
    ):
        assert environment_name in source
    assert "MilvusClient" not in source
    assert "scaffold" not in source.lower()
    assert "load_yaml_config" in source
    assert "Path(__file__).with_name(\"agent.yaml\")" in source
    assert "AgentConfig.from_env" in source
    assert "ToolCallLimitMiddleware" in source
    assert "token_budget" in source
    assert "tool_budget" in source
    assert (TEMPLATE_DIRECTORY / "README.md.tmpl").is_file()
    launcher = TEMPLATE_DIRECTORY / "run-local.sh.tmpl"
    assert launcher.is_file()
    launcher_source = launcher.read_text(encoding="utf-8")
    for value in (
        "muye-llm",
        "muye-data",
        ".run-local-muye-data.yaml",
        "MUYE_DATA_CONFIG_PATH",
        "MUYE_DATA_AGENT_AUTH_ENABLED=false",
        "uvicorn main:app",
        "Authorization: Bearer $(sed",
        "/invoke",
    ):
        assert value in launcher_source
    for test_name in ("test_contract.py.tmpl", "test_retrieval.py.tmpl", "test_e2e.py.tmpl"):
        assert (TEMPLATE_DIRECTORY / "tests" / test_name).is_file()


def test_fixture_agent_has_no_scaffold_import_and_keeps_a_fixed_scope() -> None:
    agent_source = (FIXTURE_DIRECTORY / "agent.py").read_text(encoding="utf-8")
    descriptor = yaml.safe_load((FIXTURE_DIRECTORY / "agent.yaml").read_text(encoding="utf-8"))

    compile(agent_source, str(FIXTURE_DIRECTORY / "agent.py"), "exec")
    compile((FIXTURE_DIRECTORY / "main.py").read_text(encoding="utf-8"), str(FIXTURE_DIRECTORY / "main.py"), "exec")
    assert "scaffold" not in agent_source.lower()
    assert '"field": "knowledge_id"' in agent_source
    assert '"value": "kb.product_handbook"' in agent_source
    assert descriptor["agent_id"] == "agent_fixture_knowledge"
    assert (FIXTURE_DIRECTORY / "requirements.txt").read_text(encoding="utf-8") == (
        "muye-multi-agent-sdk @ https://github.com/muye-x/muye-multi-agent-sdk/archive/refs/tags/v2.1.0.tar.gz\n"
        "langchain==1.3.14\n"
        "httpx==0.28.1\n"
    )


def test_internal_entrypoint_separates_main_and_control_credentials() -> None:
    source = (TEMPLATE_DIRECTORY / "main.py.tmpl").read_text(encoding="utf-8")

    assert "MUYE_AGENT_MAIN_TOKEN" in source
    assert "MUYE_AGENT_CONTROL_TOKEN" in source
    assert 'request.url.path == "/capabilities"' in source
    assert "MUYE_AGENT_INTERNAL_TOKEN" not in source
    assert 'load_dotenv(Path(__file__).with_name(".env"), override=False)' in source


def test_control_and_data_credentials_cannot_invoke_sub_agent(monkeypatch) -> None:
    monkeypatch.setenv("MUYE_AGENT_MAIN_TOKEN", "main-token")
    monkeypatch.setenv("MUYE_AGENT_CONTROL_TOKEN", "control-token")
    monkeypatch.setenv("MUYE_AGENT_DATA_TOKEN", "data-token")
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(FixtureKnowledgeAgent=lambda: object()))
    import muye_multi_agent_sdk

    monkeypatch.setattr(muye_multi_agent_sdk, "create_app", lambda *_args, **_kwargs: object())
    specification = importlib.util.spec_from_file_location(
        "fixture_internal_entrypoint",
        FIXTURE_DIRECTORY / "main.py",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    def request(path: str, token: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            }
        )

    assert module._verify_internal_request(request("/invoke", "main-token")) is True
    assert module._verify_internal_request(request("/capabilities", "control-token")) is True
    for path in ("/invoke", "/invoke/stream", "/cancel"):
        assert module._verify_internal_request(request(path, "control-token")) is False
        assert module._verify_internal_request(request(path, "data-token")) is False


def test_internal_entrypoint_rejects_reused_service_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MUYE_AGENT_MAIN_TOKEN", "shared-token")
    monkeypatch.setenv("MUYE_AGENT_CONTROL_TOKEN", "control-token")
    monkeypatch.setenv("MUYE_AGENT_DATA_TOKEN", "shared-token")
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(FixtureKnowledgeAgent=lambda: object()))
    import muye_multi_agent_sdk

    monkeypatch.setattr(muye_multi_agent_sdk, "create_app", lambda *_args, **_kwargs: object())
    specification = importlib.util.spec_from_file_location(
        "fixture_reused_internal_credentials",
        FIXTURE_DIRECTORY / "main.py",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)

    try:
        specification.loader.exec_module(module)
    except ValueError as exc:
        assert "互不相同" in str(exc)
    else:
        raise AssertionError("生成 Agent 必须拒绝复用的服务凭据")
