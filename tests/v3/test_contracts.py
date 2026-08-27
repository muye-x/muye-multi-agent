"""阶段 0 冻结的 v3.0 跨组件契约回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from contracts.v3 import (
    V3_CONTRACT_SCHEMA_MODELS,
    AgentBudgetV1,
    AgentEvaluationCaseV1,
    AgentEvaluationConfigV1,
    AgentModelConfigV1,
    AgentRetrievalConfigV1,
    AgentRevisionBundleManifestV1,
    AgentRevisionSpecV1,
    ChatStreamEventV1,
    JobEventV1,
    RevisionSourceAssetV1,
    RuntimeCapabilitiesV1,
    RuntimeCancelRequestV1,
    RuntimeCitationV1,
    RuntimeInvokeRequestV1,
    RuntimeInvokeResponseV1,
    RuntimeResourceBindingV1,
    revision_spec_checksum,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIRECTORY = PROJECT_ROOT / "contracts" / "fixtures"
SCHEMA_DIRECTORY = PROJECT_ROOT / "contracts" / "schemas"
TYPESCRIPT_DTO_PATH = PROJECT_ROOT / "contracts" / "typescript" / "agent-contracts-v3.ts"
TYPESCRIPT_INTERFACE_MODELS: dict[str, type[BaseModel]] = {
    "RevisionSourceAssetV1": RevisionSourceAssetV1,
    "AgentModelConfigV1": AgentModelConfigV1,
    "AgentRetrievalConfigV1": AgentRetrievalConfigV1,
    "AgentBudgetV1": AgentBudgetV1,
    "AgentEvaluationCaseV1": AgentEvaluationCaseV1,
    "AgentEvaluationConfigV1": AgentEvaluationConfigV1,
    "AgentRevisionSpecV1": AgentRevisionSpecV1,
    "RuntimeResourceBindingV1": RuntimeResourceBindingV1,
    "AgentRevisionBundleManifestV1": AgentRevisionBundleManifestV1,
    "JobEventV1": JobEventV1,
    "RuntimeCitationV1": RuntimeCitationV1,
    "RuntimeInvokeRequestV1": RuntimeInvokeRequestV1,
    "RuntimeInvokeResponseV1": RuntimeInvokeResponseV1,
    "RuntimeCancelRequestV1": RuntimeCancelRequestV1,
    "RuntimeCapabilitiesV1": RuntimeCapabilitiesV1,
    "ChatStreamEventV1": ChatStreamEventV1,
}


def _read_json(path: Path) -> Any:
    """读取固定 fixture，确保本组测试不依赖网络或模型。"""

    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("schema_name", sorted(V3_CONTRACT_SCHEMA_MODELS))
def test_valid_fixtures_match_v3_contract(schema_name: str) -> None:
    """每个 v3 公开 Schema 必须有一个严格通过的正例。"""

    payload = _read_json(FIXTURE_DIRECTORY / f"{schema_name}.valid.json")

    instance = V3_CONTRACT_SCHEMA_MODELS[schema_name].model_validate(payload)

    assert instance.schema_version.endswith("/v1")


@pytest.mark.parametrize(
    ("schema_name", "mutate", "error_fragment"),
    [
        (
            "agent-revision-v1",
            lambda payload: payload["evaluation"]["cases"][0].update(
                {"expected_source_asset_ids": ["asset_ffffffffffffffff"]}
            ),
            "不属于本 Revision",
        ),
        (
            "agent-revision-bundle-v1",
            lambda payload: payload.update(
                {
                    "resources": [
                        payload["resources"][0],
                        dict(payload["resources"][0]),
                    ]
                }
            ),
            "resource_id 不能重复",
        ),
        (
            "job-event-v1",
            lambda payload: payload.update({"event_type": "failed", "error_code": None}),
            "failed 事件必须包含 error_code",
        ),
        (
            "runtime-invoke-response-v1",
            lambda payload: payload.update({"status": "success", "error_code": "INTERNAL_ERROR"}),
            "success 响应必须包含 content",
        ),
        (
            "runtime-invoke-response-v1",
            lambda payload: payload.update(
                {"status": "refused", "error_code": "NO_EVIDENCE", "error_message": "资料不足"}
            ),
            "refused/error 响应不能包含 content 或 citations",
        ),
        (
            "chat-stream-event-v1",
            lambda payload: payload.update({"delta": None}),
            "block_delta 必须包含 block_id 和 delta",
        ),
    ],
)
def test_v3_contracts_reject_invalid_cross_boundary_payloads(
    schema_name: str,
    mutate: Any,
    error_fragment: str,
) -> None:
    """资料越权、重复资源与不完整流事件必须在边界被拒绝。"""

    payload = _read_json(FIXTURE_DIRECTORY / f"{schema_name}.valid.json")
    mutate(payload)

    with pytest.raises(ValidationError, match=error_fragment):
        V3_CONTRACT_SCHEMA_MODELS[schema_name].model_validate(payload)


@pytest.mark.parametrize("schema_name", sorted(V3_CONTRACT_SCHEMA_MODELS))
def test_checked_in_v3_json_schema_matches_pydantic_contract(schema_name: str) -> None:
    """JSON Schema 必须由当前冻结的 Pydantic 模型确定性生成。"""

    expected = V3_CONTRACT_SCHEMA_MODELS[schema_name].model_json_schema()
    expected["$id"] = f"https://muye.ai/schemas/{schema_name}.schema.json"

    assert _read_json(SCHEMA_DIRECTORY / f"{schema_name}.schema.json") == expected


def _typescript_interface_fields(source: str) -> dict[str, dict[str, bool]]:
    """提取 TypeScript DTO 字段及可选性，无需 Node 参与契约门禁。"""

    interfaces: dict[str, dict[str, bool]] = {}
    interface_pattern = re.compile(
        r"^export interface (?P<name>[A-Za-z][A-Za-z0-9_]*) \{\n(?P<body>.*?)^\}",
        re.MULTILINE | re.DOTALL,
    )
    field_pattern = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<optional>\?)?:")
    for match in interface_pattern.finditer(source):
        fields: dict[str, bool] = {}
        for line in match.group("body").splitlines():
            field_match = field_pattern.match(line.strip())
            if field_match:
                fields[field_match.group("name")] = bool(field_match.group("optional"))
        interfaces[match.group("name")] = fields
    return interfaces


def test_v3_typescript_dto_fields_match_pydantic_contracts() -> None:
    """Web/Runtime 使用的 TypeScript 字段与 Python 契约同步。"""

    interfaces = _typescript_interface_fields(TYPESCRIPT_DTO_PATH.read_text(encoding="utf-8"))
    for interface_name, model in TYPESCRIPT_INTERFACE_MODELS.items():
        schema = model.model_json_schema()
        expected_fields = set(schema["properties"])
        expected_required = set(schema.get("required", []))

        assert interface_name in interfaces
        actual_fields = interfaces[interface_name]
        actual_required = {name for name, optional in actual_fields.items() if not optional}
        assert set(actual_fields) == expected_fields
        assert actual_required == expected_required


def test_revision_checksum_is_stable_and_covers_all_frozen_inputs() -> None:
    """同一 Spec 的 checksum 稳定，资料变化必须产生新 Revision 身份。"""

    payload = _read_json(FIXTURE_DIRECTORY / "agent-revision-v1.valid.json")
    original = AgentRevisionSpecV1.model_validate(payload)
    changed_payload = _read_json(FIXTURE_DIRECTORY / "agent-revision-v1.valid.json")
    changed_payload["source_assets"][1]["sha256"] = "f" * 64
    changed = AgentRevisionSpecV1.model_validate(changed_payload)

    assert revision_spec_checksum(original) == revision_spec_checksum(
        AgentRevisionSpecV1.model_validate(payload)
    )
    assert revision_spec_checksum(original) != revision_spec_checksum(changed)
