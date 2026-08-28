"""显式创建 v3 Core 首个管理员，不能通过公开 HTTP 调用替代。"""

from __future__ import annotations

import os

from .postgres import PostgresCoreStore


def main() -> int:
    database_url = os.environ.get("MUYE_CORE_DATABASE_URL", "").strip()
    username = os.environ.get("MUYE_CORE_BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.environ.get("MUYE_CORE_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not database_url or not username or not password:
        raise ValueError("初始化需要 MUYE_CORE_DATABASE_URL、MUYE_CORE_BOOTSTRAP_ADMIN_USERNAME 和 MUYE_CORE_BOOTSTRAP_ADMIN_PASSWORD")
    PostgresCoreStore(database_url).bootstrap_admin(username, password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
