"""多厂商 LLM 统一代理服务配置。"""

import json
import os
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

load_dotenv()


DEFAULT_LLM_MODELS_JSON = (
    '[{"id":"qwen3.7-max","name":"Qwen 3.7 Max","provider_model":"qwen3.7-max",'
    '"supports_thinking":true},{"id":"qwen3.7-plus","name":"Qwen 3.7 Plus",'
    '"provider_model":"qwen3.7-plus","supports_thinking":true},{"id":"qwen-flash",'
    '"name":"Qwen Flash","provider_model":"qwen-flash","supports_thinking":true},'
    '{"id":"deepseek-v4-pro","name":"DeepSeek V4 Pro",'
    '"provider_model":"deepseek-v4-pro","supports_thinking":true},'
    '{"id":"deepseek-v4-flash","name":"DeepSeek V4 Flash",'
    '"provider_model":"deepseek-v4-flash","supports_thinking":true}]'
)


def _env_bool(name: str, default: str = "false") -> bool:
    """读取严格布尔环境变量，避免拼写错误被静默解释为 false。"""
    value = os.getenv(name, default).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是布尔值，当前值为 {value!r}")


def _env_int(name: str, default: str, *, minimum: int) -> int:
    """读取并校验有下界的整数环境变量。"""
    try:
        value = int(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


def _env_float(name: str, default: str, *, minimum: float, inclusive: bool = False) -> float:
    """读取并校验浮点环境变量。"""
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    valid = value >= minimum if inclusive else value > minimum
    if not valid:
        operator = "大于等于" if inclusive else "大于"
        raise ValueError(f"{name} 必须{operator} {minimum}")
    return value


def _env_json_object(name: str, default: str = "{}") -> dict[str, Any]:
    """读取 OpenAI compatible 的扩展请求体，要求为 JSON object。"""
    raw_value = os.getenv(name, default).strip()
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是合法 JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 JSON object")
    return value


def _env_json_array(name: str, default: str) -> list[Any]:
    """读取 JSON array 环境变量，具体元素结构由调用方继续校验。"""
    raw_value = os.getenv(name, default).strip()
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是合法 JSON array") from exc
    if not isinstance(value, list):
        raise ValueError(f"{name} 必须是 JSON array")
    return value


class LLMModelSettings(BaseModel):
    """一个允许调用的 LLM 定义，来源于 ``LLM_MODELS_JSON``。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    provider_model: str = Field(min_length=1)
    supports_thinking: bool

    @field_validator("id", "name", "provider_model")
    @classmethod
    def strip_non_empty_text(cls, value: str) -> str:
        """规范化模型标识和展示字段，拒绝仅包含空白的配置值。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("模型文本字段不能为空")
        return normalized


def _env_llm_models() -> list[LLMModelSettings]:
    """解析并验证环境变量中的模型注册表。"""
    raw_models = _env_json_array("MUYE_LLM_MODELS_JSON", DEFAULT_LLM_MODELS_JSON)
    if not raw_models:
        raise ValueError("LLM_MODELS_JSON 至少需要配置一个模型")
    return [LLMModelSettings.model_validate(item) for item in raw_models]


class Settings(BaseModel):
    """LLM 服务配置项，全部从环境变量读取。"""

    # 服务基础配置
    host: str = os.getenv("MUYE_LLM_HOST", "127.0.0.1")
    port: int = _env_int("MUYE_LLM_PORT", "9850", minimum=1)
    workers: int = _env_int("MUYE_LLM_WORKERS", "1", minimum=1)
    log_level: str = os.getenv("MUYE_LLM_LOG_LEVEL", "INFO")

    # 模型注册表与主节点配置
    llm_api_base_url: str = os.getenv("MUYE_LLM_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    llm_api_key: str = os.getenv("MUYE_LLM_API_KEY", "")
    llm_models: list[LLMModelSettings] = _env_llm_models()
    llm_default_model: str = os.getenv("MUYE_LLM_DEFAULT_MODEL", "deepseek-v4-flash").strip()
    llm_default_thinking: bool = _env_bool("MUYE_LLM_DEFAULT_THINKING", "false")
    llm_extra_body: dict[str, Any] = _env_json_object("MUYE_LLM_EXTRA_BODY_JSON")

    # Embedding 服务配置
    embed_api_base_url: str = os.getenv("MUYE_LLM_EMBED_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    embed_api_key: str = os.getenv("MUYE_LLM_EMBED_API_KEY", "")
    embed_model: str = os.getenv("MUYE_LLM_EMBED_MODEL", "text-embedding-v3")

    # 调用参数
    llm_default_temperature: float = _env_float(
        "MUYE_LLM_DEFAULT_TEMPERATURE",
        "0.1",
        minimum=0.0,
        inclusive=True,
    )
    llm_default_max_tokens: int = _env_int("MUYE_LLM_DEFAULT_MAX_TOKENS", "4096", minimum=1)
    llm_timeout: float = _env_float("MUYE_LLM_TIMEOUT", "30.0", minimum=0.0)
    llm_max_retries: int = _env_int("MUYE_LLM_MAX_RETRIES", "3", minimum=1)

    # 可选 LangSmith 可观测性；任务正文和业务 metadata 不进入 tracing。
    langsmith_enabled: bool = _env_bool("MUYE_LLM_LANGSMITH_ENABLED", "false")
    langsmith_api_key: str = os.getenv("LANGSMITH_API_KEY", "")
    langsmith_project: str = os.getenv("MUYE_LLM_LANGSMITH_PROJECT", "muye-llm")
    langsmith_endpoint: str = os.getenv("LANGSMITH_ENDPOINT", "")

    def model_post_init(self, __context: Any) -> None:
        """校验模型注册表和扩展请求体。"""
        model_ids = [model.id.strip() for model in self.llm_models]
        if any(not model_id for model_id in model_ids):
            raise ValueError("LLM_MODELS_JSON 中的模型 id 不能为空")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("LLM_MODELS_JSON 中的模型 id 必须唯一")
        if self.llm_default_model not in model_ids:
            raise ValueError("LLM_DEFAULT_MODEL 必须存在于 LLM_MODELS_JSON")

        default_model = next(model for model in self.llm_models if model.id == self.llm_default_model)
        if self.llm_default_thinking and not default_model.supports_thinking:
            raise ValueError("默认模型不支持 LLM_DEFAULT_THINKING=true")
        if "enable_thinking" in self.llm_extra_body:
            raise ValueError("LLM_EXTRA_BODY_JSON 不允许配置 enable_thinking")


settings = Settings()
