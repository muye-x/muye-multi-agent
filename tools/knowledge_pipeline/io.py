"""Knowledge Worker 受控 artifact 目录的读取与原子写入。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import TypeVar

from pydantic import ValidationError
import yaml

from contracts.models import ContractModel


ModelT = TypeVar("ModelT", bound=ContractModel)


class _UniqueKeyLoader(yaml.SafeLoader):
    """在 YAML 边界拒绝重复键，避免审阅值被后写值覆盖。"""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    """显式检测 mapping 的重复键。"""
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "配置键必须是字符串",
                key_node.start_mark,
            )
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"发现重复键：{key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_yaml_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """读取非符号链接 YAML，并对类型和未知字段执行严格校验。"""
    _reject_symlink(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取 YAML 文件：{path}") from exc
    if len(raw) > 1_048_576:
        raise ValueError(f"YAML 文件不能超过 1 MiB：{path}")
    try:
        payload = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"YAML 文件无效：{path}") from exc
    return _validate_payload(payload, model_type, path)


def load_json_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """读取非符号链接 JSON，并拒绝重复键。"""
    _reject_symlink(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取 JSON 文件：{path}") from exc
    if len(raw) > 4_194_304:
        raise ValueError(f"JSON 文件不能超过 4 MiB：{path}")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"JSON 文件无效：{path}") from exc
    return _validate_payload(payload, model_type, path)


def write_json_atomic(path: Path, value: object) -> None:
    """在受控非符号链接目录中通过 replace 原子发布 JSON artifact。"""
    _ensure_parent(path.parent)
    if path.is_symlink():
        raise ValueError(f"不允许写入符号链接：{path}")
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _validate_payload(payload: object, model_type: type[ModelT], path: Path) -> ModelT:
    """将解析结果限定为对象并保留 Pydantic 的字段级错误。"""
    if not isinstance(payload, dict):
        raise ValueError(f"配置根节点必须是对象：{path}")
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"配置不符合 {model_type.__name__}：{path}\n{exc}") from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """在 JSON 反序列化时拒绝重复键。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"发现重复 JSON 键：{key!r}")
        result[key] = value
    return result


def _ensure_parent(path: Path) -> None:
    """创建 artifact 父目录，同时拒绝路径中已有的符号链接。"""
    anchor = path.anchor
    current = Path(anchor) if anchor else Path()
    for part in path.parts[len(Path(anchor).parts) :] if anchor else path.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError(f"artifact 目录不能是符号链接：{current}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"artifact 父路径不是普通目录：{path}")


def _reject_symlink(path: Path) -> None:
    """读取前拒绝最后一段符号链接，避免审阅对象与读取对象不一致。"""
    if path.is_symlink():
        raise ValueError(f"不允许读取符号链接：{path}")
