"""
DuckDuckGo 网页搜索工具（免费，无需 API Key）
"""
import json
import logging
from langchain_core.tools import tool
from config import get_config

logger = logging.getLogger(__name__)


def _search_text(
    query: str,
    max_results: int = 5,
    region: str = "wt-wt",
    safesearch: str = "moderate",
) -> list[dict]:
    """执行 DuckDuckGo 文本搜索"""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.error("DDG 搜索依赖未安装。请安装 ddgs。")
            return []

    ddgs = DDGS(timeout=30)

    try:
        results = ddgs.text(
            query,
            region=region,
            safesearch=safesearch,
            max_results=max_results,
        )
        return list(results) if results else []

    except Exception as e:
        logger.error(f"Failed to search web: {e}")
        return []


@tool("web_search_ddg", parse_docstring=True)
def web_search_ddg(query: str, max_results: int = 5) -> str:
    """使用 DuckDuckGo 搜索网页信息（免费，无需 API Key）

    Args:
        query: 搜索关键词，描述你想查找的内容
        max_results: 返回结果数量，默认 5 条

    Returns:
        JSON 格式的搜索结果
    """
    logger.info(f"[DuckDuckGo Search] 开始搜索 - 查询: '{query}', 最大结果数: {max_results}")

    config = get_config()
    max_results = getattr(config.web_search, 'max_results', max_results)

    results = _search_text(query=query, max_results=max_results)

    if not results:
        logger.warning(f"[DuckDuckGo Search] 搜索失败 - 未找到结果: '{query}'")
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "snippet": r.get("body", r.get("snippet", "")),
        }
        for r in results
    ]

    logger.info(f"[DuckDuckGo Search] 搜索成功 - 返回 {len(normalized_results)} 条结果")
    logger.debug(f"[DuckDuckGo Search] 结果预览: {[r['title'] for r in normalized_results[:3]]}")

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }

    return json.dumps(output, indent=2, ensure_ascii=False)
