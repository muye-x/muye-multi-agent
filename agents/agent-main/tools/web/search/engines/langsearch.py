"""
LangSearch 网页搜索工具
支持基于模型的智能摘要和时效性过滤
"""
import logging
import httpx
from typing import Literal, Optional, List, Dict, Any
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# LangSearch API 配置
LANGSEARCH_API_URL = "https://api.langsearch.com/v1/web-search"
LANGSEARCH_TIMEOUT = 30


async def _search_langsearch(
    query: str,
    max_results: int = 5,
    freshness: Literal["noLimit", "day", "week", "month", "year"] = "noLimit",
    api_key: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    调用 LangSearch API 进行搜索

    Args:
        query: 搜索查询
        max_results: 最大结果数
        freshness: 时效性过滤（noLimit/day/week/month/year）
        api_key: LangSearch API Key

    Returns:
        List[Dict]: 标准化的搜索结果列表
    """
    if not api_key:
        logger.warning("LangSearch API Key 未配置")
        return []

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "query": query,
            "count": max_results,
            "summary": True,
            "freshness": freshness
        }

        async with httpx.AsyncClient(timeout=LANGSEARCH_TIMEOUT) as client:
            response = await client.post(
                LANGSEARCH_API_URL,
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()

        # 检查响应状态
        if data.get("code") != 200:
            logger.error(f"LangSearch API 返回错误: {data.get('msg', 'Unknown error')}")
            return []

        # 标准化结果格式（从 data.webPages.value 中提取）
        results = []
        web_pages = data.get("data", {}).get("webPages", {})
        items = web_pages.get("value", [])

        for item in items:
            result = {
                "name": item.get("name", ""),
                "url": item.get("url", ""),  # 保留 URL，便于用户访问原网页。
                "snippet": item.get("snippet", ""),  # 保留 snippet 作为摘要降级数据。
                "summary": item.get("summary", ""),  # AI 摘要（核心优势）
                "datePublished": item.get("datePublished"),
                "dateLastCrawled": item.get("dateLastCrawled")
            }
            results.append(result)

        logger.info(f"LangSearch 搜索成功: query='{query}', results={len(results)}, freshness={freshness}")
        return results

    except httpx.HTTPStatusError as e:
        logger.error(f"LangSearch API 请求失败: {e.response.status_code} - {e.response.text}")
        return []
    except httpx.TimeoutException:
        logger.error(f"LangSearch API 请求超时: query='{query}'")
        return []
    except Exception as e:
        logger.error(f"LangSearch 搜索异常: {e}", exc_info=True)
        return []


@tool("web_search_langsearch", parse_docstring=True)
async def web_search_langsearch(
    query: str,
    max_results: int = 5,
    freshness: Literal["noLimit", "day", "week", "month", "year"] = "noLimit"
) -> str:
    """使用 LangSearch 搜索网页 - 专为 AI 优化，提供智能摘要和时效性过滤

    **核心优势（独有功能）：**
    - ✨ **模型智能摘要**：每个结果都有 LLM 生成的精炼摘要（summary 字段）
    - ⏰ **时效性过滤**：可精确控制"最近一天/周/月/年"的结果
    - 🎯 **语义 Rerank**：结果按语义相关性重新排序，更准确

    **推荐场景（明确需要以下功能时直接使用）：**
    - 需要 AI 摘要减少后续处理（如生成报告、对比分析）
    - 需要时效性过滤（如"最近一周的新闻"、"今天的股价"）
    - 复杂查询需要语义理解（如"如何提高睡眠质量"）

    **不推荐场景：**
    - 简单查询且不需要摘要 → 用 web_search_auto（更快）
    - 预算敏感 → 用 web_search_ddg（免费）

    **freshness 参数使用指南：**
    - "day" → 用户明确要求"今天"、"最新"、"刚刚"
    - "week" → 用户要求"最近"、"近期"、"这周"
    - "month" → 用户要求"本月"、"这个月"
    - "year" → 用户要求"今年"、"2026年"
    - "noLimit" → 默认值，不限时间（百科、历史信息等）

    Args:
        query: 搜索查询关键词
        max_results: 返回的最大结果数，默认 5
        freshness: 时效性过滤，默认 noLimit（不限）

    Returns:
        JSON 格式的搜索结果，包含 name, url, snippet, summary（AI摘要）, datePublished, dateLastCrawled

    Examples:
        - "最近一周的 AI 新闻" + freshness="week" → 获取最新新闻 + AI 摘要
        - "如何学习 Python" + freshness="noLimit" → 获取高质量教程 + AI 摘要
    """
    from config import get_config
    import json

    config = get_config()
    api_key = config.web_search.langsearch_api_key

    if not api_key:
        return json.dumps({
            "error": "LangSearch API Key 未配置",
            "results": []
        }, ensure_ascii=False, indent=2)

    results = await _search_langsearch(
        query=query,
        max_results=max_results,
        freshness=freshness,
        api_key=api_key
    )

    return json.dumps({
        "query": query,
        "freshness": freshness,
        "results": results,
        "count": len(results)
    }, ensure_ascii=False, indent=2)
