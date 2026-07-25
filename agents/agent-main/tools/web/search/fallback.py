"""
搜索降级工具 - 自动尝试多个搜索引擎（支持可配置优先级）
"""
import json
import logging
import asyncio
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


async def _try_langsearch(query: str, max_results: int) -> dict:
    """尝试使用 LangSearch 搜索"""
    try:
        from tools.web.search.engines.langsearch import _search_langsearch
        from config import get_config

        config = get_config()
        api_key = config.web_search.langsearch_api_key

        if not api_key:
            logger.warning("[Auto Search] LangSearch API Key 未配置")
            return None

        logger.info("[Auto Search] 尝试使用 LangSearch 搜索...")
        results = await _search_langsearch(
            query=query,
            max_results=max_results,
            freshness="noLimit",
            api_key=api_key
        )

        if results:
            normalized_results = [
                {
                    "title": r.get("name", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("snippet", ""),
                    "summary": r.get("summary", ""),
                    "datePublished": r.get("datePublished"),
                    "dateLastCrawled": r.get("dateLastCrawled")
                }
                for r in results
            ]
            logger.info(f"[Auto Search] LangSearch 搜索成功 - 返回 {len(normalized_results)} 条结果")
            return {
                "query": query,
                "total_results": len(normalized_results),
                "results": normalized_results,
            }

        logger.warning("[Auto Search] LangSearch 搜索无结果")
        return None

    except Exception as e:
        logger.warning(f"[Auto Search] LangSearch 搜索异常 - {str(e)}")
        return None


async def _try_tavily(query: str, max_results: int) -> dict:
    """尝试使用 Tavily 搜索"""
    try:
        from tools.web.search.engines.tavily import _get_tavily_client
        from config import get_config

        logger.info("[Auto Search] 尝试使用 Tavily 搜索...")
        client = _get_tavily_client()

        if client:
            config = get_config()
            max_results_config = getattr(config.web_search, 'max_results', max_results)

            res = client.search(query, max_results=max_results_config)
            normalized_results = [
                {
                    "title": result["title"],
                    "url": result["url"],
                    "snippet": result["content"],
                }
                for result in res["results"]
            ]

            if normalized_results:
                logger.info(f"[Auto Search] Tavily 搜索成功 - 返回 {len(normalized_results)} 条结果")
                return {
                    "query": query,
                    "total_results": len(normalized_results),
                    "results": normalized_results,
                }

        logger.warning("[Auto Search] Tavily 客户端不可用或无结果")
        return None

    except Exception as e:
        logger.warning(f"[Auto Search] Tavily 搜索异常 - {str(e)}")
        return None


async def _try_serper(query: str, max_results: int) -> dict:
    """尝试使用 Google Serper 搜索"""
    try:
        from tools.web.search.engines.serper import _search_serper
        from config import get_config

        config = get_config()
        api_key = config.web_search.serper_api_key
        base_url = config.web_search.serper_base_url

        if not api_key:
            logger.warning("[Auto Search] Serper API Key 未配置")
            return None

        logger.info("[Auto Search] 尝试使用 Google Serper 搜索...")
        result = await _search_serper(
            query=query,
            max_results=max_results,
            api_key=api_key,
            base_url=base_url
        )

        if result and result.get("results"):
            logger.info(f"[Auto Search] Serper 搜索成功 - 返回 {len(result['results'])} 条结果")
            return {
                "query": query,
                "total_results": result["count"],
                "results": result["results"],
            }

        logger.warning("[Auto Search] Serper 搜索无结果")
        return None

    except Exception as e:
        logger.warning(f"[Auto Search] Serper 搜索异常 - {str(e)}")
        return None


def _try_ddg(query: str, max_results: int) -> dict:
    """尝试使用 DuckDuckGo 搜索"""
    try:
        from tools.web.search.engines.ddg import _search_text

        logger.info("[Auto Search] 尝试使用 DuckDuckGo 搜索...")
        results = _search_text(query=query, max_results=max_results)

        if results:
            normalized_results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", r.get("link", "")),
                    "snippet": r.get("body", r.get("snippet", "")),
                }
                for r in results
            ]

            logger.info(f"[Auto Search] DuckDuckGo 搜索成功 - 返回 {len(normalized_results)} 条结果")
            return {
                "query": query,
                "total_results": len(normalized_results),
                "results": normalized_results,
            }

        logger.warning("[Auto Search] DuckDuckGo 搜索无结果")
        return None

    except Exception as e:
        logger.warning(f"[Auto Search] DuckDuckGo 搜索异常 - {str(e)}")
        return None


@tool("web_search_auto", parse_docstring=True)
async def web_search_auto(query: str, max_results: int = 5) -> str:
    """智能网页搜索（推荐首选）- 自动选择最佳搜索引擎并降级重试

    **核心优势：**
    - 自动按优先级尝试：LangSearch（AI摘要）→ Serper（Google结果）→ Tavily（高质量）→ DuckDuckGo（免费兜底）
    - 确保搜索成功率，一个失败自动切换下一个
    - 无需手动选择引擎，系统自动优化

    **推荐场景（默认首选）：**
    - 不确定用哪个搜索引擎时
    - 需要高可用性的生产环境
    - 通用信息查询（新闻、百科、实时信息等）

    **不推荐场景：**
    - 明确需要 AI 摘要 → 直接用 web_search_langsearch
    - 明确需要时效性过滤 → 直接用 web_search_langsearch（支持 freshness 参数）
    - 预算敏感且可接受基础质量 → 直接用 web_search_ddg

    Args:
        query: 搜索关键词，描述你想查找的内容
        max_results: 返回结果数量，默认 5 条

    Returns:
        JSON 格式的搜索结果，包含 title, url, snippet（可能包含 summary 字段）

    Examples:
        - "2026年奥运会在哪里举办" → 自动选择最佳引擎
        - "iPhone 15 最新评测" → 自动降级确保成功
    """
    from config import get_config

    logger.info(f"[Auto Search] 开始智能搜索 - 查询: '{query}'")

    config = get_config()
    engine_priority = getattr(config.web_search, 'engine_priority', ['serper','langsearch', 'tavily', 'ddg'])

    logger.info(f"[Auto Search] 搜索引擎优先级: {' > '.join(engine_priority)}")

    # 引擎处理器映射
    engine_handlers = {
        'langsearch': _try_langsearch,
        'serper': _try_serper,
        'tavily': _try_tavily,
        'ddg': _try_ddg
    }

    # 按优先级尝试各个搜索引擎
    tried_engines = []
    for engine_name in engine_priority:
        handler = engine_handlers.get(engine_name)
        if not handler:
            logger.warning(f"[Auto Search] 未知的搜索引擎: {engine_name}")
            continue

        tried_engines.append(engine_name)

        # 根据引擎类型调用（async 或 sync）
        if engine_name in ['langsearch', 'tavily', 'serper']:
            result = await handler(query, max_results)
        else:
            result = handler(query, max_results)

        if result:
            return json.dumps(result, indent=2, ensure_ascii=False)

    # 所有搜索引擎都失败
    error_result = {
        "error": "All search engines failed",
        "query": query,
        "message": f"已尝试 {', '.join(tried_engines)}，均无法获取结果"
    }
    logger.error(f"[Auto Search] 所有搜索引擎均失败 - 查询: '{query}'")
    return json.dumps(error_result, ensure_ascii=False)
