"""
InfoQuest 网页搜索工具（需要 API Key）
"""
import logging
from langchain_core.tools import tool
from config import get_config
from tools.web.search.clients.infoquest_client import InfoQuestClient

logger = logging.getLogger(__name__)


def _get_infoquest_client() -> InfoQuestClient:
    """获取 InfoQuest 客户端"""
    config = get_config()
    search_time_range = getattr(config.web_search, 'infoquest_time_range', -1)

    return InfoQuestClient(search_time_range=search_time_range)


@tool("web_search_infoquest", parse_docstring=True)
def web_search_infoquest(query: str) -> str:
    """使用 InfoQuest 搜索网页（BytePlus，需要 API Key）

    Args:
        query: 搜索关键词

    Returns:
        JSON 格式的搜索结果
    """
    client = _get_infoquest_client()
    return client.web_search(query)
