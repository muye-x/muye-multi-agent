"""
Google Serper 网页搜索工具
"""
import httpx
import json
import logging
from typing import Optional
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

async def _search_serper(
    query: str,
    max_results: int = 10,
    api_key: Optional[str] = None,
    base_url: str = "https://google.serper.dev"
) -> dict:
    """
    调用 Serper API 进行文字搜索

    Args:
        query: 搜索关键词
        max_results: 最大结果数
        api_key: Serper API Key
        base_url: API 基础 URL

    Returns:
        标准化的搜索结果
    """
    if not api_key:
        logger.warning("Serper API Key 未配置")
        return {"error": "API Key 未配置", "results": []}

    try:
        headers = {
            'X-API-KEY': api_key,
            'Content-Type': 'application/json'
        }

        payload = {
            "q": query,
            "num": max_results
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{base_url}/search",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()

        # 标准化结果（与其他搜索引擎格式一致）
        results = []
        for item in data.get("organic", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "position": item.get("position", 0)
            })

        logger.info(f"Serper 搜索成功: query='{query}', results={len(results)}")
        return {
            "query": query,
            "results": results,
            "count": len(results)
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"Serper API 请求失败: {e.response.status_code} - {e.response.text}")
        return {"error": f"HTTP {e.response.status_code}", "results": []}
    except Exception as e:
        logger.error(f"Serper 搜索异常: {e}", exc_info=True)
        return {"error": str(e), "results": []}

@tool("web_search_serper", parse_docstring=True)
async def web_search_serper(
    query: str,
    max_results: int = 10
) -> str:
    """使用 Google Serper 搜索网页 - 基于 Google 搜索结果

    **核心优势：**
    - 🔍 真实 Google 搜索结果（非爬虫）
    - ⚡ 快速响应，无需浏览器渲染
    - 💰 成本低（相比 SerpAPI）

    **推荐场景：**
    - 需要权威搜索结果（Google 质量）
    - 简单查询不需要 AI 摘要时

    Args:
        query: 搜索关键词
        max_results: 返回的最大结果数，默认 10

    Returns:
        JSON 格式的搜索结果，包含 title, url, snippet, position
    """
    from config import get_config

    config = get_config()
    api_key = config.web_search.serper_api_key
    base_url = config.web_search.serper_base_url

    if not api_key:
        return json.dumps({
            "error": "Serper API Key 未配置",
            "results": []
        }, ensure_ascii=False, indent=2)

    result = await _search_serper(
        query=query,
        max_results=max_results,
        api_key=api_key,
        base_url=base_url
    )

    return json.dumps(result, ensure_ascii=False, indent=2)
