"""阶段 0 的 v2 质量基线必须可离线重放并得到稳定报告。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.v3_baseline import build_report, load_baseline


FIXTURE_DIRECTORY = Path(__file__).with_name("fixtures")


def _read_json(path: Path) -> object:
    """读取已审阅 JSON fixture。"""

    return json.loads(path.read_text(encoding="utf-8"))


def test_v2_quality_baseline_report_is_deterministic() -> None:
    """三类资料结构的检索、citation、拒答、延迟和 token 基线可机器比较。"""

    baseline = load_baseline(FIXTURE_DIRECTORY / "v2-quality-baseline-input.json")

    assert build_report(baseline) == _read_json(
        FIXTURE_DIRECTORY / "v2-quality-baseline-report.json"
    )


def test_baseline_rejects_missing_pipeline_observation(tmp_path: Path) -> None:
    """缺少任一检索 pipeline 的记录不能成为发布质量基线。"""

    payload = _read_json(FIXTURE_DIRECTORY / "v2-quality-baseline-input.json")
    assert isinstance(payload, dict)
    cases = payload["cases"]
    assert isinstance(cases, list)
    first_case = cases[0]
    assert isinstance(first_case, dict)
    first_case["retrieval"] = first_case["retrieval"][:2]
    invalid_path = tmp_path / "invalid-baseline.json"
    invalid_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="dense、keyword、hybrid"):
        load_baseline(invalid_path)
