"""v2.0 模板 Agent 核心契约的正反例与 JSON Schema 回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from contracts.local_dev import LocalDevAgentV1, build_local_dev_registration
from contracts.models import (
    CONTRACT_SCHEMA_MODELS,
    AgentBuildRecordV1,
    AgentCatalogEntryV1,
    AgentCatalogSnapshotV1,
    AgentDeploymentV1,
    AgentDescriptorV1,
    AgentGenerationSpecV1,
    AgentRuntimeV1,
    AgentSourceV1,
    CollectionIndexPlanV1,
    EvaluationSetV1,
    KnowledgeJobV1,
    KnowledgeResourceManifestV1,
    ParsedDocumentV1,
    ResourceBindingV1,
    ResourceSnapshotV1,
    SchemaProposalV1,
    SourceProvenanceV1,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIRECTORY = PROJECT_ROOT / "contracts" / "fixtures"
SCHEMA_DIRECTORY = PROJECT_ROOT / "contracts" / "schemas"
TYPESCRIPT_DTO_PATH = PROJECT_ROOT / "contracts" / "typescript" / "agent-contracts-v1.ts"
TYPESCRIPT_INTERFACE_MODELS: dict[str, type[BaseModel]] = {
    "ResourceBindingV1": ResourceBindingV1,
    "AgentRuntimeV1": AgentRuntimeV1,
    "AgentDeploymentV1": AgentDeploymentV1,
    "AgentSourceV1": AgentSourceV1,
    "AgentDescriptorV1": AgentDescriptorV1,
    "AgentGenerationSpecV1": AgentGenerationSpecV1,
    "SourceProvenanceV1": SourceProvenanceV1,
    "AgentBuildRecordV1": AgentBuildRecordV1,
    "AgentCatalogEntryV1": AgentCatalogEntryV1,
    "AgentCatalogSnapshotV1": AgentCatalogSnapshotV1,
    "ParsedDocumentV1": ParsedDocumentV1,
    "SchemaProposalV1": SchemaProposalV1,
    "CollectionIndexPlanV1": CollectionIndexPlanV1,
    "KnowledgeResourceManifestV1": KnowledgeResourceManifestV1,
    "ResourceSnapshotV1": ResourceSnapshotV1,
    "EvaluationSetV1": EvaluationSetV1,
    "KnowledgeJobV1": KnowledgeJobV1,
}


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


def _typescript_interface_fields(source: str) -> dict[str, dict[str, bool]]:
    """提取 DTO interface 的字段及可选性，无需为契约门禁引入 Node 工具链。"""
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


def test_typescript_dto_fields_match_pydantic_contracts() -> None:
    """TypeScript DTO 的字段与可选性必须和 Pydantic JSON Schema 同步。"""
    interfaces = _typescript_interface_fields(TYPESCRIPT_DTO_PATH.read_text(encoding="utf-8"))

    for interface_name, model in TYPESCRIPT_INTERFACE_MODELS.items():
        assert interface_name in interfaces
        schema = model.model_json_schema()
        expected_fields = set(schema["properties"])
        expected_required = set(schema.get("required", []))
        actual_fields = interfaces[interface_name]
        actual_required = {name for name, optional in actual_fields.items() if not optional}

        assert set(actual_fields) == expected_fields
        assert actual_required == expected_required


def _local_dev_agent(**overrides: object) -> LocalDevAgentV1:
    """构造单 Agent 本地联调注册项，供 loopback 边界回归使用。"""

    values: dict[str, object] = {
        "agent_id": "agent_local_handbook",
        "slug": "local-handbook",
        "agent_version": "1.0.0",
        "tool_name": "local_help",
        "display_name": "本地手册",
        "description": "仅用于本地联调。",
        "supported_intents": ["本地咨询"],
        "service_name": "agent-local-handbook",
        "base_url": "http://127.0.0.1:8001",
        "timeout_seconds": 10,
        "internal_protocol_version": "muye-agent-internal/3.0",
        "descriptor_checksum": "a" * 64,
        "source_tree_checksum": "b" * 64,
        "resource_bindings": [ResourceBindingV1(resource_id="kb.local", skill_ref="skill_local@1")],
    }
    values.update(overrides)
    return LocalDevAgentV1.model_validate(values)


@pytest.mark.parametrize("base_url", ["http://localhost:8001", "https://127.0.0.1:8001", "http://10.0.0.1:8001", "http://127.0.0.1:8001/path"])
def test_local_dev_registration_rejects_non_literal_loopback_targets(base_url: str) -> None:
    """开发注册表不能成为 Main 访问任意本地网络目标的通道。"""

    with pytest.raises(ValidationError, match="loopback"):
        _local_dev_agent(base_url=base_url)


def test_local_dev_registration_checksum_detects_tampering() -> None:
    registration = build_local_dev_registration(user_id="local-dev-user", agent=_local_dev_agent())
    payload = registration.model_dump(mode="json")
    payload["agent"]["description"] = "篡改后的内容"

    with pytest.raises(ValidationError, match="checksum"):
        type(registration).model_validate(payload)


def test_local_dev_registration_uses_descriptor_slug_for_service_identity() -> None:
    """Agent ID 可独立命名，local-dev 服务身份必须沿用正式的 slug 规则。"""

    registration = build_local_dev_registration(
        user_id="local-dev-user",
        agent=_local_dev_agent(agent_id="agent_hotel_hr", slug="hotel-employee", service_name="agent-hotel-employee"),
    )

    assert registration.agent.service_name == "agent-hotel-employee"
    with pytest.raises(ValidationError, match="slug"):
        _local_dev_agent(agent_id="agent_hotel_hr", slug="hotel-employee", service_name="agent-hotel-hr")
