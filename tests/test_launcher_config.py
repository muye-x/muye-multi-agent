"""根启动器配置预检的回归测试。"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values

import main as launcher


def _complete_llm_environment() -> dict[str, str]:
    """构造不包含真实凭据的完整测试配置。"""
    return {
        "MUYE_LLM_API_KEY": "test-chat-key",
        "MUYE_LLM_EMBED_API_KEY": "test-embed-key",
        "MUYE_LLM_API_BASE_URL": "https://llm.example.test/v1",
        "MUYE_LLM_EMBED_API_BASE_URL": "https://embed.example.test/v1",
        "MUYE_LLM_EMBED_DEFAULT_MODEL": "test-embedding",
        "MUYE_LLM_EMBED_MODELS_JSON": (
            '[{"id":"test-embedding","name":"Test Embedding",'
            '"provider_model":"provider-embedding","dimensions":3}]'
        ),
        "MUYE_LLM_DEFAULT_MODEL": "test-model",
        "MUYE_LLM_MODEL": "test-model",
        "MUYE_LLM_MODELS_JSON": (
            '[{"id":"test-model","name":"Test Model",'
            '"provider_model":"provider-model","supports_thinking":false}]'
        ),
    }


def test_validate_llm_environment_reports_missing_keys() -> None:
    errors = launcher.validate_llm_environment({})

    assert any("MUYE_LLM_API_KEY 未配置" in error for error in errors)
    assert any("MUYE_LLM_EMBED_API_KEY 未配置" in error for error in errors)


def test_validate_llm_environment_accepts_complete_configuration() -> None:
    assert launcher.validate_llm_environment(_complete_llm_environment()) == []


def test_validate_llm_environment_rejects_invalid_model_registry() -> None:
    values = _complete_llm_environment()
    values["MUYE_LLM_MODELS_JSON"] = "not-json"

    errors = launcher.validate_llm_environment(values)

    assert "MUYE_LLM_MODELS_JSON 必须是合法的 JSON array" in errors


def test_validate_llm_environment_rejects_unregistered_agent_model() -> None:
    values = _complete_llm_environment()
    values["MUYE_LLM_MODEL"] = "unregistered-model"

    errors = launcher.validate_llm_environment(values)

    assert "MUYE_LLM_MODEL 必须存在于 MUYE_LLM_MODELS_JSON" in errors


def test_validate_llm_environment_rejects_example_placeholders() -> None:
    values = _complete_llm_environment()
    values["MUYE_LLM_API_BASE_URL"] = (
        "https://your-openai-compatible-endpoint.example/v1"
    )
    values["MUYE_LLM_DEFAULT_MODEL"] = "your-model-alias"

    errors = launcher.validate_llm_environment(values)

    assert any("占位地址" in error for error in errors)
    assert any("占位模型" in error for error in errors)


def test_validate_llm_environment_handles_malformed_url() -> None:
    values = _complete_llm_environment()
    values["MUYE_LLM_API_BASE_URL"] = "http://[invalid"

    errors = launcher.validate_llm_environment(values)

    assert "MUYE_LLM_API_BASE_URL 必须是有效的 HTTP(S) URL" in errors


def test_load_runtime_environment_keeps_shell_priority(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MUYE_LLM_API_KEY=file-key\nMUYE_LLM_DEFAULT_MODEL=file-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MUYE_LLM_API_KEY", "shell-key")
    monkeypatch.delenv("MUYE_LLM_DEFAULT_MODEL", raising=False)

    launcher.load_runtime_environment(env_file)

    assert launcher.os.environ["MUYE_LLM_API_KEY"] == "shell-key"
    assert launcher.os.environ["MUYE_LLM_DEFAULT_MODEL"] == "file-model"


def test_read_llm_environment_preserves_legacy_positional_argument_order(
    tmp_path: Path,
) -> None:
    """第二个位置参数必须继续表示主 Agent 配置，而不是 muye-data 配置。"""
    llm_env = tmp_path / "llm.env"
    agent_main_env = tmp_path / "agent-main.env"
    data_env = tmp_path / "data.env"
    llm_env.write_text("SHARED_VALUE=llm\n", encoding="utf-8")
    agent_main_env.write_text("SHARED_VALUE=agent-main\n", encoding="utf-8")
    data_env.write_text("SHARED_VALUE=muye-data\n", encoding="utf-8")

    legacy_values = launcher.read_llm_environment(llm_env, agent_main_env)
    values_with_data = launcher.read_llm_environment(
        llm_env,
        agent_main_env,
        data_env_file=data_env,
    )

    assert legacy_values["SHARED_VALUE"] == "agent-main"
    assert values_with_data["SHARED_VALUE"] == "agent-main"


def test_main_stops_before_starting_services_when_configuration_is_missing(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(launcher, "load_runtime_environment", lambda: None)
    monkeypatch.setattr(launcher, "read_llm_environment", lambda: {})

    def fail_if_started(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("配置不完整时不应启动子进程")

    monkeypatch.setattr(launcher, "start_service", fail_if_started)

    launcher.main()

    output = capsys.readouterr().out
    assert "启动前配置检查未通过" in output
    assert "尚未启动任何服务" in output
    assert ".env.example" in output


def test_each_service_provides_safe_environment_example() -> None:
    project_root = Path(__file__).resolve().parents[1]
    examples = [
        project_root / ".env.example",
        project_root / "muye-llm" / ".env.example",
        project_root / "muye-data" / ".env.example",
        project_root / "agents" / "agent-main" / ".env.example",
        project_root / "muye-gateway" / ".env.example",
    ]
    sensitive_names = {
        "MUYE_LLM_API_KEY",
        "MUYE_LLM_EMBED_API_KEY",
        "MUYE_LLM_RERANK_API_KEY",
        "MUYE_DATA_MILVUS_TOKEN",
        "LANGSMITH_API_KEY",
        "LANGSEARCH_API_KEY",
        "TAVILY_API_KEY",
        "INFOQUEST_API_KEY",
        "SERPER_API_KEY",
        "JINA_API_KEY",
    }

    for example in examples:
        assert example.is_file(), f"缺少配置模板: {example}"
        values = dotenv_values(example)
        for name in sensitive_names & values.keys():
            assert not values[name], f"{example} 中的 {name} 必须留空"


def test_fixed_business_agent_directories_and_configuration_are_removed() -> None:
    """v2.0 只能从受审计 Catalog 发现 SubAgent，不能保留固定业务入口。"""
    project_root = Path(__file__).resolve().parents[1]
    assert not (project_root / "agents" / "agent-travel").exists()
    assert not (project_root / "agents" / "agent-order").exists()
    runtime_sources = (
        project_root / "main.py",
        project_root / ".env.example",
        project_root / "muye-gateway" / ".env.example",
    )
    forbidden = ("MUYE_AGENT_TRAVEL", "MUYE_AGENT_ORDER", "agent-travel", "agent-order")
    for source in runtime_sources:
        content = source.read_text(encoding="utf-8")
        assert not any(value in content for value in forbidden), source


def test_data_service_is_disabled_by_default() -> None:
    services = launcher.enabled_services({})

    assert "muye-data" not in {service["cwd"] for service in services}
    assert launcher.validate_data_environment({}) == []


def test_data_service_requires_existing_config_when_enabled(tmp_path: Path) -> None:
    errors = launcher.validate_data_environment(
        {"MUYE_DATA_ENABLED": "true", "MUYE_DATA_CONFIG_PATH": "missing.yaml"},
        project_root=tmp_path,
    )

    assert any("文件不存在" in error for error in errors)


def test_data_service_accepts_enabled_local_config(tmp_path: Path) -> None:
    data_directory = tmp_path / "muye-data"
    data_directory.mkdir()
    (data_directory / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    values = {
        "MUYE_DATA_ENABLED": "true",
        "MUYE_DATA_CONFIG_PATH": "config.yaml",
        "MUYE_DATA_LLM_BASE_URL": "http://muye-llm.test:9850",
    }

    assert launcher.validate_data_environment(values, project_root=tmp_path) == []
    assert "muye-data" in {service["cwd"] for service in launcher.enabled_services(values)}
