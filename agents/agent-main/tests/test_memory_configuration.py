"""主 Agent 记忆功能开关的回归测试。"""
from __future__ import annotations

from types import SimpleNamespace

import asyncio

import config as main_config
import core.orchestrator as orchestrator_module
from config.settings import CatalogConfig, MemoryConfig, validate_config
from core.orchestrator import AgentManager


def test_memory_config_reads_global_switch(monkeypatch) -> None:
    """全局开关关闭时，不应装配记忆功能。"""
    monkeypatch.setenv("MEMORY_ENABLED", "false")

    config = MemoryConfig()

    assert config.enabled is False


def test_disabled_memory_does_not_require_mongodb_uri() -> None:
    """关闭全局记忆后，MongoDB 连接串不应成为启动前置条件。"""
    config = SimpleNamespace(
        llm=SimpleNamespace(api_base="http://127.0.0.1:9850"),
        checkpointer=SimpleNamespace(backend="memory", postgresql_uri=""),
        memory=SimpleNamespace(
            enabled=False,
            enable_mongodb=True,
            mongodb=SimpleNamespace(uri=""),
        ),
        catalog=CatalogConfig(),
    )

    validate_config(config)


def test_enabled_memory_requires_mongodb() -> None:
    """避免记忆更新队列在后台访问被禁用的 MongoDB。"""
    config = SimpleNamespace(
        llm=SimpleNamespace(api_base="http://127.0.0.1:9850"),
        checkpointer=SimpleNamespace(backend="memory", postgresql_uri=""),
        memory=SimpleNamespace(
            enabled=True,
            enable_mongodb=False,
            mongodb=SimpleNamespace(uri=""),
        ),
        catalog=CatalogConfig(),
    )

    try:
        validate_config(config)
    except ValueError as error:
        assert "MEMORY_ENABLED=true" in str(error)
    else:
        raise AssertionError("启用记忆且禁用 MongoDB 时必须拒绝配置")


def test_agent_manager_does_not_enable_memory_middleware_when_disabled(monkeypatch) -> None:
    """全局开关关闭时，装配器不得把记忆中间件交给主 Agent。"""
    observed: dict[str, object] = {}

    class FakeCheckpointerManager:
        def __init__(self, config) -> None:
            observed["checkpointer_config"] = config

        async def get(self, *, enabled: bool):
            observed["checkpointer_enabled"] = enabled
            return object()

    class FakeMainAgent:
        def __init__(self, *, enable_memory: bool, checkpointer, **kwargs) -> None:
            observed["enable_memory"] = enable_memory
            observed["checkpointer"] = checkpointer

    config = SimpleNamespace(
        memory=SimpleNamespace(enabled=False),
        checkpointer=SimpleNamespace(
            backend="memory",
            sqlite_path="conversations.db",
            postgresql_uri="",
            postgres_pool_size=1,
            postgres_max_overflow=0,
            postgres_pool_timeout=1.0,
            postgres_pool_recycle=1,
        ),
        catalog=CatalogConfig(),
    )
    monkeypatch.setattr(main_config, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator_module, "CheckpointerManager", FakeCheckpointerManager)
    monkeypatch.setattr(orchestrator_module, "MainAgentOrchestrator", FakeMainAgent)

    manager = AgentManager()
    asyncio.run(manager.initialize())

    assert observed["enable_memory"] is False
    assert manager.initialized is True
