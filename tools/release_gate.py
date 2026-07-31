"""v2.0 发布证据的离线校验器。

该模块不创建 tag、镜像或发布记录；它只验证操作者提供的 Alpha/RC/正式发布证据是否仍与
当前 SDK、模板和 JSON Schema 一致，并拒绝本机 Docker image ID 伪装成 registry manifest digest。
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import re
from typing import Sequence

import yaml

from tools.agent_generator.checksums import canonical_checksum, read_source_tree


_CHECKSUM_PATTERN = re.compile(r"[a-f0-9]{64}")
_IMAGE_PATTERN = re.compile(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}")
_STAGES = frozenset({"alpha", "rc", "release"})


def verify_evidence(path: Path, *, workspace_root: Path) -> None:
    """验证一份不可变发布证据与当前受控源树一致。

    ``path`` 必须是普通 YAML 文件。失败即抛出 ``ValueError``，调用方不得继续将该证据
    标记为通过；成功只证明源冻结一致，不替代目标环境 E2E 演练。
    """
    if path.is_symlink() or not path.is_file():
        raise ValueError("发布证据必须是普通 YAML 文件")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("发布证据不是合法 YAML") from exc
    if not isinstance(payload, dict):
        raise ValueError("发布证据必须是对象")
    required = {"schema_version", "release_version", "stage", "sdk_version", "template", "schema_checksums", "images", "catalog_checksum", "verification_refs"}
    if set(payload) != required:
        raise ValueError("发布证据字段不完整或包含未知字段")
    if payload["schema_version"] != "muye.ai/release-evidence/v1":
        raise ValueError("发布证据 schema_version 不支持")
    if not isinstance(payload["release_version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", payload["release_version"]):
        raise ValueError("release_version 必须是 SemVer")
    if payload["stage"] not in _STAGES:
        raise ValueError("stage 必须是 alpha、rc 或 release")
    _verify_sdk(payload["sdk_version"], workspace_root)
    _verify_template(payload["template"], workspace_root)
    _verify_schemas(payload["schema_checksums"], workspace_root)
    _verify_images(payload["images"])
    _require_checksum(payload["catalog_checksum"], "catalog_checksum")
    refs = payload["verification_refs"]
    if not isinstance(refs, list) or not refs or not all(isinstance(item, str) and item.strip() for item in refs):
        raise ValueError("verification_refs 必须包含至少一条不可变验证记录引用")


def _verify_sdk(value: object, workspace_root: Path) -> None:
    if not isinstance(value, str):
        raise ValueError("sdk_version 必须是字符串")
    manifest = _load_template_manifest(workspace_root)
    if value != manifest.get("sdk_version"):
        raise ValueError("发布证据的 sdk_version 与模板 manifest 不一致")


def _verify_template(value: object, workspace_root: Path) -> None:
    if not isinstance(value, dict) or set(value) != {"id", "version", "source_tree_checksum"}:
        raise ValueError("template 必须包含 id、version 和 source_tree_checksum")
    manifest = _load_template_manifest(workspace_root)
    template_root = workspace_root / "templates" / "agents" / "react-knowledge" / "v1"
    if value["id"] != manifest.get("template_id") or value["version"] != manifest.get("template_version"):
        raise ValueError("发布证据的 template identity 与 manifest 不一致")
    expected = canonical_checksum(read_source_tree(template_root))
    if value["source_tree_checksum"] != expected:
        raise ValueError("发布证据的模板源码 checksum 已漂移")


def _verify_schemas(value: object, workspace_root: Path) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError("schema_checksums 不能为空")
    schema_root = workspace_root / "contracts" / "schemas"
    for relative_path, checksum in value.items():
        if not isinstance(relative_path, str) or Path(relative_path).name != relative_path or not relative_path.endswith(".json"):
            raise ValueError("schema_checksums 只能引用 schemas 根目录的 JSON 文件")
        _require_checksum(checksum, f"schema checksum {relative_path}")
        source = schema_root / relative_path
        if not source.is_file() or sha256(source.read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Schema checksum 已漂移：{relative_path}")


def _verify_images(value: object) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError("images 不能为空")
    for service, reference in value.items():
        if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", service):
            raise ValueError("images 服务名无效")
        if not isinstance(reference, str) or _IMAGE_PATTERN.fullmatch(reference) is None:
            raise ValueError("images 必须使用 registry repository@sha256 manifest digest")


def _require_checksum(value: object, name: str) -> None:
    if not isinstance(value, str) or _CHECKSUM_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} 必须是 sha256 hex")


def _load_template_manifest(workspace_root: Path) -> dict[str, object]:
    path = workspace_root / "templates" / "agents" / "react-knowledge" / "v1" / "template-manifest.yaml"
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError("模板 manifest 无效")
    return content


def main(argv: Sequence[str] | None = None, *, workspace_root: Path | None = None) -> int:
    """提供 ``muye.sh release verify <evidence.yaml>`` 入口。"""
    parser = argparse.ArgumentParser(prog="muye.sh release")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("evidence", type=Path)
    arguments = parser.parse_args(argv)
    root = (workspace_root or Path.cwd()).resolve(strict=True)
    if arguments.command == "verify":
        verify_evidence(arguments.evidence.resolve(strict=True), workspace_root=root)
        print("release evidence: verified")
    return 0
