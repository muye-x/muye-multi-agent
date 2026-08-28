"""Core 的生产 ASGI 工厂；只使用 v3 PostgreSQL 事实源。"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .api import create_app
from .postgres import PostgresCoreStore


def create_configured_app():
    """从受控环境变量构造生产 Core，缺失数据库配置即拒绝启动。"""

    database_url = os.environ.get("MUYE_CORE_DATABASE_URL", "").strip()
    artifact_root = os.environ.get("MUYE_CORE_ARTIFACT_ROOT", "/var/lib/muye/artifacts").strip()
    if not database_url or not artifact_root:
        raise ValueError("Core 需要 MUYE_CORE_DATABASE_URL 和 MUYE_CORE_ARTIFACT_ROOT")
    return create_app(store=PostgresCoreStore(database_url), artifact_root=Path(artifact_root))


if __name__ == "__main__":
    uvicorn.run("muye_core.main:create_configured_app", factory=True, host=os.environ.get("MUYE_CORE_HOST", "127.0.0.1"), port=int(os.environ.get("MUYE_CORE_PORT", "9870")))
