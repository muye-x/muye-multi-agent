"""独立 Python 服务依赖清单的回归测试。"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_each_independently_runnable_python_service_has_requirements_file() -> None:
    """Compose 中的 Python 服务及生成 Agent 模板必须能独立安装运行依赖。"""

    requirement_files = (
        "control_server/requirements.txt",
        "muye-gateway/requirements.txt",
        "agents/agent-main/requirements.txt",
        "muye-llm/requirements.txt",
        "muye-data/requirements.txt",
        "templates/agents/react-knowledge/v1/requirements.txt.tmpl",
    )

    for relative_path in requirement_files:
        path = PROJECT_ROOT / relative_path
        assert path.is_file(), f"missing independent service requirements: {relative_path}"
        assert path.read_text(encoding="utf-8").strip(), (
            f"empty independent service requirements: {relative_path}"
        )


def test_root_requirements_aggregates_all_core_python_services() -> None:
    """根镜像安装入口必须包含 Compose 中所有 Python 服务的依赖。"""

    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")

    for relative_path in (
        "control_server/requirements.txt",
        "muye-gateway/requirements.txt",
        "agents/agent-main/requirements.txt",
        "muye-llm/requirements.txt",
        "muye-data/requirements.txt",
    ):
        assert f"-r ./{relative_path}" in requirements
