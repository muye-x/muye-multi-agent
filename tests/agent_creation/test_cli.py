"""`muye.sh agent prepare` 自动确认快捷入口的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agent_generator import cli as agent_cli
from tools.cli import main as cli_main


def test_prepare_auto_approves_current_plan_with_named_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """快捷入口只能使用刚刚生成的计划 checksum 和指定审核人创建 Agent。"""

    calls: list[tuple[str, object]] = []

    class FakeCreationService:
        def __init__(self, workspace_root: Path) -> None:
            assert workspace_root == tmp_path

        def prepare(self, project_directory: Path) -> SimpleNamespace:
            calls.append(("prepare", project_directory))
            return SimpleNamespace(plan_checksum="a" * 64, project_slug="hotel-employee")

        def create(self, project_directory: Path, *, plan_checksum: str, approved_by: str) -> dict[str, str]:
            calls.append(("create", (project_directory, plan_checksum, approved_by)))
            return {"directory": "agents/agent-hotel-employee", "status": "created"}

    monkeypatch.setattr(agent_cli, "AgentCreationService", FakeCreationService)

    assert cli_main(
        [
            "agent",
            "prepare",
            "agent-projects/hotel-employee",
            "--auto-approved-by",
            "release_reviewer",
        ],
        workspace_root=tmp_path,
    ) == 0

    output = json.loads(capsys.readouterr().out)
    project_directory = Path("agent-projects/hotel-employee")
    assert calls == [
        ("prepare", project_directory),
        ("create", (project_directory, "a" * 64, "release_reviewer")),
    ]
    assert output == {
        "approved_by": "release_reviewer",
        "dev": "not-requested",
        "plan": "config/generated/agent-creation-plans/hotel-employee/current.json",
        "plan_checksum": "a" * 64,
        "result": {"directory": "agents/agent-hotel-employee", "status": "created"},
        "status": "created-with-auto-approval",
    }


def test_prepare_dev_requires_auto_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """无效组合必须在创建服务、模型或 Milvus 调用前失败。"""

    monkeypatch.setattr(
        agent_cli,
        "AgentCreationService",
        lambda _: pytest.fail("参数校验失败时不应创建服务"),
    )

    assert cli_main(
        ["agent", "prepare", "agent-projects/hotel-employee", "--dev"],
        workspace_root=tmp_path,
    ) == 2
    assert "必须同时提供 --auto-approved-by" in capsys.readouterr().err


def test_dev_delegates_to_local_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agent dev` 必须复用独立本地联调生命周期，而非进入发布流程。"""

    calls: list[tuple[Path, str]] = []

    class FakeLifecycle:
        def __init__(self, workspace_root: Path) -> None:
            self.workspace_root = workspace_root

        def run(self, slug: str) -> int:
            calls.append((self.workspace_root, slug))
            return 0

    monkeypatch.setattr(agent_cli, "AgentDevLifecycle", FakeLifecycle)

    assert cli_main(["agent", "dev", "hotel-employee"], workspace_root=tmp_path) == 0
    assert calls == [(tmp_path, "hotel-employee")]


def test_prepare_dev_reuses_the_same_local_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一键生成后的联调入口必须与显式 `agent dev` 共用实现。"""

    class FakeCreationService:
        def __init__(self, _workspace_root: Path) -> None:
            pass

        def prepare(self, _project_directory: Path) -> SimpleNamespace:
            return SimpleNamespace(plan_checksum="a" * 64, project_slug="hotel-employee")

        def create(self, _project_directory: Path, *, plan_checksum: str, approved_by: str) -> dict[str, str]:
            assert plan_checksum == "a" * 64
            assert approved_by == "reviewer"
            return {"status": "created"}

    calls: list[str] = []

    class FakeLifecycle:
        def __init__(self, workspace_root: Path) -> None:
            assert workspace_root == tmp_path

        def run(self, slug: str) -> int:
            calls.append(slug)
            return 0

    monkeypatch.setattr(agent_cli, "AgentCreationService", FakeCreationService)
    monkeypatch.setattr(agent_cli, "AgentDevLifecycle", FakeLifecycle)

    assert cli_main(
        [
            "agent",
            "prepare",
            "agent-projects/hotel-employee",
            "--auto-approved-by",
            "reviewer",
            "--dev",
        ],
        workspace_root=tmp_path,
    ) == 0
    assert calls == ["hotel-employee"]
