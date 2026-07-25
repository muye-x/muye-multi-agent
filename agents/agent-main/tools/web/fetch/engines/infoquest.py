"""
InfoQuest 网页抓取工具
"""
import logging
from langchain_core.tools import tool
from config import get_config
from tools.web.search.clients.infoquest_client import InfoQuestClient
from utils.url_policy import UnsafeUrlError, validate_external_url

logger = logging.getLogger(__name__)


@tool("web_fetch_infoquest", parse_docstring=True)
def web_fetch_infoquest(url: str) -> str:
    """使用 InfoQuest 抓取网页内容

    Args:
        url: 要抓取的网页 URL

    Returns:
        网页内容（HTML 格式）
    """
    try:
        safe_url = validate_external_url(url)
    except UnsafeUrlError as exc:
        return f"Error: 不安全的网页 URL：{exc}"
    config = get_config()
    fetch_timeout = getattr(config.web_fetch, 'infoquest_timeout', -1)
    fetch_time = getattr(config.web_fetch, 'infoquest_fetch_time', -1)
    navigation_timeout = getattr(config.web_fetch, 'infoquest_navigation_timeout', -1)

    client = InfoQuestClient(
        fetch_timeout=fetch_timeout,
        fetch_time=fetch_time,
        fetch_navigation_timeout=navigation_timeout
    )

    return client.fetch(safe_url)
