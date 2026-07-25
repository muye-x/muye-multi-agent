"""Web 工具模块 - 搜索与受限网页抓取。"""
from .search import (
    web_search_ddg,
    web_search_langsearch,
    web_search_tavily,
    web_search_infoquest,
    web_search_serper,
    web_search_auto,
)
from .fetch import (
    web_fetch,
    web_fetch_tavily,
    web_fetch_infoquest,
)
__all__ = [
    'web_search_ddg',
    'web_search_langsearch',
    'web_search_tavily',
    'web_search_infoquest',
    'web_search_serper',
    'web_search_auto',
    'web_fetch',
    'web_fetch_tavily',
    'web_fetch_infoquest',
]
