"""公开 SDK 源依赖声明的回归测试。"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SDK_DIRECT_REFERENCE = (
    "muye-multi-agent-sdk @ "
    "git+https://github.com/muye-x/muye-multi-agent-sdk.git@"
    "v2.1.0"
)


def test_requirements_use_the_pinned_public_sdk_release() -> None:
    """所有保留的服务 requirements 均应使用同一个公开 SDK 发布 tag。"""
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
