"""
Tavily 网页搜索工具（需要 API Key）
"""
import json
import logging
from langchain_core.tools import tool
from config import get_config

logger = logging.getLogger(__name__)


def _get_tavily_client():
    """获取 Tavily 客户端"""
    try:
        from tavily import TavilyClient
    except ImportError:
        logger.error("tavily-python not installed. Run: pip install tavily-python")
        return None

    config = get_config()
    api_key = getattr(config.web_search, 'tavily_api_key', None)

    if not api_key:
        logger.error("TAVILY_API_KEY not configured")
        return None

    return TavilyClient(api_key=api_key)


@tool("web_search_tavily", parse_docstring=True)
def web_search_tavily(query: str) -> str:
    """使用 Tavily 搜索网页（需要 API Key，搜索质量高）

    Args:
        query: 搜索关键词

    Returns:
        JSON 格式的搜索结果
    """
    logger.info(f"[Tavily Search] 开始搜索 - 查询: '{query}'")

    client = _get_tavily_client()
    if not client:
        error_msg = "Tavily client not available"
        logger.error(f"[Tavily Search] 初始化失败 - {error_msg}")
        return json.dumps({"error": error_msg}, ensure_ascii=False)

    config = get_config()
    max_results = getattr(config.web_search, 'max_results', 5)

    logger.info(f"[Tavily Search] 配置 - 最大结果数: {max_results}")

    try:
        res = client.search(query, max_results=max_results)

        normalized_results = [
            {
                "title": result["title"],
                "url": result["url"],
                "snippet": result["content"],
            }
            for result in res["results"]
        ]

        logger.info(f"[Tavily Search] 搜索成功 - 返回 {len(normalized_results)} 条结果")
        logger.debug(f"[Tavily Search] 结果预览: {[r['title'] for r in normalized_results[:3]]}")

        output = {
            "query": query,
            "total_results": len(normalized_results),
            "results": normalized_results,
        }

        return json.dumps(output, indent=2, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[Tavily Search] 搜索失败 - 错误: {str(e)}")
        return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)
