"""生成器使用的稳定序列化与 SHA-256 工具。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping


PROVENANCE_FILE_NAME = ".muye-generation.json"
_RUNTIME_ARTIFACT_DIRECTORIES = {"__pycache__", ".pytest_cache"}
_ENV_FILE_PATTERNS = {".env", ".env.local", ".env.production"}


def canonical_checksum(value: object) -> str:
    """计算 JSON 兼容值的稳定 SHA-256，供输入和来源记录重放。"""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def source_tree_checksum(directory: Path) -> str:
    """计算目录的稳定源码树 checksum，并排除含生成时间的 provenance 和环境变量文件。"""
    files = _regular_files(directory)
    digest = sha256()
    for relative_path, path in files.items():
        if relative_path == PROVENANCE_FILE_NAME:
            continue
        if _is_env_file(relative_path):
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _is_env_file(relative_path: str) -> bool:
    """检查文件是否为环境变量配置文件（排除 .env.example 模板文件）。"""
    parts = relative_path.split("/")
    filename = parts[-1]
    if filename.endswith(".example"):
        return False
    return filename in _ENV_FILE_PATTERNS or filename.startswith(".env.")


def read_source_tree(directory: Path) -> dict[str, str]:
    """读取生成目录内的 UTF-8 文件，供只读 diff 使用。"""
    tree: dict[str, str] = {}
    for relative_path, path in _regular_files(directory).items():
        if relative_path == PROVENANCE_FILE_NAME:
            continue
        if _is_env_file(relative_path):
            continue
        try:
            tree[relative_path] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"生成文件必须使用 UTF-8：{relative_path}") from exc
    return tree


def _regular_files(directory: Path) -> Mapping[str, Path]:
    """列出目录内的常规文件，并拒绝 symlink，避免 checksum 绕过目录边界。"""
    if directory.is_symlink():
        raise ValueError(f"目录不能是符号链接：{directory}")
    if not directory.is_dir():
        raise ValueError(f"目录不存在或不是目录：{directory}")

    files: dict[str, Path] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"目录不能包含符号链接：{path}")
        if _RUNTIME_ARTIFACT_DIRECTORIES.intersection(path.parts) or path.suffix == ".pyc":
            continue
        if path.is_file():
            relative_path = path.relative_to(directory).as_posix()
            files[relative_path] = path
    return files
