"""阶段 1 的确定性 Markdown 解析器。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

from .contracts import ParsedBlockV1, ParsedDocumentV1, SourceLocatorV1


MAX_SOURCE_BYTES = 1_048_576
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parse_markdown_file(path: Path, *, source_root: Path) -> ParsedDocumentV1:
    """将一个受控目录内的 UTF-8 Markdown 文件转为稳定块序列。

    解析器只支持 `.md` 和 `.markdown`，会拒绝符号链接逃逸、空文档和超过 PoC 预算的
    输入。每个一级至六级标题开启新块，未命名的前导内容归入文件名标题。
    """
    root = source_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        relative_path = resolved_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Markdown 来源必须位于 source_root 内") from exc
    if resolved_path.suffix.lower() not in {".md", ".markdown"}:
        raise ValueError("阶段 1 PoC 仅支持 Markdown 文件")

    raw_bytes = resolved_path.read_bytes()
    if not raw_bytes:
        raise ValueError("Markdown 文件不能为空")
    if len(raw_bytes) > MAX_SOURCE_BYTES:
        raise ValueError("Markdown 文件超过阶段 1 PoC 的 1 MiB 上限")
    try:
        normalized = raw_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ValueError("Markdown 文件必须使用 UTF-8") from exc
    if not normalized.strip():
        raise ValueError("Markdown 文件不能只包含空白")

    source_path = relative_path.as_posix()
    blocks = _split_blocks(normalized, source_path=source_path, default_heading=resolved_path.stem)
    title = _document_title(blocks, fallback=resolved_path.stem)
    return ParsedDocumentV1(
        schema_version="muye.ai/poc-parsed-document/v1",
        document_id=f"doc_{_checksum(source_path)[:16]}",
        title=title,
        source_path=source_path,
        source_checksum=_checksum_bytes(raw_bytes),
        content_checksum=_checksum(normalized),
        blocks=blocks,
    )


def _split_blocks(content: str, *, source_path: str, default_heading: str) -> list[ParsedBlockV1]:
    """按 Markdown 标题分块，并在块标识中包含路径、行号和正文。"""
    lines = content.split("\n")
    current_heading = default_heading
    current_start_line = 1
    current_lines: list[str] = []
    blocks: list[ParsedBlockV1] = []

    def flush(end_line: int) -> None:
        normalized_content = "\n".join(current_lines).strip()
        if not normalized_content:
            return
        ordinal = len(blocks)
        identity = f"{source_path}\n{current_start_line}\n{normalized_content}"
        blocks.append(
            ParsedBlockV1(
                block_id=f"block_{_checksum(identity)[:16]}",
                ordinal=ordinal,
                heading=current_heading,
                content=normalized_content,
                source_locator=SourceLocatorV1(
                    path=source_path,
                    start_line=current_start_line,
                    end_line=end_line,
                ),
            )
        )

    for line_number, line in enumerate(lines, start=1):
        heading = _HEADING_PATTERN.match(line)
        if heading is None:
            current_lines.append(line)
            continue
        flush(line_number - 1)
        current_heading = heading.group(2).strip()
        current_start_line = line_number
        current_lines = [line]
    flush(len(lines))

    if not blocks:
        raise ValueError("Markdown 文件不包含可用文本块")
    return blocks


def _document_title(blocks: list[ParsedBlockV1], *, fallback: str) -> str:
    """优先选择第一个一级标题，否则保留调用方提供的文件名标题。"""
    for block in blocks:
        first_line = block.content.split("\n", maxsplit=1)[0]
        heading = _HEADING_PATTERN.match(first_line)
        if heading is not None and len(heading.group(1)) == 1:
            return heading.group(2).strip()
    return fallback.strip() or "未命名知识文档"


def _checksum(value: str) -> str:
    """计算 UTF-8 文本的 SHA-256。"""
    return _checksum_bytes(value.encode("utf-8"))


def _checksum_bytes(value: bytes) -> str:
    """计算任意字节内容的 SHA-256。"""
    return sha256(value).hexdigest()
