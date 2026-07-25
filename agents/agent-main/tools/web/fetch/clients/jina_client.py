"""
Jina AI 客户端（从 deer-flow 迁移）
"""
import logging
import os
import httpx

logger = logging.getLogger(__name__)
_api_key_warned = False


class JinaClient:
    """Jina AI Reader 客户端"""

    async def crawl(
        self,
        url: str,
        return_format: str = "html",
        timeout: int = 10,
        max_response_bytes: int = 2_000_000,
    ) -> str:
        """抓取网页内容"""
        global _api_key_warned

        headers = {
            "Content-Type": "application/json",
            "X-Return-Format": return_format,
            "X-Timeout": str(timeout),
        }
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
        elif not _api_key_warned:
            _api_key_warned = True
            logger.warning("Jina API key not set. Using free tier with rate limits.")

        data = {"url": url}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://r.jina.ai/",
                    headers=headers,
                    json=data,
                    timeout=timeout
                )

            # 对上游错误返回可诊断的上下文。
            if response.status_code == 401:
                error_msg = "Jina API 认证失败，请检查 JINA_API_KEY 环境变量"
                logger.error(f"{error_msg} - URL: {url}")
                return f"Error: {error_msg}"

            elif response.status_code == 403:
                error_msg = "Jina API 访问被拒绝，可能超出免费额度或 API Key 无效"
                logger.error(f"{error_msg} - URL: {url}")
                return f"Error: {error_msg}"

            elif response.status_code == 429:
                error_msg = "Jina API 请求过于频繁，请稍后重试"
                logger.error(f"{error_msg} - URL: {url}")
                return f"Error: {error_msg}"

            elif response.status_code == 404:
                error_msg = f"无法访问该网页，URL 可能不存在或已失效: {url}"
                logger.error(f"{error_msg}")
                return f"Error: {error_msg}"

            elif response.status_code != 200:
                error_msg = f"网页抓取失败（HTTP {response.status_code}），请尝试其他 URL"
                logger.error(f"{error_msg} - URL: {url} - Response: {response.text[:200]}")
                return f"Error: {error_msg}"

            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > max_response_bytes:
                        return "Error: 网页响应超过大小限制"
                except ValueError:
                    logger.warning("Jina 返回无效 Content-Length")
            if len(response.content) > max_response_bytes:
                return "Error: 网页响应超过大小限制"

            # 空响应不能作为有效抓取结果。
            if not response.text or not response.text.strip():
                error_msg = "网页内容为空，可能是动态加载页面或需要登录"
                logger.warning(f"{error_msg} - URL: {url}")
                return f"Error: {error_msg}"

            return response.text

        except httpx.TimeoutException:
            error_msg = f"网页抓取超时（{timeout}秒），请尝试增加超时时间或更换 URL"
            logger.error(f"{error_msg} - URL: {url}")
            return f"Error: {error_msg}"

        except httpx.ConnectError:
            error_msg = "无法连接到 Jina API 服务，请检查网络连接"
            logger.error(f"{error_msg} - URL: {url}")
            return f"Error: {error_msg}"

        except Exception as e:
            error_msg = f"网页抓取失败: {str(e)}"
            logger.exception(f"{error_msg} - URL: {url}")
            return f"Error: {error_msg}"
