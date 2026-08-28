"""阶段 1 的内容寻址 Artifact 写入器。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from typing import BinaryIO


MAX_ASSET_BYTES = 100 * 1024 * 1024


class AssetValidationError(ValueError):
    """上传内容、文件名或存储布局不满足阶段 1 安全约束。"""


@dataclass(frozen=True, slots=True)
class StoredAsset:
    sha256: str
    size_bytes: int
    storage_key: str
    reused: bool


class ArtifactStore:
    """将上传流原子写入固定根目录的内容寻址存储。"""

    def __init__(self, root: Path, *, max_bytes: int = MAX_ASSET_BYTES) -> None:
        self._root = root.absolute()
        self._max_bytes = max_bytes

    def readiness(self) -> None:
        """验证根目录可创建且不经 symlink 逃逸。"""

        if self._root.is_symlink():
            raise AssetValidationError("Artifact 根目录必须是普通目录")
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise AssetValidationError("Artifact 根目录必须是普通目录")

    def store(self, stream: BinaryIO, *, filename: str) -> StoredAsset:
        """流式计算 hash 后原子发布；失败时不会留下可见半成品。"""

        if not filename or Path(filename).name != filename or filename in {".", ".."}:
            raise AssetValidationError("文件名非法")
        self.readiness()
        digest = sha256()
        size_bytes = 0
        file_descriptor, temp_name = tempfile.mkstemp(prefix="upload-", dir=self._root)
        temporary_path = Path(temp_name)
        try:
            with os.fdopen(file_descriptor, "wb") as destination:
                while chunk := stream.read(64 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > self._max_bytes:
                        raise AssetValidationError("文件超过大小上限")
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            checksum = digest.hexdigest()
            storage_key = f"assets/{checksum[:2]}/{checksum}"
            destination_path = self._root / storage_key
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if destination_path.parent.is_symlink() or not destination_path.parent.is_dir():
                raise AssetValidationError("Asset 目录必须是普通目录")
            if destination_path.exists():
                if destination_path.is_symlink() or not destination_path.is_file():
                    raise AssetValidationError("目标 Asset 不是普通文件")
                return StoredAsset(checksum, size_bytes, storage_key, reused=True)
            os.replace(temporary_path, destination_path)
            return StoredAsset(checksum, size_bytes, storage_key, reused=False)
        finally:
            temporary_path.unlink(missing_ok=True)
