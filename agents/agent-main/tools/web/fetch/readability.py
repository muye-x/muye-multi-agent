"""
HTML 内容提取工具（从 deer-flow 迁移）
"""
import logging
import re
import subprocess
from urllib.parse import urljoin

from markdownify import markdownify as md
from readabilipy import simple_json_from_html_string

logger = logging.getLogger(__name__)


class Article:
    """文章对象"""

    def __init__(self, title: str, html_content: str, url: str = ""):
        self.title = title
        self.html_content = html_content
        self.url = url

    def to_markdown(self, including_title: bool = True) -> str:
        """转换为 Markdown 格式"""
        markdown = ""
        if including_title:
            markdown += f"# {self.title}\n\n"

        if self.html_content is None or not str(self.html_content).strip():
            markdown += "*No content available*\n"
        else:
            markdown += md(self.html_content)

        return markdown


class ReadabilityExtractor:
    """HTML 可读性提取器"""

    def extract_article(self, html: str, url: str = "") -> Article:
        """从 HTML 提取文章内容"""
        try:
            article = simple_json_from_html_string(html, use_readability=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning(
                "Readability.js extraction failed, falling back to pure-Python extraction",
                exc_info=True,
            )
            article = simple_json_from_html_string(html, use_readability=False)

        html_content = article.get("content")
        if not html_content or not str(html_content).strip():
            html_content = "No content could be extracted from this page"

        title = article.get("title")
        if not title or not str(title).strip():
            title = "Untitled"

        return Article(title=title, html_content=html_content, url=url)
