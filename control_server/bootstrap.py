"""显式初始化 Control PostgreSQL schema 与首个管理员。

该入口只由 ``scripts/muye.sh init`` 调用。服务进程启动不会读取管理员密码，避免
重启时重复创建账号或把一次性初始化凭据留在长期运行环境中。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from .identity import PostgresIdentityStore


load_dotenv(Path(__file__).resolve().parent / ".env")


def main() -> int:
    """从显式环境变量创建唯一初始管理员；缺失配置即失败。"""
    database_url = os.environ.get("MUYE_CONTROL_DATABASE_URL", "").strip()
    username = os.environ.get("MUYE_CONTROL_BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.environ.get("MUYE_CONTROL_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not database_url or not username or not password:
        raise ValueError("初始化需要 MUYE_CONTROL_DATABASE_URL、用户名和密码")
    store = PostgresIdentityStore(database_url)
    store.initialize()
    store.bootstrap_admin(username=username, password=password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
