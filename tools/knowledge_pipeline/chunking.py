"""确定性文本切分和 citation 产物构造。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath

from contracts.models import ChunkingPolicyV1, ParsedBlockV1, ParsedDocumentV1

from .checksums import stable_identifier
from .models import KnowledgeChunkV1


def chunk_documents(
    documents: list[ParsedDocumentV1],
    *,
    policy: ChunkingPolicyV1,
) -> list[KnowledgeChunkV1]:
    """按文档和 block 原始顺序切分，结果不依赖模型、时钟或文件系统遍历顺序。"""
    chunks: list[KnowledgeChunkV1] = []
    for document in documents:
        for block in document.blocks:
            for content in _split_content(block.content, policy):
                chunk_index = len(chunks)
                content_hash = sha256(content.encode("utf-8")).hexdigest()
                chunk_id = stable_identifier(
                    "chunk_",
                    document.knowledge_version_id,
                    document.document_id,
                    block.block_id,
                    str(chunk_index),
                    content_hash,
                )
                chunks.append(
                    KnowledgeChunkV1(
                        chunk_id=chunk_id,
                        knowledge_version_id=document.knowledge_version_id,
                        document_id=document.document_id,
                        source_file_id=document.source_file_id,
                        content=content,
                        title=_document_title(document.source_path),
                        citation_id=stable_identifier("citation_", chunk_id),
                        source_locators=[block.locator],
                        block_ids=[block.block_id],
                        chunk_index=chunk_index,
                        content_hash=content_hash,
                    )
                )
    if not chunks:
        raise ValueError("解析文档未生成任何可发布 chunk")
    return chunks


def _split_content(content: str, policy: ChunkingPolicyV1) -> list[str]:
    """优先在空白处断开，并以固定字符重叠保留相邻上下文。"""
    normalized = " ".join(content.split())
    if len(normalized) <= policy.max_characters:
        return [normalized]
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + policy.max_characters)
        if end < len(normalized):
            boundary = normalized.rfind(" ", start + policy.min_characters, end)
            if boundary > start:
                end = boundary
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(normalized):
            break
        start = max(end - policy.overlap_characters, start + 1)
    return chunks


def _document_title(source_path: str) -> str:
    """使用文件 stem 作为最小展示标题，不从正文抽取或执行任何指令。"""
    title = PurePosixPath(source_path).stem.strip()
    return title or "untitled"
