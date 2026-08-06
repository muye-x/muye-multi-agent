"""根启动器的模块化环境边界回归测试。"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values
import pytest

import main as launcher


def test_enabled_services_reads_only_data_modules_local_flag(tmp_path: Path, monkeypatch) -> None:
    """可选 Data 服务只由自己的 `.env` 控制，根目录配置不参与决策。"""

    data_directory = tmp_path / "muye-data"
    data_directory.mkdir()
    (data_directory / ".env").write_text("MUYE_DATA_ENABLED=true\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MUYE_DATA_ENABLED", raising=False)

    services = launcher.enabled_services()

    assert "muye-data" in {service["cwd"] for service in services}


def test_shell_can_explicitly_override_data_modules_local_flag(tmp_path: Path, monkeypatch) -> None:
    """shell override 保留，但只覆盖目标模块自己的开关。"""

    data_directory = tmp_path / "muye-data"
    data_directory.mkdir()
    (data_directory / ".env").write_text("MUYE_DATA_ENABLED=true\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("MUYE_DATA_ENABLED", "false")

    assert "muye-data" not in {service["cwd"] for service in launcher.enabled_services()}


def test_service_runtime_address_uses_modules_local_port(tmp_path: Path, monkeypatch) -> None:
    """根启动器应探测模块实际配置的端口，而不是使用历史固定值。"""

    module_directory = tmp_path / "muye-llm"
    module_directory.mkdir()
    (module_directory / ".env").write_text(
        "MUYE_LLM_HOST=0.0.0.0\nMUYE_LLM_PORT=9950\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MUYE_LLM_HOST", raising=False)
    monkeypatch.delenv("MUYE_LLM_PORT", raising=False)

    service = next(item for item in launcher.SERVICES if item["cwd"] == "muye-llm")

    assert launcher.service_runtime_address(service) == (
        "0.0.0.0",
        9950,
        "http://127.0.0.1:9950/health",
    )


def test_service_runtime_address_rejects_invalid_port(tmp_path: Path, monkeypatch) -> None:
    module_directory = tmp_path / "muye-llm"
    module_directory.mkdir()
    (module_directory / ".env").write_text("MUYE_LLM_PORT=70000\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MUYE_LLM_PORT", raising=False)
    service = next(item for item in launcher.SERVICES if item["cwd"] == "muye-llm")

    try:
        launcher.service_runtime_address(service)
    except ValueError as exc:
        assert "MUYE_LLM_PORT" in str(exc)
    else:
        raise AssertionError("非法端口必须被拒绝")


def test_main_starts_services_without_reading_root_environment(monkeypatch, tmp_path: Path) -> None:
    """根启动器只编排服务，不再加载根 `.env` 或检查模块密钥。"""

    (tmp_path / ".env").write_text("MUYE_LLM_API_KEY=should-not-be-loaded\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["main.py", "--dry-run"])
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MUYE_LLM_API_KEY", raising=False)
    monkeypatch.setattr(launcher, "dry_run", lambda services: None)

    launcher.main()

    assert "MUYE_LLM_API_KEY" not in launcher.os.environ


def test_start_service_exposes_workspace_contract_modules_to_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务本地包与根目录 ``contracts`` 必须同时可导入。"""

    module_directory = tmp_path / "muye-data"
    module_directory.mkdir()
    captured: dict[str, object] = {}

    class _Process:
        stdout = None

        def poll(self) -> None:
            return None

    class _Thread:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    def _popen(*_args: object, **kwargs: object) -> _Process:
        captured.update(kwargs)
        return _Process()

    service = {
        "name": "muye-data",
        "cwd": "muye-data",
        "cmd": ["python", "main.py"],
        "host_env": "MUYE_DATA_HOST",
        "port_env": "MUYE_DATA_PORT",
        "default_port": 9840,
    }
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_is_port_in_use", lambda _port: False)
    monkeypatch.setattr(launcher, "wait_for_healthy", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher.subprocess, "Popen", _popen)
    monkeypatch.setattr(launcher.threading, "Thread", _Thread)
    launcher._started_procs.clear()

    assert launcher.start_service(service, health_timeout=1) is not None
    environment = captured["env"]
    assert isinstance(environment, dict)
    paths = environment["PYTHONPATH"].split(launcher.os.pathsep)
    assert paths[:2] == [str(module_directory), str(tmp_path)]


def test_each_module_provides_safe_environment_example() -> None:
    """每个运行模块和创建工具都有独立、无密钥的配置模板。"""

    project_root = Path(__file__).resolve().parents[1]
    examples = [
        project_root / "muye-llm" / ".env.example",
        project_root / "muye-data" / ".env.example",
        project_root / "agents" / "agent-main" / ".env.example",
        project_root / "control_server" / ".env.example",
        project_root / "muye-gateway" / ".env.example",
        project_root / "tools" / "agent_catalog" / ".env.example",
    ]
    sensitive_names = {
        "MUYE_LLM_API_KEY",
        "MUYE_LLM_EMBED_API_KEY",
        "MUYE_LLM_RERANK_API_KEY",
        "MUYE_DATA_MILVUS_TOKEN",
        "MUYE_KNOWLEDGE_MILVUS_TOKEN",
        "MUYE_CONTROL_OPERATOR_TOKEN",
        "MUYE_CONTROL_MAIN_TOKEN",
        "MUYE_CONTROL_HEALTH_TOKEN",
        "MUYE_CONTROL_GATEWAY_TOKEN",
        "POSTGRES_PASSWORD",
        "LANGSMITH_API_KEY",
    }

    assert not (project_root / ".env.example").exists()
    assert dotenv_values(project_root / "muye-data" / ".env.example")["MUYE_DATA_ENABLED"] == "false"
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
        project_root / "muye-gateway" / ".env.example",
    )
    forbidden = ("MUYE_AGENT_TRAVEL", "MUYE_AGENT_ORDER", "agent-travel", "agent-order")
    for source in runtime_sources:
        content = source.read_text(encoding="utf-8")
        assert not any(value in content for value in forbidden), source
