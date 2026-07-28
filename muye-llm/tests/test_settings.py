"""Embedding/Rerank 注册表环境配置的兼容与校验测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import (
    EmbeddingModelSettings,
    RerankModelSettings,
    Settings,
    _env_embedding_models,
    _env_float,
    _env_http_url,
)


def test_legacy_embed_model_becomes_single_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUYE_LLM_EMBED_MODELS_JSON", raising=False)
    monkeypatch.setenv("MUYE_LLM_EMBED_MODEL", " legacy-provider-model ")

    models = _env_embedding_models()

    assert models == [
        EmbeddingModelSettings(
            id="legacy-provider-model",
            name="legacy-provider-model",
            provider_model="legacy-provider-model",
            dimensions=None,
        )
    ]


def test_settings_choose_first_embedding_and_rerank_alias_by_default() -> None:
    configured = Settings(
        embed_models=[
            EmbeddingModelSettings(
                id="embedding-a",
                name="Embedding A",
                provider_model="provider-a",
                dimensions=768,
            )
        ],
        embed_default_model="",
        rerank_models=[
            RerankModelSettings(
                id="rerank-a",
                name="Rerank A",
                provider_model="provider-rerank-a",
            )
        ],
        rerank_default_model="",
    )

    assert configured.embed_default_model == "embedding-a"
    assert configured.rerank_default_model == "rerank-a"


def test_settings_reject_duplicate_embedding_aliases() -> None:
    duplicate = EmbeddingModelSettings(
        id="duplicate",
        name="Duplicate",
        provider_model="provider-model",
        dimensions=1024,
    )

    with pytest.raises(ValidationError, match="模型 id 必须唯一"):
        Settings(
            embed_models=[duplicate, duplicate],
            embed_default_model="duplicate",
        )


def test_enabled_rerank_requires_registered_default_alias() -> None:
    with pytest.raises(ValidationError, match="RERANK_MODELS_JSON 不能为空"):
        Settings(
            rerank_enabled=True,
            rerank_models=[],
            rerank_default_model="",
        )


def test_enabled_rerank_requires_non_empty_api_url() -> None:
    """直接构造 Settings 也必须拒绝无法调用的 Rerank 配置。"""
    with pytest.raises(ValidationError, match="MUYE_LLM_RERANK_API_URL 不能为空"):
        Settings(rerank_enabled=True, rerank_api_url="")


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_float_environment_values_must_be_finite(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("TEST_FINITE_FLOAT", value)

    with pytest.raises(ValueError, match="有限数字"):
        _env_float("TEST_FINITE_FLOAT", "1", minimum=0)


def test_rerank_url_must_be_complete_and_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_RERANK_URL", "https://user:password@example.test/rerank")

    with pytest.raises(ValueError, match="不能包含 URL 凭据"):
        _env_http_url("TEST_RERANK_URL", "")
