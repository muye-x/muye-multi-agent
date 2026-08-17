"""公开 SDK 源依赖声明的回归测试。"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SDK_DIRECT_REFERENCE = (
    "muye-multi-agent-sdk @ "
    "git+https://github.com/muye-x/muye-multi-agent-sdk.git@"
    "main"
)


def test_requirements_use_the_sdk_main_branch() -> None:
    """所有服务 requirements 均应使用 SDK main 分支。"""
    paths = (
        PROJECT_ROOT / "requirements.txt",
        PROJECT_ROOT / "agents" / "agent-main" / "requirements.txt",
    )

    for path in paths:
        declarations = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("muye-multi-agent-sdk")
        }
        assert declarations == {SDK_DIRECT_REFERENCE}, path
