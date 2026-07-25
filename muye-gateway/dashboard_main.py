"""Muye Gateway 运维控制台本地启动入口。"""

from __future__ import annotations

import os

import uvicorn

from dashboard_api.app import app


def main() -> None:
    """将控制台绑定到回环地址，供根启动器或 Nginx 使用。"""
    uvicorn.run(
        app,
        host=os.getenv("MUYE_DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("MUYE_DASHBOARD_PORT", "9870")),
        log_level=os.getenv("MUYE_DASHBOARD_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
