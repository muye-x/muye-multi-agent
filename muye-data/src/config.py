"""版本化资源配置与进程环境配置。

YAML 只描述既有数据库目标、逻辑字段映射和召回 pipeline。数据库凭据只能通过
环境变量名引用，配置模型没有明文密码或 token 字段。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.contracts import FIELD_NAME_PATTERN, RESOURCE_NAME_PATTERN
from src.errors import ConfigurationError


ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
PHYSICAL_FIELD_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]*$"


class StrictConfigModel(BaseModel):
    """拒绝未知配置键与隐式类型转换的配置基类。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class MilvusConnectionConfig(StrictConfigModel):
    """Milvus 连接定义；token 只能来自 ``token_env``。"""

    type: Literal["milvus"]
    uri: str = Field(min_length=1, max_length=2048)
    token_env: str | None = Field(default=None, pattern=ENV_NAME_PATTERN)
    database: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Milvus uri 必须是 HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Milvus uri 不能包含凭据")
        return normalized


class OpenSearchConnectionConfig(StrictConfigModel):
    """OpenSearch 连接定义；用户名和密码必须成对引用环境变量。"""

    type: Literal["opensearch"]
    hosts: list[str] = Field(min_length=1, max_length=16)
    username_env: str | None = Field(default=None, pattern=ENV_NAME_PATTERN)
    password_env: str | None = Field(default=None, pattern=ENV_NAME_PATTERN)
    verify_certs: bool = True
    ca_certs: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("hosts")
    @classmethod
    def validate_hosts(cls, value: list[str]) -> list[str]:
        """仅接受 HTTP(S) URL，避免将任意连接参数透传给客户端。"""
        normalized: list[str] = []
        for host in value:
            candidate = host.strip().rstrip("/")
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("OpenSearch hosts 必须是 HTTP(S) URL")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("OpenSearch hosts 不能包含凭据")
            normalized.append(candidate)
        return normalized

    @model_validator(mode="after")
    def validate_credentials(self) -> "OpenSearchConnectionConfig":
        if bool(self.username_env) != bool(self.password_env):
            raise ValueError("username_env 与 password_env 必须同时配置")
        return self


ConnectionConfig = Annotated[
    MilvusConnectionConfig | OpenSearchConnectionConfig,
    Field(discriminator="type"),
]


class FieldMapping(StrictConfigModel):
    """公共逻辑角色到使用者自定义物理字段的最小映射。"""

    id: str = Field(pattern=PHYSICAL_FIELD_PATTERN)
    content: str = Field(pattern=PHYSICAL_FIELD_PATTERN)
    vector: str | None = Field(default=None, pattern=PHYSICAL_FIELD_PATTERN)
    keyword: str | None = Field(default=None, pattern=PHYSICAL_FIELD_PATTERN)
    exposed_fields: dict[str, str] = Field(default_factory=dict)
    filterable_fields: dict[str, str] = Field(default_factory=dict)

    @field_validator("exposed_fields", "filterable_fields")
    @classmethod
    def validate_field_map(cls, value: dict[str, str]) -> dict[str, str]:
        """校验逻辑字段和安全的物理字段标识。"""
        for logical_name, physical_name in value.items():
            if re.fullmatch(FIELD_NAME_PATTERN, logical_name) is None:
                raise ValueError(f"非法逻辑字段名：{logical_name!r}")
            if re.fullmatch(PHYSICAL_FIELD_PATTERN, physical_name) is None:
                raise ValueError(f"非法物理字段名：{physical_name!r}")
        return value

    @model_validator(mode="after")
    def prevent_vector_exposure(self) -> "FieldMapping":
        """向量字段不得通过通用 fields 响应泄漏。"""
        if self.vector and self.vector in self.exposed_fields.values():
            raise ValueError("vector 字段不能出现在 exposed_fields")
        return self


class EmbeddingConfig(StrictConfigModel):
    """资源向量字段对应的 muye-llm 模型别名和固定维度。"""

    model: str = Field(pattern=RESOURCE_NAME_PATTERN)
    dimensions: int = Field(ge=1, le=65536)


class RerankConfig(StrictConfigModel):
    """可选重排阶段；required 决定失败时是否允许回退。"""

    model: str = Field(pattern=RESOURCE_NAME_PATTERN)
    required: bool = False


class DensePipelineConfig(StrictConfigModel):
    """单路稠密向量召回配置。"""

    type: Literal["dense"]
    candidate_k: int = Field(default=50, ge=1, le=1000)
    metric_type: Literal["COSINE", "IP", "L2"] = "COSINE"
    rerank: RerankConfig | None = None


class KeywordPipelineConfig(StrictConfigModel):
    """单路 BM25/关键词召回配置。"""

    type: Literal["keyword"]
    candidate_k: int = Field(default=50, ge=1, le=1000)
    rerank: RerankConfig | None = None


class HybridPipelineConfig(StrictConfigModel):
    """并发稠密与关键词召回后执行加权 RRF 的配置。"""

    type: Literal["hybrid"]
    dense_candidate_k: int = Field(default=50, ge=1, le=1000)
    keyword_candidate_k: int = Field(default=50, ge=1, le=1000)
    dense_weight: float = Field(default=1.0, gt=0, le=100)
    keyword_weight: float = Field(default=1.0, gt=0, le=100)
    rank_constant: int = Field(default=60, ge=1, le=10000)
    metric_type: Literal["COSINE", "IP", "L2"] = "COSINE"
    dense_required: bool = False
    keyword_required: bool = False
    rerank: RerankConfig | None = None


PipelineConfig = Annotated[
    DensePipelineConfig | KeywordPipelineConfig | HybridPipelineConfig,
    Field(discriminator="type"),
]


class ResourceConfig(StrictConfigModel):
    """一个只读逻辑资源及其可选召回 pipeline。"""

    connection: str = Field(pattern=RESOURCE_NAME_PATTERN)
    target: str = Field(min_length=1, max_length=255)
    fields: FieldMapping
    embedding: EmbeddingConfig | None = None
    pipelines: dict[str, PipelineConfig] = Field(min_length=1)
    default_pipeline: str = Field(pattern=RESOURCE_NAME_PATTERN)
    default_return_fields: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character.isspace() or ord(character) < 32 for character in normalized):
            raise ValueError("target 不能包含空白或控制字符")
        return normalized

    @field_validator("pipelines")
    @classmethod
    def validate_pipeline_names(cls, value: dict[str, PipelineConfig]) -> dict[str, PipelineConfig]:
        for name in value:
            if re.fullmatch(RESOURCE_NAME_PATTERN, name) is None:
                raise ValueError(f"非法 pipeline 名称：{name!r}")
        return value

    @model_validator(mode="after")
    def validate_pipeline_fields(self) -> "ResourceConfig":
        """确保 pipeline 需要的逻辑角色已配置，而不限制其他业务字段。"""
        if self.default_pipeline not in self.pipelines:
            raise ValueError("default_pipeline 必须存在于 pipelines")
        for pipeline in self.pipelines.values():
            if pipeline.type in {"dense", "hybrid"}:
                if self.fields.vector is None or self.embedding is None:
                    raise ValueError("dense/hybrid pipeline 需要 fields.vector 与 embedding")
            if pipeline.type in {"keyword", "hybrid"} and self.fields.keyword is None:
                raise ValueError("keyword/hybrid pipeline 需要 fields.keyword")
        if len(self.default_return_fields) != len(set(self.default_return_fields)):
            raise ValueError("default_return_fields 不能重复")
        unknown_fields = set(self.default_return_fields) - set(self.fields.exposed_fields)
        if unknown_fields:
            raise ValueError("default_return_fields 必须属于 exposed_fields")
        return self


class DataConfig(StrictConfigModel):
    """muye-data YAML 根配置，当前仅接受版本 1。"""

    version: Literal[1]
    connections: dict[str, ConnectionConfig] = Field(min_length=1)
    resources: dict[str, ResourceConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> "DataConfig":
        for name in (*self.connections.keys(), *self.resources.keys()):
            if re.fullmatch(RESOURCE_NAME_PATTERN, name) is None:
                raise ValueError(f"非法配置别名：{name!r}")
        for resource in self.resources.values():
            if resource.connection not in self.connections:
                raise ValueError(f"资源引用了未知 connection：{resource.connection!r}")
        return self


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝 YAML 重复键，防止后写配置静默覆盖前值。"""


def _construct_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigurationError(f"YAML 包含重复键：{key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_data_config(path: Path) -> DataConfig:
    """读取并严格校验版本化 YAML；不创建或探测任何数据库资源。"""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"无法读取配置文件：{path}") from exc
    if len(raw_bytes) > 1_048_576:
        raise ConfigurationError("配置文件不能超过 1 MiB")
    try:
        payload = yaml.load(raw_bytes.decode("utf-8"), Loader=_UniqueKeyLoader)
    except UnicodeDecodeError as exc:
        raise ConfigurationError("配置文件必须使用 UTF-8") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError("配置文件不是合法 YAML") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("配置文件根节点必须是 object")
    try:
        return DataConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigurationError(f"配置校验失败：{exc}") from exc


def require_environment_value(name: str, environ: Mapping[str, str] | None = None) -> str:
    """解析配置引用的敏感环境变量，不返回空白值。"""
    source = environ if environ is not None else os.environ
    value = source.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"配置引用的环境变量 {name} 未设置")
    return value


def _env_int(
    environ: Mapping[str, str],
    name: str,
    default: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        value = int(environ.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} 必须大于等于 {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} 必须小于等于 {maximum}")
    return value


def _env_float(environ: Mapping[str, str], name: str, default: str, *, minimum: float) -> float:
    try:
        value = float(environ.get(name, default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字") from exc
    if not math.isfinite(value):
        raise ConfigurationError(f"{name} 必须是有限数字")
    if value <= minimum:
        raise ConfigurationError(f"{name} 必须大于 {minimum}")
    return value


class ServiceSettings(StrictConfigModel):
    """进程级设置，统一由 ``MUYE_DATA_*`` 环境变量构造。"""

    host: str
    port: int = Field(ge=1, le=65535)
    workers: int = Field(ge=1)
    log_level: str
    config_path: Path
    llm_base_url: str
    llm_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    backend_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    total_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    backend_max_retries: int = Field(ge=0, le=1)
    rerank_max_documents: int = Field(ge=100, le=1000)

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MUYE_DATA_LLM_BASE_URL 必须是 HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("MUYE_DATA_LLM_BASE_URL 不能包含凭据")
        return normalized

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ServiceSettings":
        """从显式 mapping 或进程环境读取严格设置，便于测试隔离。"""
        source = environ if environ is not None else os.environ
        return cls(
            host=source.get("MUYE_DATA_HOST", "127.0.0.1"),
            port=_env_int(source, "MUYE_DATA_PORT", "9840", minimum=1),
            workers=_env_int(source, "MUYE_DATA_WORKERS", "1", minimum=1),
            log_level=source.get("MUYE_DATA_LOG_LEVEL", "INFO").upper(),
            config_path=Path(source.get("MUYE_DATA_CONFIG_PATH", "config.yaml")),
            llm_base_url=source.get("MUYE_DATA_LLM_BASE_URL", "http://127.0.0.1:9850"),
            llm_timeout_seconds=_env_float(source, "MUYE_DATA_LLM_TIMEOUT", "10", minimum=0),
            backend_timeout_seconds=_env_float(source, "MUYE_DATA_BACKEND_TIMEOUT", "5", minimum=0),
            total_timeout_seconds=_env_float(source, "MUYE_DATA_TOTAL_TIMEOUT", "15", minimum=0),
            backend_max_retries=_env_int(
                source,
                "MUYE_DATA_BACKEND_MAX_RETRIES",
                "1",
                minimum=0,
                maximum=1,
            ),
            rerank_max_documents=_env_int(
                source,
                "MUYE_DATA_RERANK_MAX_DOCUMENTS",
                "100",
                minimum=100,
                maximum=1000,
            ),
        )
