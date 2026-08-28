"""JSON Schema 导出与 CI 漂移检查。"""

from __future__ import annotations

from pathlib import Path

from contracts.export_schemas import check_schemas, export_schemas, render_schemas


def test_schema_check_accepts_complete_generated_directory(tmp_path: Path) -> None:
    """完整导出后不应报告任何 Schema 漂移。"""

    export_schemas(tmp_path)

    assert check_schemas(tmp_path) == []


def test_schema_check_detects_missing_changed_and_unexpected_files(tmp_path: Path) -> None:
    """CI 必须同时发现漏提交、内容漂移和已废弃的 Schema。"""

    export_schemas(tmp_path)
    filenames = sorted(render_schemas())
    missing_filename, changed_filename = filenames[:2]
    (tmp_path / missing_filename).unlink()
    (tmp_path / changed_filename).write_text("{}\n", encoding="utf-8")
    unexpected_filename = "unexpected-v1.schema.json"
    (tmp_path / unexpected_filename).write_text("{}\n", encoding="utf-8")

    assert check_schemas(tmp_path) == [
        f"缺少 Schema：{missing_filename}",
        f"存在未声明 Schema：{unexpected_filename}",
        f"Schema 内容已漂移：{changed_filename}",
    ]
