"""
evermemOS API 客户端
负责通过 API 调用管理语义长期记忆
特点：
- 仅通过 API 调用，不涉及内部实现
- 语义搜索能力（100-500ms）
- 事实的增删改查
- 支持重试机制
- 异步 HTTP 请求
"""
import logging
from typing import Dict, Any, List, Optional

import httpx

from config import get_config

logger = logging.getLogger(__name__)


class EvermemAPIClient:
    """evermemOS API 客户端（仅 API 调用）"""

    def __init__(self):
        """初始化 evermemOS API 客户端"""
        self.config = get_config().memory.evermem
        self.client: Optional[httpx.AsyncClient] = None
        self._initialized = False

    async def connect(self) -> None:
        """
        初始化 HTTP 客户端

        Raises:
            Exception: 初始化失败时抛出
        """
        if self._initialized:
            return

        try:
            # 创建异步 HTTP 客户端
            self.client = httpx.AsyncClient(
                base_url=self.config.api_url,
                timeout=self.config.timeout,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json"
                }
            )

            # 测试连接（调用健康检查端点）
            try:
                response = await self.client.get("/health")
                if response.status_code == 200:
                    logger.info(f"evermemOS API 连接成功: {self.config.api_url}")
                else:
                    logger.warning(f"evermemOS API 健康检查返回非 200 状态码: {response.status_code}")
            except httpx.HTTPError:
                logger.warning("evermemOS API 健康检查失败，但客户端已初始化")

            self._initialized = True

        except Exception as e:
            logger.error(f"evermemOS API 客户端初始化失败: {e}")
            raise

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self.client:
            await self.client.aclose()
            self.client = None
            self._initialized = False
            logger.info("evermemOS API 客户端已关闭")

    async def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> Optional[httpx.Response]:
        """
        带重试机制的 HTTP 请求

        Args:
            method: HTTP 方法（GET/POST/PUT/DELETE）
            endpoint: API 端点
            **kwargs: 其他请求参数

        Returns:
            Optional[httpx.Response]: 响应对象，失败时返回 None
        """
        if not self._initialized:
            await self.connect()

        for attempt in range(self.config.max_retries):
            try:
                response = await self.client.request(method, endpoint, **kwargs)
                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP 请求失败 (尝试 {attempt + 1}/{self.config.max_retries}): {e}")
                if attempt == self.config.max_retries - 1:
                    logger.error(f"HTTP 请求最终失败: {e}")
                    return None

            except httpx.RequestError as e:
                logger.warning(f"请求错误 (尝试 {attempt + 1}/{self.config.max_retries}): {e}")
                if attempt == self.config.max_retries - 1:
                    logger.error(f"请求最终失败: {e}")
                    return None

        return None

    async def add_fact(
        self,
        user_id: str,
        memory_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        添加事实到 evermemOS

        Args:
            user_id: 用户 ID
            memory_id: 记忆 ID（对应 MongoDB 的 fact_id）
            content: 事实内容
            metadata: 可选的元数据（如 category, confidence 等）

        Returns:
            bool: 是否添加成功
        """
        try:
            payload = {
                "user_id": user_id,
                "memory_id": memory_id,
                "content": content,
            }
            if metadata:
                payload["metadata"] = metadata

            response = await self._request_with_retry(
                "POST",
                "/api/memories",
                json=payload
            )

            if response and response.status_code in [200, 201]:
                logger.debug(f"事实 {memory_id} 已添加到 evermemOS")
                return True
            else:
                logger.error(f"添加事实到 evermemOS 失败")
                return False

        except Exception as e:
            logger.error(f"添加事实到 evermemOS 异常: {e}")
            return False

    async def search_facts(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.7
    ) -> List[Dict[str, Any]]:
        """
        语义搜索事实

        Args:
            user_id: 用户 ID
            query: 搜索查询
            limit: 返回数量限制
            min_similarity: 最小相似度阈值

        Returns:
            List[Dict]: 搜索结果列表，每个结果包含 memory_id, content, similarity 等
        """
        try:
            payload = {
                "user_id": user_id,
                "query": query,
                "limit": limit,
                "min_similarity": min_similarity
            }

            response = await self._request_with_retry(
                "POST",
                "/api/memories/search",
                json=payload
            )

            if response and response.status_code == 200:
                results = response.json().get("results", [])
                logger.debug(f"语义搜索返回 {len(results)} 条结果")
                return results
            else:
                logger.error("语义搜索失败")
                return []

        except Exception as e:
            logger.error(f"语义搜索异常: {e}")
            return []

    async def get_facts(
        self,
        user_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取用户的所有事实

        Args:
            user_id: 用户 ID
            limit: 返回数量限制

        Returns:
            List[Dict]: 事实列表
        """
        try:
            response = await self._request_with_retry(
                "GET",
                f"/api/memories/user/{user_id}",
                params={"limit": limit}
            )

            if response and response.status_code == 200:
                facts = response.json().get("memories", [])
                logger.debug(f"获取用户 {user_id} 的 {len(facts)} 条事实")
                return facts
            else:
                logger.error(f"获取用户 {user_id} 的事实失败")
                return []

        except Exception as e:
            logger.error(f"获取事实异常: {e}")
            return []

    async def update_fact(
        self,
        memory_id: str,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        更新事实

        Args:
            memory_id: 记忆 ID
            content: 可选的新内容
            metadata: 可选的新元数据

        Returns:
            bool: 是否更新成功
        """
        try:
            payload = {}
            if content:
                payload["content"] = content
            if metadata:
                payload["metadata"] = metadata

            if not payload:
                logger.warning("更新事实时未提供任何更新内容")
                return False

            response = await self._request_with_retry(
                "PUT",
                f"/api/memories/{memory_id}",
                json=payload
            )

            if response and response.status_code == 200:
                logger.debug(f"事实 {memory_id} 已更新")
                return True
            else:
                logger.error(f"更新事实 {memory_id} 失败")
                return False

        except Exception as e:
            logger.error(f"更新事实异常: {e}")
            return False

    async def delete_fact(self, memory_id: str) -> bool:
        """
        删除事实

        Args:
            memory_id: 记忆 ID

        Returns:
            bool: 是否删除成功
        """
        try:
            response = await self._request_with_retry(
                "DELETE",
                f"/api/memories/{memory_id}"
            )

            if response and response.status_code in [200, 204]:
                logger.debug(f"事实 {memory_id} 已从 evermemOS 删除")
                return True
            else:
                logger.error(f"删除事实 {memory_id} 失败")
                return False

        except Exception as e:
            logger.error(f"删除事实异常: {e}")
            return False

    async def delete_user_facts(self, user_id: str) -> bool:
        """
        删除用户的所有事实

        Args:
            user_id: 用户 ID

        Returns:
            bool: 是否删除成功
        """
        try:
            response = await self._request_with_retry(
                "DELETE",
                f"/api/memories/user/{user_id}"
            )

            if response and response.status_code in [200, 204]:
                logger.info(f"用户 {user_id} 的所有事实已从 evermemOS 删除")
                return True
            else:
                logger.error(f"删除用户 {user_id} 的事实失败")
                return False

        except Exception as e:
            logger.error(f"删除用户事实异常: {e}")
            return False

    async def get_stats(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        获取统计信息

        Args:
            user_id: 可选的用户 ID，如果提供则返回该用户的统计信息

        Returns:
            Dict: 统计信息
        """
        try:
            endpoint = f"/api/stats/user/{user_id}" if user_id else "/api/stats"

            response = await self._request_with_retry("GET", endpoint)

            if response and response.status_code == 200:
                return response.json()
            else:
                logger.error("获取统计信息失败")
                return {"error": "Failed to get stats"}

        except Exception as e:
            logger.error(f"获取统计信息异常: {e}")
            return {"error": str(e)}
