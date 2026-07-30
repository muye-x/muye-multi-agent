"""受限本地文件解析器，统一输出 ``ParsedDocumentV1``。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re

from contracts.models import ParsedBlockV1, ParsedDocumentV1, SourceLocatorV1

from .checksums import file_checksum, stable_identifier
from .errors import DependencyUnavailableError, OcrRequiredError, ParserFailedError
from .models import KnowledgeSourceConfigV1


_SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}
_TEXT_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def discover_source_files(config: KnowledgeSourceConfigV1, *, import_root: Path) -> list[Path]:
    """解析受控导入根下的配置路径和 glob，拒绝符号链接与越界来源。"""
    root = import_root.resolve(strict=True)
    if not root.is_dir() or import_root.is_symlink():
        raise ParserFailedError("import_root 必须是非符号链接目录")
    files: dict[str, Path] = {}
    for source in config.sources:
        candidate = _resolve_within_root(root, source.path, description="知识源路径")
        if candidate.is_symlink() or not candidate.exists():
            raise ParserFailedError(f"知识源不存在、不是普通路径或是符号链接：{source.path}")
        if candidate.is_file():
            if candidate.suffix.lower() not in _SUPPORTED_SUFFIXES:
                raise ParserFailedError(f"不支持的知识源扩展名：{candidate.name}")
            if not _matches_includes(candidate, candidate.parent, source.include):
                raise ParserFailedError(f"文件不匹配 sources.include：{source.path}")
            files[candidate.relative_to(root).as_posix()] = candidate
            continue
        if not candidate.is_dir():
            raise ParserFailedError(f"知识源不是文件或目录：{source.path}")
        for pattern in source.include:
            for path in sorted(candidate.glob(pattern)):
                if path.is_symlink():
                    raise ParserFailedError(f"不允许知识源包含符号链接：{path}")
                if not path.is_file() or path.suffix.lower() not in _SUPPORTED_SUFFIXES:
                    continue
                resolved = _resolve_existing_within_root(root, path, description="知识源文件")
                files[resolved.relative_to(root).as_posix()] = resolved
    if not files:
        raise ParserFailedError("未发现匹配的 PDF/DOCX/MD/TXT 知识文件")
    return [files[name] for name in sorted(files)]


def parse_documents(
    paths: Iterable[Path],
    *,
    import_root: Path,
    config: KnowledgeSourceConfigV1,
    knowledge_version_id: str,
    ocr_available: bool = False,
) -> list[ParsedDocumentV1]:
    """依序解析文件，任何一个失败均阻止该 KnowledgeVersion 继续发布。"""
    root = import_root.resolve(strict=True)
    documents = [
        parse_document(
            path,
            import_root=root,
            config=config,
            knowledge_version_id=knowledge_version_id,
            ocr_available=ocr_available,
        )
        for path in paths
    ]
    if not documents:
        raise ParserFailedError("未解析到任何知识文件")
    return documents


def parse_document(
    path: Path,
    *,
    import_root: Path,
    config: KnowledgeSourceConfigV1,
    knowledge_version_id: str,
    ocr_available: bool = False,
) -> ParsedDocumentV1:
    """解析一个常规文件并将位置转换为稳定的相对 citation locator。"""
    root = import_root.resolve(strict=True)
    source = _resolve_existing_within_root(root, path, description="知识源文件")
    if source.is_symlink() or not source.is_file():
        raise ParserFailedError(f"知识源必须是普通文件：{source}")
    file_size = source.stat().st_size
    if file_size <= 0:
        raise ParserFailedError(f"知识源为空：{source.name}")
    if file_size > config.max_file_bytes:
        raise ParserFailedError(f"知识源超过 max_file_bytes：{source.name}")
    suffix = source.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ParserFailedError(f"不支持的知识源扩展名：{source.name}")
    relative_path = source.relative_to(root).as_posix()
    source_hash = file_checksum(source)
    # 同一份字节可出现在不同的受控源文件中；路径是文件身份的一部分，不能让它们共享主键。
    source_file_id = stable_identifier("file_", relative_path, source_hash)
    document_id = stable_identifier("doc_", knowledge_version_id, relative_path, source_hash)
    if suffix in {".md", ".txt"}:
        extracted = _parse_text_document(source, relative_path)
    elif suffix == ".pdf":
        extracted = _parse_pdf(source, relative_path, profile=config.parser_profile, ocr_available=ocr_available)
    else:
        extracted = _parse_docx(source, relative_path, profile=config.parser_profile)
    blocks = [
        ParsedBlockV1(
            block_id=stable_identifier("block_", document_id, str(ordinal), content),
            ordinal=ordinal,
            content=content,
            locator=locator,
        )
        for ordinal, (content, locator) in enumerate(extracted)
    ]
    if not blocks:
        raise ParserFailedError(f"知识源未提取到可发布文本：{relative_path}")
    return ParsedDocumentV1(
        schema_version="muye.ai/parsed-document/v1",
        knowledge_id=config.knowledge_id,
        knowledge_version_id=knowledge_version_id,
        document_id=document_id,
        source_file_id=source_file_id,
        source_path=relative_path,
        source_checksum=source_hash,
        parser_profile=config.parser_profile,
        blocks=blocks,
    )


def _parse_text_document(path: Path, relative_path: str) -> list[tuple[str, SourceLocatorV1]]:
    """确定性解析 Markdown/TXT 的非空段落，并保留原始行范围。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ParserFailedError(f"知识源必须使用 UTF-8：{relative_path}") from exc
    if _TEXT_CONTROL_CHARACTERS.search(raw):
        raise ParserFailedError(f"知识源包含不允许的控制字符：{relative_path}")
    lines = raw.splitlines()
    blocks: list[tuple[str, SourceLocatorV1]] = []
    paragraph: list[str] = []
    start_line = 1
    for line_number, line in enumerate(lines, start=1):
        if line.strip():
            if not paragraph:
                start_line = line_number
            paragraph.append(line.rstrip())
            continue
        _append_text_block(blocks, paragraph, relative_path, start_line, line_number - 1)
        paragraph = []
    _append_text_block(blocks, paragraph, relative_path, start_line, len(lines))
    return blocks


def _append_text_block(
    blocks: list[tuple[str, SourceLocatorV1]],
    lines: list[str],
    relative_path: str,
    start: int,
    end: int,
) -> None:
    """规范化一个文本段落；空白段落不会产生空 block。"""
    content = "\n".join(lines).strip()
    if content:
        blocks.append(
            (
                content,
                SourceLocatorV1(source_path=relative_path, kind="line", start=start, end=max(start, end)),
            )
        )


def _parse_pdf(
    path: Path,
    relative_path: str,
    *,
    profile: str,
    ocr_available: bool,
) -> list[tuple[str, SourceLocatorV1]]:
    """默认以关闭 OCR 的 Docling 解析 PDF；扫描件明确要求 OCR capability。"""
    if profile != "docling-default-v1":
        raise ParserFailedError("PDF 只能使用 docling-default-v1 解析 profile")
    try:
        markdown = _convert_docling(path, enable_ocr=ocr_available)
    except Exception as exc:
        if isinstance(exc, DependencyUnavailableError):
            raise
        raise ParserFailedError(f"Docling 无法解析 PDF：{relative_path}") from exc
    blocks = _text_blocks_from_pages(markdown, relative_path)
    if blocks:
        return blocks
    if not ocr_available:
        raise OcrRequiredError(f"PDF 未提取到文本且未启用 ocr:paddle：{relative_path}")
    raise ParserFailedError(f"OCR Worker 未从扫描 PDF 提取到文本：{relative_path}")


def _parse_docx(path: Path, relative_path: str, *, profile: str) -> list[tuple[str, SourceLocatorV1]]:
    """默认通过 Docling 解析 DOCX，失败不会降级为不透明二进制读取。"""
    if profile != "docling-default-v1":
        raise ParserFailedError("DOCX 只能使用 docling-default-v1 解析 profile")
    try:
        markdown = _convert_docling(path, enable_ocr=False)
    except Exception as exc:
        if isinstance(exc, DependencyUnavailableError):
            raise
        raise ParserFailedError(f"Docling 无法解析 DOCX：{relative_path}") from exc
    blocks = _text_blocks_from_pages(markdown, relative_path)
    if not blocks:
        raise ParserFailedError(f"DOCX 未提取到可发布文本：{relative_path}")
    return blocks


def _convert_docling(path: Path, *, enable_ocr: bool) -> str:
    """按 profile 构造 Docling；仅 OCR Worker capability 请求时才开启 OCR。"""
    try:
        from docling.document_converter import DocumentConverter
        if not enable_ocr:
            return DocumentConverter().convert(str(path)).document.export_to_markdown()
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import PdfFormatOption
    except ModuleNotFoundError as exc:
        raise DependencyUnavailableError(
            "PDF/DOCX 默认解析需要 Docling；安装 requirements-knowledge-docling.txt 后重试"
        ) from exc
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )
    return converter.convert(str(path)).document.export_to_markdown()


def _text_blocks_from_pages(text: str, relative_path: str) -> list[tuple[str, SourceLocatorV1]]:
    """将 Docling Markdown 规范化为段落，并以稳定的段落序号作为 locator。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[tuple[str, SourceLocatorV1]] = []
    for position, paragraph in enumerate(normalized.split("\n\n"), start=1):
        content = paragraph.strip()
        if content:
            blocks.append(
                (
                    content,
                    SourceLocatorV1(source_path=relative_path, kind="paragraph", start=position, end=position),
                )
            )
    return blocks


def _resolve_within_root(root: Path, relative_path: str, *, description: str) -> Path:
    """组合用户配置路径后确认仍位于显式导入根内。"""
    candidate = (root / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ParserFailedError(f"{description} 必须位于 import_root 内") from exc
    return candidate


def _resolve_existing_within_root(root: Path, path: Path, *, description: str) -> Path:
    """解析现有文件并防止中间符号链接逃逸受控导入根。"""
    try:
        candidate = path.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ParserFailedError(f"{description} 必须位于 import_root 内") from exc
    return candidate


def _matches_includes(path: Path, base: Path, patterns: list[str]) -> bool:
    """判断单文件 source 是否满足固定 glob，目录 source 已由 glob 过滤。"""
    del base
    return any(path.suffix.lower() == pattern.removeprefix("**/*").lower() for pattern in patterns)
