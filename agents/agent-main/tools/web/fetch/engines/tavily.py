"""
Tavily 网页抓取工具
"""
import logging
from langchain_core.tools import tool
from config import get_config
from utils.url_policy import UnsafeUrlError, validate_external_url

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


@tool("web_fetch_tavily", parse_docstring=True)
def web_fetch_tavily(url: str) -> str:
    """使用 Tavily 抓取网页内容

    Args:
        url: 要抓取的网页 URL

    Returns:
        网页内容（Markdown 格式）
    """
    try:
        safe_url = validate_external_url(url)
    except UnsafeUrlError as exc:
        return f"Error: 不安全的网页 URL：{exc}"
    logger.info(f"[Tavily Fetch] 开始抓取 - URL: {safe_url}")

    client = _get_tavily_client()
    if not client:
        error_msg = "Tavily client not available"
        logger.error(f"[Tavily Fetch] 初始化失败 - {error_msg}")
        return f"Error: {error_msg}"

    config = get_config()

    try:
        res = client.extract([safe_url])

        if "failed_results" in res and len(res["failed_results"]) > 0:
            error = res['failed_results'][0]['error']
            logger.error(f"[Tavily Fetch] 抓取失败 - URL: {url}, 错误: {error}")
            return f"Error: {error}"
        elif "results" in res and len(res["results"]) > 0:
            result = res["results"][0]
            content_length = len(result['raw_content'])
            max_length = config.content_processing.max_content_length
            logger.info(f"[Tavily Fetch] 抓取成功 - 标题: {result['title']}, 内容长度: {content_length} 字符")
            return f"# {result['title']}\n\n{result['raw_content'][:max_length]}"
        else:
            logger.warning(f"[Tavily Fetch] 抓取失败 - URL: {url}, 未找到结果")
            return "Error: No results found"

    except Exception as e:
        logger.error(f"[Tavily Fetch] 抓取异常 - URL: {url}, 错误: {str(e)}")
        return f"Error: {str(e)}"
