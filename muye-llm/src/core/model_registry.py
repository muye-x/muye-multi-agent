"""可调用 LLM 的注册表、能力校验和请求级模型选择。"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import EmbeddingModelSettings, LLMModelSettings, RerankModelSettings


class ModelSelectionError(ValueError):
    """调用方选择了未注册模型或模型不支持的能力。"""


@dataclass(frozen=True, slots=True)
class EmbeddingModelSelection:
    """一次 Embedding 调用解析后的内部模型定义。"""

    id: str
    name: str
    provider_model: str
    dimensions: int | None


@dataclass(frozen=True, slots=True)
class RerankModelSelection:
    """一次 Rerank 调用解析后的内部模型定义。"""

    id: str
    name: str
    provider_model: str
    provider: str


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """一次 Chat 调用解析后的最终模型与 thinking 状态。"""

    id: str
    name: str
    provider_model: str
    supports_thinking: bool
    thinking_enabled: bool


class ModelRegistry:
    """从配置白名单解析模型，并阻止任意 provider model 透传。"""

    def __init__(
        self,
        models: list[LLMModelSettings],
        *,
        default_model: str,
        default_thinking: bool,
    ) -> None:
        self._models = tuple(models)
        self._models_by_id = {model.id: model for model in models}
        self.default_model = default_model
        self.default_thinking = default_thinking

    @property
    def models(self) -> tuple[LLMModelSettings, ...]:
        """按环境配置顺序返回模型定义，用于稳定展示模型列表。"""
        return self._models

    def resolve(
        self,
        model_id: str | None,
        enable_thinking: bool | None,
    ) -> ModelSelection:
        """解析请求覆盖与默认值，并验证 thinking 能力。"""
        effective_model_id = self.default_model if model_id is None else model_id.strip()
        model = self._models_by_id.get(effective_model_id)
        if model is None:
            raise ModelSelectionError(f"不支持的模型: {effective_model_id or '<empty>'}")

        thinking_enabled = (
            self.default_thinking if enable_thinking is None else enable_thinking
        )
        if thinking_enabled and not model.supports_thinking:
            raise ModelSelectionError(f"模型 {model.id} 不支持 thinking")

        return ModelSelection(
            id=model.id,
            name=model.name,
            provider_model=model.provider_model,
            supports_thinking=model.supports_thinking,
            thinking_enabled=thinking_enabled,
        )


class EmbeddingModelRegistry:
    """Embedding 模型别名白名单与默认模型选择器。"""

    def __init__(
        self,
        models: list[EmbeddingModelSettings],
        *,
        default_model: str,
    ) -> None:
        self._models = tuple(models)
        self._models_by_id = {model.id: model for model in models}
        self.default_model = default_model

    @property
    def models(self) -> tuple[EmbeddingModelSettings, ...]:
        """按配置顺序返回公开展示所需的模型定义。"""
        return self._models

    def resolve(self, model_id: str | None) -> EmbeddingModelSelection:
        """解析请求别名，拒绝任意上游模型名透传。"""
        effective_model_id = self.default_model if model_id is None else model_id.strip()
        model = self._models_by_id.get(effective_model_id)
        if model is None:
            raise ModelSelectionError(
                f"不支持的 Embedding 模型: {effective_model_id or '<empty>'}"
            )
        return EmbeddingModelSelection(
            id=model.id,
            name=model.name,
            provider_model=model.provider_model,
            dimensions=model.dimensions,
        )


class RerankModelRegistry:
    """Rerank 模型别名白名单与默认模型选择器。"""

    def __init__(
        self,
        models: list[RerankModelSettings],
        *,
        default_model: str,
    ) -> None:
        self._models = tuple(models)
        self._models_by_id = {model.id: model for model in models}
        self.default_model = default_model

    @property
    def models(self) -> tuple[RerankModelSettings, ...]:
        """按配置顺序返回公开展示所需的模型定义。"""
        return self._models

    def resolve(self, model_id: str | None) -> RerankModelSelection:
        """解析请求别名，拒绝 provider 模型名直接透传。"""
        effective_model_id = self.default_model if model_id is None else model_id.strip()
        model = self._models_by_id.get(effective_model_id)
        if model is None:
            raise ModelSelectionError(
                f"不支持的 Rerank 模型: {effective_model_id or '<empty>'}"
            )
        return RerankModelSelection(
            id=model.id,
            name=model.name,
            provider_model=model.provider_model,
            provider=model.provider,
        )
