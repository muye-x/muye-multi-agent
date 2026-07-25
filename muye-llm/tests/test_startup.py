"""LLM 服务直接启动时的配置预检测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_service_main() -> ModuleType:
    """使用独立模块名加载服务入口，避免与根启动器同名冲突。"""
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("muye_llm_service_main", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service_main = _load_service_main()


def test_validate_startup_configuration_reports_both_missing_keys(monkeypatch) -> None:
    monkeypatch.setattr(service_main.settings, "llm_api_key", "")
    monkeypatch.setattr(service_main.settings, "embed_api_key", "")

    errors = service_main.validate_startup_configuration()

    assert any("MUYE_LLM_API_KEY 未配置" in error for error in errors)
    assert any("MUYE_LLM_EMBED_API_KEY 未配置" in error for error in errors)


def test_main_does_not_call_uvicorn_when_configuration_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(service_main.settings, "llm_api_key", "")
    monkeypatch.setattr(service_main.settings, "embed_api_key", "")

    def fail_if_started(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("配置不完整时不应启动 Uvicorn")

    monkeypatch.setattr(service_main.uvicorn, "run", fail_if_started)

    service_main.main()
