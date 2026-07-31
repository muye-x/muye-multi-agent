"""阶段 5 SubAgent 到 muye-data 服务身份与 Resource 权限测试。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Request
from pydantic import SecretStr
import pytest

from contracts.catalog import build_catalog_snapshot
from contracts.models import AgentCatalogEntryV1, ResourceBindingV1
from src.auth import DataServiceAuthorizer
from src.errors import (
    AuthorizationUnavailableError,
    ServiceAuthenticationError,
    ServiceAuthorizationError,
)


def _snapshot(*, status: str = "ACTIVE"):
    return build_catalog_snapshot(
        [
            AgentCatalogEntryV1(
                agent_id="agent_product_handbook",
                agent_version="1.0.0",
                tool_name="product_help",
                display_name="产品手册",
                description="查询产品手册。",
                supported_intents=["产品咨询"],
                service_name="agent-product-handbook",
                base_url="http://agent-product-handbook:8000",
                timeout_seconds=30,
                internal_protocol_version="muye-agent-internal/3.0",
                api_profile="internal",
                descriptor_checksum="a" * 64,
                source_tree_checksum="b" * 64,
                image_digest=f"sha256:{'c' * 64}",
                resource_bindings=[
                    ResourceBindingV1(resource_id="kb.product", skill_ref="skill_product@1")
                ],
                capabilities_checksum="d" * 64,
                status=status,
            )
        ]
    )


def _authorizer(tmp_path: Path, *, status: str = "ACTIVE") -> tuple[DataServiceAuthorizer, Path]:
    path = tmp_path / "active-catalog.json"
    path.write_text(json.dumps(_snapshot(status=status).model_dump(mode="json")), encoding="utf-8")
    return (
        DataServiceAuthorizer(
            catalog_path=path,
            tokens={"agent_product_handbook": SecretStr("product-data-token")},
        ),
        path,
    )


def _request(*, token: str = "product-data-token", overrides: dict[str, str] | None = None) -> Request:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Muye-Service-Id": "agent-product-handbook",
        "X-Muye-Deployment-Id": "agent_product_handbook:1.0.0:cccccccccccc",
        "X-Muye-Agent-Id": "agent_product_handbook",
        "X-Muye-Agent-Version": "1.0.0",
        "X-Muye-Descriptor-Checksum": "a" * 64,
        "X-Muye-Source-Checksum": "b" * 64,
    }
    headers.update(overrides or {})
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/retrieve",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        }
    )


def test_active_agent_token_is_bound_to_identity_and_declared_resource(tmp_path: Path) -> None:
    authorizer, _ = _authorizer(tmp_path)

    authorizer.authorize(_request(), resource_id="kb.product")

    with pytest.raises(ServiceAuthorizationError):
        authorizer.authorize(_request(), resource_id="kb.other")
    with pytest.raises(ServiceAuthorizationError):
        authorizer.authorize(
            _request(overrides={"X-Muye-Descriptor-Checksum": "e" * 64}),
            resource_id="kb.product",
        )
    with pytest.raises(ServiceAuthenticationError):
        authorizer.authorize(_request(token="control-token"), resource_id="kb.product")


def test_data_authorization_fails_closed_after_catalog_status_or_checksum_changes(tmp_path: Path) -> None:
    authorizer, path = _authorizer(tmp_path, status="DEGRADED")
    with pytest.raises(ServiceAuthorizationError):
        authorizer.authorize(_request(), resource_id="kb.product")

    payload = _snapshot().model_dump(mode="json")
    payload["catalog_checksum"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AuthorizationUnavailableError):
        authorizer.authorize(_request(), resource_id="kb.product")


def test_data_authorizer_configuration_requires_distinct_target_tokens(tmp_path: Path) -> None:
    disabled = DataServiceAuthorizer.from_env({}, base_directory=tmp_path)
    assert disabled.enabled is False

    with pytest.raises(ValueError, match="不同"):
        DataServiceAuthorizer.from_env(
            {
                "MUYE_DATA_AGENT_AUTH_ENABLED": "true",
                "MUYE_DATA_AGENT_CATALOG_PATH": "active.json",
                "MUYE_DATA_AGENT_TOKENS_JSON": json.dumps(
                    {
                        "agent_product_handbook": "shared-token",
                        "agent_other_service": "shared-token",
                    }
                ),
            },
            base_directory=tmp_path,
        )
