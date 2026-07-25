"""
工具动态注册器
根据配置动态加载启用的工具
"""
import logging
from typing import List
from langchain_core.tools import BaseTool
from config import get_config

logger = logging.getLogger(__name__)


def get_web_search_tools() -> List[BaseTool]:
    """获取网页搜索工具（根据配置动态加载）"""
    config = get_config()
    enabled_engines = getattr(config.web_search, 'enabled_engines', ['ddg'])
    engine_priority = getattr(config.web_search, 'engine_priority', ['langsearch', 'tavily', 'ddg'])

    tools = []

    # 添加智能搜索工具（自动降级）
    try:
        from tools.web.search.fallback import web_search_auto
        tools.append(web_search_auto)
        logger.info(f"✓ 加载 web_search_auto（自动降级），优先级: {' > '.join(engine_priority)}")
    except Exception as e:
        logger.warning(f"Failed to load Auto Search: {e}")

    if 'langsearch' in enabled_engines:
        try:
            from tools.web.search.engines.langsearch import web_search_langsearch
            tools.append(web_search_langsearch)
            logger.info("✓ 加载 web_search_langsearch（支持智能摘要和时效性过滤）")
        except Exception as e:
            logger.warning(f"Failed to load LangSearch: {e}")

    if 'ddg' in enabled_engines:
        try:
            from tools.web.search.engines.ddg import web_search_ddg
            tools.append(web_search_ddg)
            logger.info("✓ 加载 web_search_ddg")
        except Exception as e:
            logger.warning(f"Failed to load DuckDuckGo search: {e}")

    if 'tavily' in enabled_engines:
        try:
            from tools.web.search.engines.tavily import web_search_tavily
            tools.append(web_search_tavily)
            logger.info("✓ 加载 web_search_tavily")
        except Exception as e:
            logger.warning(f"Failed to load Tavily search: {e}")

    if 'infoquest' in enabled_engines:
        try:
            from tools.web.search.engines.infoquest import web_search_infoquest
            tools.append(web_search_infoquest)
            logger.info("✓ 加载 web_search_infoquest")
        except Exception as e:
            logger.warning(f"Failed to load InfoQuest search: {e}")

    if 'serper' in enabled_engines:
        try:
            from tools.web.search.engines.serper import web_search_serper
            tools.append(web_search_serper)
            logger.info("✓ 加载 web_search_serper（Google 搜索结果）")
        except Exception as e:
            logger.warning(f"Failed to load Serper search: {e}")

    return tools


def get_web_fetch_tools() -> List[BaseTool]:
    """获取网页抓取工具"""
    config = get_config()

    tools = []

    if getattr(config.web_fetch, 'enabled', True):
        try:
            from tools.web.fetch.engines.jina import web_fetch
            tools.append(web_fetch)
            logger.info("Loaded Jina web fetch tool")
        except Exception as e:
            logger.warning(f"Failed to load web fetch tool: {e}")

    # 如果启用了 Tavily，也添加 Tavily fetch
    if 'tavily' in getattr(config.web_search, 'enabled_engines', []):
        try:
            from tools.web.fetch.engines.tavily import web_fetch_tavily
            tools.append(web_fetch_tavily)
            logger.info("Loaded Tavily web fetch tool")
        except Exception as e:
            logger.warning(f"Failed to load Tavily fetch: {e}")

    # 如果启用了 InfoQuest，也添加 InfoQuest fetch
    if 'infoquest' in getattr(config.web_search, 'enabled_engines', []):
        try:
            from tools.web.fetch.engines.infoquest import web_fetch_infoquest
            tools.append(web_fetch_infoquest)
            logger.info("Loaded InfoQuest web fetch tool")
        except Exception as e:
            logger.warning(f"Failed to load InfoQuest fetch: {e}")

    return tools


def get_web_tools() -> List[BaseTool]:
    """获取保留的网页检索与受限抓取工具。"""
    tools = []
    tools.extend(get_web_search_tools())
    tools.extend(get_web_fetch_tools())
    logger.info("Loaded %s retained web tools", len(tools))
    return tools
