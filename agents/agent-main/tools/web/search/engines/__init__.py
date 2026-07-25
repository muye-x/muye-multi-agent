"""网页搜索引擎包。"""
from .ddg import web_search_ddg
from .langsearch import web_search_langsearch
from .serper import web_search_serper
from .tavily import web_search_tavily
from .infoquest import web_search_infoquest

__all__ = [
    'web_search_ddg',
    'web_search_langsearch',
    'web_search_serper',
    'web_search_tavily',
    'web_search_infoquest',
]
