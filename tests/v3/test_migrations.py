"""阶段 0 v3 迁移工具的无数据库行为测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from muye_core.migrations.runner import apply_migrations, discover_migrations, render_plan


def _write_migration(directory: Path, name: str, source: str = "SELECT 1;\n") -> None:
    """写入临时 SQL 迁移 fixture。"""

    (directory / name).write_text(source, encoding="utf-8")


class _FakeCursor:
    """最小 PostgreSQL cursor fake，记录迁移 SQL 与迁移账本。"""

    def __init__(self, applied: dict[int, str], statements: list[str]) -> None:
        self._applied = applied
        self._statements = statements
        self._rows: list[tuple[int, str]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[object, ...] | None = None) -> None:
        normalized = " ".join(statement.split())
        self._statements.append(normalized)
        if normalized.startswith("SELECT version, checksum"):
            self._rows = sorted(self._applied.items())
        elif normalized.startswith("INSERT INTO muye_schema_migrations"):
            assert parameters is not None
            version, _name, checksum = parameters
            self._applied[int(version)] = str(checksum)

    def fetchall(self) -> list[tuple[int, str]]:
        return self._rows


class _FakeConnection:
    """验证 commit、rollback 和 close 的无网络连接 fake。"""

    def __init__(self) -> None:
        self.applied: dict[int, str] = {}
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.applied, self.statements)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_v3_migration_plan_contains_core_and_knowledge_job_schema() -> None:
    """Core 与阶段 2 知识 Job 迁移必须可被确定性发现。"""

    migrations = discover_migrations()

    assert [(migration.version, migration.name) for migration in migrations] == [
        (1, "phase1_core"),
        (2, "phase1_constraints"),
        (3, "phase2_knowledge_jobs"),
    ]


def test_migrations_are_ordered_and_checksums_are_stable(tmp_path: Path) -> None:
    """连续版本产生排序稳定、内容可审计的计划。"""

    _write_migration(tmp_path, "0001_create_agents.sql", "CREATE TABLE agents ();\n")
    _write_migration(tmp_path, "0002_add_indexes.sql", "CREATE INDEX agents_id_idx ON agents ();\n")

    migrations = discover_migrations(tmp_path)

    assert [migration.version for migration in migrations] == [1, 2]
    assert migrations[0].checksum != migrations[1].checksum
    assert '"version": "0001"' in render_plan(migrations)


def test_apply_migrations_records_checksum_and_rejects_drift(tmp_path: Path) -> None:
    """应用迁移后，修改同一版本内容必须回滚并阻断继续执行。"""

    migration_path = tmp_path / "0001_create_agents.sql"
    _write_migration(tmp_path, migration_path.name, "CREATE TABLE agents ();\n")
    connection = _FakeConnection()

    applied = apply_migrations(
        "postgresql://phase-zero-test",
        directory=tmp_path,
        connect=lambda _database_url: connection,
    )

    assert [migration.version for migration in applied] == [1]
    assert connection.committed is True
    assert connection.closed is True
    assert connection.applied[1] == applied[0].checksum
    assert any(statement == "CREATE TABLE agents ();" for statement in connection.statements)

    migration_path.write_text("CREATE TABLE agents (id INTEGER);\n", encoding="utf-8")
    drift_connection = _FakeConnection()
    drift_connection.applied = dict(connection.applied)

    with pytest.raises(ValueError, match="checksum 漂移"):
        apply_migrations(
            "postgresql://phase-zero-test",
            directory=tmp_path,
            connect=lambda _database_url: drift_connection,
        )

    assert drift_connection.rolled_back is True
    assert drift_connection.closed is True


@pytest.mark.parametrize("name", ["0000_invalid.sql", "0002_skip_first.sql", "1_bad.sql", "0001-bad.sql"])
def test_migration_discovery_rejects_unsafe_or_non_contiguous_names(tmp_path: Path, name: str) -> None:
    """迁移序列不能出现空洞、非规范命名或未审阅 SQL 文件。"""

    _write_migration(tmp_path, name)

    with pytest.raises(ValueError):
        discover_migrations(tmp_path)
