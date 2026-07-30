"""Generator 的受限 YAML/JSON 读取器和路径辅助函数。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError
import yaml

from contracts.models import ContractModel


ModelT = TypeVar("ModelT", bound=ContractModel)


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝 YAML 重复键，避免配置审阅值与实际生效值不同。"""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    """在 PyYAML 构造 mapping 时检测每个键的重复定义。"""
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """在 JSON 解析阶段拒绝重复键，避免 provenance 审阅与实际值不一致。"""
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"发现重复 JSON 键：{key!r}")
        value[key] = item
    return value


def load_yaml_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """读取唯一键 YAML 并执行严格 Pydantic 校验。"""
    _reject_symlink(path)
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"无法读取 YAML 配置 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"YAML 配置必须是对象：{path}")
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"YAML 配置不符合 {model_type.__name__}：{path}\n{exc}") from exc


def load_json_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """读取唯一键 JSON 并执行严格 Pydantic 校验。"""
    _reject_symlink(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"无法读取 JSON 配置 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 配置必须是对象：{path}")
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"JSON 配置不符合 {model_type.__name__}：{path}\n{exc}") from exc


def write_json(path: Path, value: object) -> None:
    """以稳定、可审阅的 JSON 格式写入已在 staging 目录内的生成文件。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def assert_path_within(path: Path, root: Path, *, description: str) -> Path:
    """解析现有路径并确认其仍位于受控 root，拒绝符号链接逃逸。"""
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{description} 必须位于 {resolved_root} 内") from exc
    return resolved_path


def target_exists(path: Path) -> bool:
    """将 broken symlink 也视为占用目标，避免目录生成覆盖它。"""
    return os.path.lexists(path)


def _reject_symlink(path: Path) -> None:
    """读取配置前拒绝符号链接，确保审阅的文件就是 Generator 使用的文件。"""
    if path.is_symlink():
        raise ValueError(f"不允许读取符号链接：{path}")
