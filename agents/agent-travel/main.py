"""Travel 参考服务 ASGI 入口。"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

from muye_multi_agent_sdk import AgentConfig, create_app

from travel_agent import TravelAgent


def _has_setting(name: str, env_file: Path | str | None) -> bool:
    """判断配置是否由进程环境或本地 env 文件显式提供。"""
    if name in os.environ:
        return True
    return bool(env_file and Path(env_file).is_file() and name in dotenv_values(env_file))


def build_config(env_file: Path | str | None = Path(".env")) -> AgentConfig:
    """加载 SDK 配置，并应用 Travel 服务公开契约的默认 profile。"""
    config = AgentConfig.from_env(env_file)
    if not _has_setting("MUYE_SDK_API_PROFILES", env_file):
        config.api.profiles = {"internal", "public"}
    if config.api.public_path is None:
        config.api.public_path = "/api/v1/travel"
    return config


config = build_config()
app = create_app(TravelAgent(config))

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("MUYE_AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("MUYE_AGENT_PORT", "8011")),
    )
