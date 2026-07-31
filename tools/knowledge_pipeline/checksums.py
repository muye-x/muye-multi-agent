"""Knowledge Worker 使用的稳定序列化和文件 checksum 工具。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any


def canonical_checksum(value: object) -> str:
    """计算 JSON 兼容值的稳定 SHA-256，不依赖墙上时钟或字典插入顺序。"""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode("utf-8")).hexdigest()


def file_checksum(path: Path) -> str:
    """流式计算常规文件 SHA-256，避免大文件解析前整块读入内存。"""
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_identifier(prefix: str, *parts: str, length: int = 32) -> str:
    """由不可变输入派生稳定 ID，供 document/block/chunk/citation 溯源。"""
    digest = sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:length]}"


def verify_declared_checksum(model: Any, *, checksum_field: str, label: str) -> None:
    """重新计算版本化 artifact checksum，拒绝仅修改正文而保留旧声明值的文件。"""
    payload = model.model_dump(mode="json")
    declared = payload.pop(checksum_field, None)
    actual = canonical_checksum(payload)
    if declared != actual:
        raise ValueError(f"{label} checksum 不匹配")
