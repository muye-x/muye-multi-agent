"""v2.0 模板 Agent 核心契约的正反例与 JSON Schema 回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from contracts.models import CONTRACT_SCHEMA_MODELS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIRECTORY = PROJECT_ROOT / "contracts" / "fixtures"
SCHEMA_DIRECTORY = PROJECT_ROOT / "contracts" / "schemas"
TYPESCRIPT_DTO_PATH = PROJECT_ROOT / "contracts" / "typescript" / "agent-contracts-v1.ts"


def _read_json(path: Path) -> Any:
    """读取版本化 JSON fixture，避免测试依赖外部服务或模型。"""
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("schema_name", sorted(CONTRACT_SCHEMA_MODELS))
def test_valid_fixtures_match_their_contract(schema_name: str) -> None:
    """每个公开契约都必须有一个可被严格模型接受的正例。"""
    payload = _read_json(FIXTURE_DIRECTORY / f"{schema_name}.valid.json")

    instance = CONTRACT_SCHEMA_MODELS[schema_name].model_validate(payload)

    assert instance.schema_version.endswith("/v1")


@pytest.mark.parametrize("case", _read_json(FIXTURE_DIRECTORY / "invalid-cases-v1.json"))
def test_invalid_fixtures_are_rejected(case: dict[str, Any]) -> None:
    """网络地址、路径遍历、浮动镜像和不受支持 profile 都必须在边界拒绝。"""
    schema_name = case["model"]
    payload = _read_json(FIXTURE_DIRECTORY / f"{schema_name}.valid.json")
    payload.update(case["patch"])

    with pytest.raises(ValidationError) as error:
        CONTRACT_SCHEMA_MODELS[schema_name].model_validate(payload)

    assert case["error_field"] in str(error.value)


@pytest.mark.parametrize("schema_name", sorted(CONTRACT_SCHEMA_MODELS))
def test_checked_in_json_schema_matches_pydantic_contract(schema_name: str) -> None:
    """JSON Schema 与 Python 校验模型必须由同一版本同步生成。"""
    expected = CONTRACT_SCHEMA_MODELS[schema_name].model_json_schema()
    expected["$id"] = f"https://muye.ai/schemas/{schema_name}.schema.json"

    actual = _read_json(SCHEMA_DIRECTORY / f"{schema_name}.schema.json")

    assert actual == expected


def test_typescript_dto_declares_all_public_contracts() -> None:
    """尚未引入 Web 工程前，至少防止 TypeScript 声明遗漏任一公开契约。"""
    source = TYPESCRIPT_DTO_PATH.read_text(encoding="utf-8")

    for contract_name in (
        "AgentDescriptorV1",
        "AgentGenerationSpecV1",
        "SourceProvenanceV1",
        "AgentBuildRecordV1",
        "AgentCatalogSnapshotV1",
    ):
        assert f"export interface {contract_name}" in source
