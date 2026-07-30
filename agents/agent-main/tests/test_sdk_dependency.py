"""公开 SDK 源依赖声明的回归测试。"""

from __future__ import annotations

import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SDK_DIRECT_REFERENCE = (
    "muye-multi-agent-sdk @ "
    "git+https://github.com/muye-x/muye-multi-agent-sdk.git@"
    "v2.0.0"
)


def test_requirements_use_the_pinned_public_sdk_release() -> None:
    """所有可独立安装的 requirements 均应使用同一个公开 SDK 发布 tag。"""
    paths = (
        PROJECT_ROOT / "requirements.txt",
        PROJECT_ROOT / "agents" / "agent-main" / "requirements.txt",
        PROJECT_ROOT / "agents" / "agent-travel" / "requirements.txt",
        PROJECT_ROOT / "agents" / "agent-order" / "requirements.txt",
    )

    for path in paths:
        declarations = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("muye-multi-agent-sdk")
        }
        assert declarations == {SDK_DIRECT_REFERENCE}, path


def test_packaged_agents_use_the_pinned_public_sdk_release() -> None:
    """两个可打包子 Agent 的项目元数据应与 requirements 使用同一 SDK 发布 tag。"""
    paths = (
        PROJECT_ROOT / "agents" / "agent-travel" / "pyproject.toml",
        PROJECT_ROOT / "agents" / "agent-order" / "pyproject.toml",
    )

    for path in paths:
        metadata = tomllib.loads(path.read_text(encoding="utf-8"))
        assert SDK_DIRECT_REFERENCE in metadata["project"]["dependencies"], path
