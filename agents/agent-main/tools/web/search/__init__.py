"""
网页搜索工具模块
"""
from .engines.ddg import web_search_ddg
from .engines.langsearch import web_search_langsearch
from .engines.tavily import web_search_tavily
from .engines.infoquest import web_search_infoquest
from .engines.serper import web_search_serper
from .fallback import web_search_auto

__all__ = [
    'web_search_ddg',
    'web_search_langsearch',
    'web_search_tavily',
    'web_search_infoquest',
    'web_search_serper',
    'web_search_auto',
]
