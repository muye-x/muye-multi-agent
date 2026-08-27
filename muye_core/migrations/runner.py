"""受控发现并应用 v3 PostgreSQL SQL 迁移。

迁移文件只允许位于本包的 ``sql/`` 目录，使用严格递增的 ``NNNN_description.sql``
命名。应用时会把版本和内容 checksum 写入 ``muye_schema_migrations``，从而拒绝
已部署迁移被改写。阶段 0 没有业务迁移；此模块为阶段 1 的数据库演进建立门禁。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any


_MIGRATION_FILENAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    """一份可审计 SQL 迁移的不可变元数据。"""

    version: int
    name: str
    path: Path
    checksum: str
    sql: str


def default_migration_directory() -> Path:
    """返回仓库内受控 v3 SQL 迁移目录。"""

    return Path(__file__).with_name("sql")


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """读取并校验迁移文件，拒绝未知 SQL、重复版本、空文件和版本空洞。"""

    migration_directory = (directory or default_migration_directory()).resolve()
    if not migration_directory.is_dir():
        raise ValueError(f"迁移目录不存在：{migration_directory}")
    migrations: list[Migration] = []
    for path in sorted(migration_directory.iterdir()):
        if path.name == "README.md":
            continue
        if not path.is_file():
            raise ValueError(f"迁移目录只能包含普通文件：{path.name}")
        if path.suffix != ".sql":
            raise ValueError(f"迁移目录包含未知文件：{path.name}")
        match = _MIGRATION_FILENAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"迁移文件必须命名为 NNNN_description.sql：{path.name}")
        source = path.read_text(encoding="utf-8")
        if not source.strip() or "\x00" in source:
            raise ValueError(f"迁移文件不能为空或包含 NUL：{path.name}")
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                path=path,
                checksum=sha256(source.encode("utf-8")).hexdigest(),
                sql=source,
            )
        )
    versions = [migration.version for migration in migrations]
    if len(set(versions)) != len(versions):
        raise ValueError("迁移版本不能重复")
    if versions and versions != list(range(1, len(versions) + 1)):
        raise ValueError("迁移版本必须从 0001 开始连续递增")
    return migrations


def render_plan(migrations: list[Migration]) -> str:
    """将迁移计划渲染为稳定 JSON，供 CI 和发布证据比较。"""

    payload = [
        {"version": f"{migration.version:04d}", "name": migration.name, "checksum": migration.checksum}
        for migration in migrations
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _connect(database_url: str) -> Any:
    """延迟导入 psycopg，便于无数据库的 CI 只执行计划校验。"""

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 由依赖声明保证生产环境存在
        raise RuntimeError("应用 v3 数据库迁移需要 psycopg") from exc
    return psycopg.connect(database_url, autocommit=False)


def apply_migrations(
    database_url: str,
    *,
    directory: Path | None = None,
    connect: Callable[[str], Any] = _connect,
) -> list[Migration]:
    """在单个事务中应用尚未执行的迁移并拒绝 checksum 漂移。

    调用方只能通过受控环境变量提供数据库 URL。该函数不会记录 URL，也不会自动
    回退迁移；任意 SQL 或数据库错误会回滚当前事务并向上抛出。
    """

    if not database_url.strip():
        raise ValueError("数据库 URL 不能为空")
    migrations = discover_migrations(directory)
    connection = connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS muye_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute("SELECT version, checksum FROM muye_schema_migrations ORDER BY version")
            applied = {int(version): str(checksum) for version, checksum in cursor.fetchall()}
            for migration in migrations:
                previous_checksum = applied.get(migration.version)
                if previous_checksum is not None:
                    if previous_checksum != migration.checksum:
                        raise ValueError(f"已应用迁移 checksum 漂移：{migration.version:04d}")
                    continue
                cursor.execute(migration.sql)
                cursor.execute(
                    "INSERT INTO muye_schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return migrations


def main() -> None:
    """提供无副作用的 plan 与显式 apply 两种 CLI 子命令。"""

    parser = argparse.ArgumentParser(description="Muye v3 PostgreSQL migration runner")
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--directory", type=Path, default=default_migration_directory())
    parser.add_argument("--database-url-env", default="MUYE_CORE_DATABASE_URL")
    arguments = parser.parse_args()
    if arguments.command == "plan":
        print(render_plan(discover_migrations(arguments.directory)), end="")
        return
    database_url = os.getenv(arguments.database_url_env, "")
    if not database_url:
        parser.error(f"环境变量 {arguments.database_url_env} 未配置")
    applied = apply_migrations(database_url, directory=arguments.directory)
    print(render_plan(applied), end="")


if __name__ == "__main__":
    main()
