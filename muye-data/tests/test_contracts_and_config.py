"""公共请求契约和版本化配置的边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.backends.factory import build_backends
from src.config import DataConfig, ServiceSettings, load_data_config
from src.contracts import RetrieveRequest
from src.errors import ConfigurationError


def _valid_config() -> dict:
    return {
        "version": 1,
        "connections": {
            "milvus": {
                "type": "milvus",
                "uri": "http://milvus.test:19530",
                "token_env": "MILVUS_TEST_TOKEN",
            }
        },
        "resources": {
            "knowledge": {
                "connection": "milvus",
                "target": "documents",
                "fields": {
                    "id": "document_id",
                    "content": "content",
                    "vector": "embedding",
                    "keyword": "sparse_embedding",
                    "exposed_fields": {"title": "title"},
                    "filterable_fields": {"category": "category"},
                },
                "embedding": {"model": "embed-v1", "dimensions": 3},
                "pipelines": {
                    "hybrid": {"type": "hybrid"},
                    "keyword": {"type": "keyword"},
                },
                "default_pipeline": "hybrid",
                "default_return_fields": ["title"],
            }
        },
    }


def test_request_rejects_unknown_fields_and_whitespace_query() -> None:
    with pytest.raises(ValidationError):
        RetrieveRequest.model_validate({"resource": "knowledge", "query": " ", "database": "x"})


def test_filter_rejects_operator_with_ambiguous_shape() -> None:
    with pytest.raises(ValidationError):
        RetrieveRequest.model_validate(
            {
                "resource": "knowledge",
                "query": "hello",
                "filter": {"op": "eq", "field": "category", "values": ["a"]},
            }
        )


def test_filter_depth_is_bounded() -> None:
    expression: dict = {"op": "eq", "field": "category", "value": "a"}
    for _ in range(9):
        expression = {"op": "not", "condition": expression}

    with pytest.raises(ValidationError, match="最大嵌套深度"):
        RetrieveRequest.model_validate(
            {"resource": "knowledge", "query": "hello", "filter": expression}
        )


def test_config_rejects_plaintext_credentials() -> None:
    payload = _valid_config()
    payload["connections"]["milvus"]["token"] = "should-not-be-here"

    with pytest.raises(ValidationError):
        DataConfig.model_validate(payload)


def test_config_requires_pipeline_fields() -> None:
    payload = _valid_config()
    del payload["resources"]["knowledge"]["fields"]["vector"]

    with pytest.raises(ValidationError, match="fields.vector"):
        DataConfig.model_validate(payload)


def test_config_does_not_allow_vector_exposure() -> None:
    payload = _valid_config()
    payload["resources"]["knowledge"]["fields"]["exposed_fields"]["raw"] = "embedding"

    with pytest.raises(ValidationError, match="vector 字段"):
        DataConfig.model_validate(payload)


def test_yaml_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nversion: 1\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="重复键"):
        load_data_config(path)


def test_backend_factory_resolves_secret_from_environment_without_network() -> None:
    config = DataConfig.model_validate(_valid_config())

    backends = build_backends(config, environ={"MILVUS_TEST_TOKEN": "test-token"})

    assert backends["milvus"].backend_type == "milvus"


def test_backend_factory_rejects_missing_referenced_secret() -> None:
    config = DataConfig.model_validate(_valid_config())

    with pytest.raises(ConfigurationError, match="MILVUS_TEST_TOKEN"):
        build_backends(config, environ={})


def test_backend_factory_ignores_unreferenced_connection_credentials() -> None:
    payload = _valid_config()
    payload["connections"]["unused_milvus"] = {
        "type": "milvus",
        "uri": "http://unused.test:19530",
        "token_env": "UNUSED_MILVUS_TOKEN",
    }
    config = DataConfig.model_validate(payload)

    backends = build_backends(config, environ={"MILVUS_TEST_TOKEN": "test-token"})

    assert set(backends) == {"milvus"}


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_service_timeouts_must_be_finite(value: str) -> None:
    with pytest.raises(ConfigurationError, match="有限数字"):
        ServiceSettings.from_env({"MUYE_DATA_TOTAL_TIMEOUT": value})
