"""
Jina AI 网页抓取工具
"""
import logging
from langchain_core.tools import tool
from config import get_config
from tools.web.fetch.clients.jina_client import JinaClient
from tools.web.fetch.readability import ReadabilityExtractor
from utils.url_policy import UnsafeUrlError, validate_external_url

logger = logging.getLogger(__name__)
readability_extractor = ReadabilityExtractor()


@tool("web_fetch", parse_docstring=True)
async def web_fetch(url: str) -> str:
    """抓取指定 URL 的网页内容并提取主要文本

    只抓取用户直接提供的 URL 或从 web_search 返回的 URL。
    无法访问需要认证的内容（如私有 Google Docs、登录墙后的页面）。
    URL 必须包含协议：https://example.com 是有效的，example.com 无效。

    Args:
        url: 要抓取的网页 URL

    Returns:
        Markdown 格式的网页内容
    """
    try:
        safe_url = validate_external_url(url)
    except UnsafeUrlError as exc:
        return f"Error: 不安全的网页 URL：{exc}"

    jina_client = JinaClient()
    config = get_config()
    timeout = getattr(config.web_fetch, 'timeout', 10)
    max_length = config.content_processing.max_content_length

    html_content = await jina_client.crawl(
        safe_url,
        return_format="html",
        timeout=timeout,
        max_response_bytes=config.web_fetch.max_response_bytes,
    )

    if isinstance(html_content, str) and html_content.startswith("Error:"):
        return html_content

    article = readability_extractor.extract_article(html_content, url=safe_url)
    return article.to_markdown()[:max_length]
