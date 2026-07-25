"""网页抓取引擎包。"""
from .jina import web_fetch
from .tavily import web_fetch_tavily
from .infoquest import web_fetch_infoquest

__all__ = [
    'web_fetch',
    'web_fetch_tavily',
    'web_fetch_infoquest',
]
