"""声明式 Runtime Bundle 的安全加载与完整性校验。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from pydantic import ValidationError

from contracts.v3 import (
    AgentRevisionBundleManifestV1,
    AgentRevisionSpecV1,
    RuntimeResourceBindingV1,
    revision_bundle_checksum,
    revision_spec_checksum,
)


class BundleLoadError(ValueError):
    """Bundle 缺失、格式不合法或完整性不匹配时抛出。"""


@dataclass(frozen=True, slots=True)
class LoadedBundle:
    """经校验后可供 Runtime 使用的不可变声明式配置。"""

    manifest: AgentRevisionBundleManifestV1
    revision: AgentRevisionSpecV1
    resources: tuple[RuntimeResourceBindingV1, ...]


_MEMBER_NAMES = frozenset(
    {"manifest.json", "revision.json", "resource-snapshot.json", "evaluation-summary.json"}
)
_MAX_MEMBER_BYTES = 2 * 1024 * 1024


def _read_member(root: Path, name: str) -> bytes:
    """在分配内容缓冲区前限制单个 Bundle 成员大小。"""

    path = root / name
    if path.stat().st_size > _MAX_MEMBER_BYTES:
        raise BundleLoadError("Bundle 成员超过大小上限")
    content = path.read_bytes()
    if len(content) > _MAX_MEMBER_BYTES:
        raise BundleLoadError("Bundle 成员超过大小上限")
    return content


def load_bundle(root: Path) -> LoadedBundle:
    """加载目录 Bundle，并在解析业务成员前校验全部固定成员的 checksum。"""

    if root.is_symlink() or not root.is_dir():
        raise BundleLoadError("Bundle 根目录必须是普通目录")
    entries = list(root.iterdir())
    if {entry.name for entry in entries} != _MEMBER_NAMES:
        raise BundleLoadError("Bundle 成员不完整或包含未知文件")
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise BundleLoadError("Bundle 成员必须是普通文件")
    try:
        manifest = AgentRevisionBundleManifestV1.model_validate_json(_read_member(root, "manifest.json"))
        members = {name: _read_member(root, name) for name in _MEMBER_NAMES - {"manifest.json"}}
        if revision_bundle_checksum(manifest, members) != manifest.bundle_checksum:
            raise BundleLoadError("Bundle checksum 校验失败")
        revision = AgentRevisionSpecV1.model_validate_json(members["revision.json"])
        snapshot = json.loads(members["resource-snapshot.json"])
        evaluation = json.loads(members["evaluation-summary.json"])
    except (OSError, ValidationError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, BundleLoadError):
            raise
        raise BundleLoadError("Bundle 内容格式非法") from exc
    if revision.agent_id != manifest.agent_id or revision.revision_id != manifest.revision_id:
        raise BundleLoadError("Bundle identity 不一致")
    if revision_spec_checksum(revision) != manifest.revision_checksum:
        raise BundleLoadError("Revision checksum 校验失败")
    if not isinstance(evaluation, dict) or evaluation.get("passed") is not True:
        raise BundleLoadError("Bundle 未通过评测门禁")
    try:
        resources = tuple(RuntimeResourceBindingV1.model_validate(item) for item in snapshot["resources"])
    except (KeyError, TypeError, ValidationError) as exc:
        raise BundleLoadError("Resource Snapshot 格式非法") from exc
    if list(resources) != manifest.resources:
        raise BundleLoadError("Resource Snapshot 与 manifest 不一致")
    return LoadedBundle(manifest=manifest, revision=revision, resources=resources)
