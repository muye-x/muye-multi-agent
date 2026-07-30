"""已发布 Resource Snapshot 的严格加载与到只读配置的转换。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.config import ResourceConfig
from src.errors import ConfigurationError


@dataclass(frozen=True)
class LoadedResourceSnapshot:
    """已校验快照的 revision、checksum 与可供 RetrievalService 原子替换的资源表。"""

    revision: str
    checksum: str
    resources: dict[str, ResourceConfig]


def load_resource_snapshot(path: Path, *, known_connections: set[str]) -> LoadedResourceSnapshot:
    """加载阶段 4 发布的 Snapshot，任何候选错误均在替换服务状态前失败。"""
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("Resource Snapshot 不存在、不是普通文件或是符号链接")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError("无法读取 Resource Snapshot") from exc
    if len(raw) > 4_194_304:
        raise ConfigurationError("Resource Snapshot 不能超过 4 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConfigurationError("Resource Snapshot 不是合法唯一键 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("Resource Snapshot 根节点必须是 object")
    _reject_unknown_keys(payload, {"schema_version", "snapshot_revision", "snapshot_checksum", "resources"}, "Snapshot")
    if payload.get("schema_version") != "muye.ai/resource-snapshot/v1":
        raise ConfigurationError("Resource Snapshot schema_version 不受支持")
    revision = payload.get("snapshot_revision")
    checksum = payload.get("snapshot_checksum")
    resources_payload = payload.get("resources")
    if not isinstance(revision, str) or not _safe_reference(revision):
        raise ConfigurationError("Resource Snapshot revision 无效")
    if not isinstance(checksum, str) or not _sha256(checksum):
        raise ConfigurationError("Resource Snapshot checksum 无效")
    expected_payload = dict(payload)
    expected_payload.pop("snapshot_checksum", None)
    if canonical_checksum(expected_payload) != checksum:
        raise ConfigurationError("Resource Snapshot checksum 不匹配")
    if not isinstance(resources_payload, dict) or not resources_payload:
        raise ConfigurationError("Resource Snapshot 必须包含至少一个资源")
    resources: dict[str, ResourceConfig] = {}
    for resource_id, manifest in resources_payload.items():
        if not isinstance(resource_id, str) or not _resource_name(resource_id) or not isinstance(manifest, dict):
            raise ConfigurationError("Resource Snapshot 包含非法资源")
        resources[resource_id] = _resource_from_manifest(
            resource_id,
            manifest,
            known_connections=known_connections,
        )
    return LoadedResourceSnapshot(revision=revision, checksum=checksum, resources=resources)


def canonical_checksum(value: object) -> str:
    """与 Scaffold 发布器使用相同的稳定 JSON SHA-256 算法。"""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def _resource_from_manifest(
    resource_id: str,
    manifest: dict[str, Any],
    *,
    known_connections: set[str],
) -> ResourceConfig:
    """严格校验 Resource Manifest 并转换为既有 `ResourceConfig`。"""
    expected_keys = {
        "schema_version",
        "resource_id",
        "resource_revision",
        "resource_checksum",
        "knowledge_id",
        "knowledge_version_id",
        "collection_plan_checksum",
        "connection",
        "target",
        "fields",
        "embedding_alias",
        "embedding_dimensions",
        "pipelines",
        "default_pipeline",
        "default_return_fields",
    }
    _reject_unknown_keys(manifest, expected_keys, f"Resource {resource_id}")
    if manifest.get("schema_version") != "muye.ai/knowledge-resource-manifest/v1":
        raise ConfigurationError(f"Resource {resource_id} schema_version 不受支持")
    if manifest.get("resource_id") != resource_id:
        raise ConfigurationError(f"Resource {resource_id} 的 resource_id 不一致")
    resource_checksum = manifest.get("resource_checksum")
    if not isinstance(resource_checksum, str) or not _sha256(resource_checksum):
        raise ConfigurationError(f"Resource {resource_id} checksum 无效")
    checksum_payload = dict(manifest)
    checksum_payload.pop("resource_checksum", None)
    if canonical_checksum(checksum_payload) != resource_checksum:
        raise ConfigurationError(f"Resource {resource_id} checksum 不匹配")
    connection = manifest.get("connection")
    if not isinstance(connection, str) or connection not in known_connections:
        raise ConfigurationError(f"Resource {resource_id} 引用了未初始化 connection")
    fields = manifest.get("fields")
    if not isinstance(fields, dict):
        raise ConfigurationError(f"Resource {resource_id} fields 无效")
    _reject_unknown_keys(fields, {"id", "content", "vector", "keyword", "exposed_fields", "filterable_fields"}, "Resource fields")
    pipelines = manifest.get("pipelines")
    if not isinstance(pipelines, dict) or not pipelines:
        raise ConfigurationError(f"Resource {resource_id} pipelines 无效")
    config_pipelines: dict[str, dict[str, object]] = {}
    for name, pipeline in pipelines.items():
        if not isinstance(name, str) or not _resource_name(name) or not isinstance(pipeline, dict):
            raise ConfigurationError(f"Resource {resource_id} pipeline 无效")
        config_pipelines[name] = _convert_pipeline(pipeline, resource_id=resource_id)
    config_payload = {
        "connection": connection,
        "target": manifest.get("target"),
        "fields": fields,
        "embedding": {
            "model": manifest.get("embedding_alias"),
            "dimensions": manifest.get("embedding_dimensions"),
        },
        "pipelines": config_pipelines,
        "default_pipeline": manifest.get("default_pipeline"),
        "default_return_fields": manifest.get("default_return_fields"),
    }
    try:
        return ResourceConfig.model_validate(config_payload)
    except ValidationError as exc:
        raise ConfigurationError(f"Resource {resource_id} 与 muye-data 配置不兼容：{exc}") from exc


def _convert_pipeline(pipeline: dict[str, Any], *, resource_id: str) -> dict[str, object]:
    """把发布契约的平坦 rerank 字段转换成现有召回服务的嵌套配置。"""
    allowed = {
        "type",
        "candidate_k",
        "dense_candidate_k",
        "keyword_candidate_k",
        "dense_weight",
        "keyword_weight",
        "rank_constant",
        "rerank_model",
        "rerank_required",
    }
    _reject_unknown_keys(pipeline, allowed, f"Resource {resource_id} pipeline")
    result = {key: value for key, value in pipeline.items() if key not in {"rerank_model", "rerank_required"}}
    rerank_model = pipeline.get("rerank_model")
    rerank_required = pipeline.get("rerank_required", False)
    if rerank_model is not None:
        if not isinstance(rerank_model, str) or not _resource_name(rerank_model):
            raise ConfigurationError(f"Resource {resource_id} rerank_model 无效")
        if not isinstance(rerank_required, bool):
            raise ConfigurationError(f"Resource {resource_id} rerank_required 无效")
        result["rerank"] = {"model": rerank_model, "required": rerank_required}
    elif rerank_required is not False:
        raise ConfigurationError(f"Resource {resource_id} required rerank 缺少模型")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON 重复键会使审阅值与实际加载值不一致，必须在解析时拒绝。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 键：{key!r}")
        result[key] = value
    return result


def _reject_unknown_keys(payload: dict[str, Any], allowed: set[str], label: str) -> None:
    """快照格式比本地 config 更严格，拒绝未来字段在旧服务中静默生效。"""
    unknown = set(payload) - allowed
    if unknown:
        raise ConfigurationError(f"{label} 包含未知字段：{sorted(unknown)}")


def _sha256(value: str) -> bool:
    """检查固定长度小写 SHA-256。"""
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _resource_name(value: str) -> bool:
    """复用现有 resource/pipeline 标识边界。"""
    return 1 <= len(value) <= 128 and value[0].isalpha() and all(
        character.isalnum() or character in "_.-" for character in value
    )


def _safe_reference(value: str) -> bool:
    """快照 revision 不得成为路径遍历或 URL 通道。"""
    return (
        _resource_name(value.replace("/", "_"))
        and not value.startswith("/")
        and "://" not in value
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )
