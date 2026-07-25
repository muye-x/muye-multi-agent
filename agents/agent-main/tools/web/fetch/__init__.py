"""
网页抓取工具模块
"""
from .engines.jina import web_fetch
from .engines.tavily import web_fetch_tavily
from .engines.infoquest import web_fetch_infoquest

__all__ = [
    'web_fetch',
    'web_fetch_tavily',
    'web_fetch_infoquest',
]
