"""
Redis 短期记忆管理器
负责管理会话级别的短期记忆，存储原始对话消息
特点：
- 毫秒级访问速度
- 自动过期（默认1小时）
- 消息数量限制
- 支持会话摘要生成
"""
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

import redis.asyncio as redis

from config import get_config
from utils.timezone import get_china_time

logger = logging.getLogger(__name__)


class RedisContextManager:
    """Redis 短期记忆管理器"""

    def __init__(self):
        """初始化 Redis 管理器"""
        self.client: Optional[redis.Redis] = None
        self.config = get_config().memory.redis

    async def connect(self) -> None:
        """
        连接到 Redis 服务器

        Raises:
            redis.ConnectionError: 连接失败时抛出
        """
        if self.client is None:
            try:
                self.client = await redis.Redis(
                    host=self.config.host,
                    port=self.config.port,
                    db=self.config.db,
                    password=self.config.password,
                    decode_responses=True,  # 自动解码为字符串
                    socket_connect_timeout=5,
                    socket_keepalive=True,
                )
                # 测试连接
                await self.client.ping()
                logger.info(f"Redis 连接成功: {self.config.host}:{self.config.port}")
            except Exception as e:
                logger.error(f"Redis 连接失败: {e}")
                raise

    async def close(self) -> None:
        """关闭 Redis 连接"""
        if self.client:
            await self.client.close()
            self.client = None
            logger.info("Redis 连接已关闭")

    def _get_session_key(self, session_id: str) -> str:
        """
        生成会话的 Redis 键

        Args:
            session_id: 会话 ID

        Returns:
            str: Redis 键，格式为 "minagent:session:{session_id}"
        """
        return f"{self.config.key_prefix}{session_id}"

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        添加消息到会话

        Args:
            session_id: 会话 ID
            role: 消息角色（user/assistant/system）
            content: 消息内容
            metadata: 可选的元数据（如时间戳、工具调用等）

        Returns:
            bool: 是否添加成功
        """
        if not self.client:
            await self.connect()

        try:
            key = self._get_session_key(session_id)

            # 构造消息对象
            message = {
                "role": role,
                "content": content,
                "timestamp": get_china_time().isoformat(),
            }
            if metadata:
                message["metadata"] = metadata

            # 添加到列表
            await self.client.rpush(key, json.dumps(message, ensure_ascii=False))

            # 设置过期时间
            await self.client.expire(key, self.config.ttl_seconds)

            # 限制消息数量（保留最新的 N 条）
            await self.client.ltrim(key, -self.config.max_messages, -1)

            logger.debug(f"消息已添加到会话 {session_id}: {role}")
            return True

        except Exception as e:
            logger.error(f"添加消息失败: {e}")
            return False

    async def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        获取会话的所有消息

        Args:
            session_id: 会话 ID
            limit: 可选的消息数量限制（获取最新的 N 条）

        Returns:
            List[Dict]: 消息列表，按时间顺序排列
        """
        if not self.client:
            await self.connect()

        try:
            key = self._get_session_key(session_id)

            # 获取消息列表
            if limit:
                messages_raw = await self.client.lrange(key, -limit, -1)
            else:
                messages_raw = await self.client.lrange(key, 0, -1)

            # 解析 JSON
            messages = [json.loads(msg) for msg in messages_raw]

            logger.debug(f"从会话 {session_id} 获取了 {len(messages)} 条消息")
            return messages

        except Exception as e:
            logger.error(f"获取消息失败: {e}")
            return []

    async def clear_session(self, session_id: str) -> bool:
        """
        清除会话的所有消息

        Args:
            session_id: 会话 ID

        Returns:
            bool: 是否清除成功
        """
        if not self.client:
            await self.connect()

        try:
            key = self._get_session_key(session_id)
            await self.client.delete(key)
            logger.info(f"会话 {session_id} 已清除")
            return True

        except Exception as e:
            logger.error(f"清除会话失败: {e}")
            return False

    async def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        """
        获取会话摘要信息

        Args:
            session_id: 会话 ID

        Returns:
            Dict: 会话摘要，包含消息数量、最早/最晚消息时间等
        """
        if not self.client:
            await self.connect()

        try:
            key = self._get_session_key(session_id)

            # 获取消息数量
            message_count = await self.client.llen(key)

            if message_count == 0:
                return {
                    "session_id": session_id,
                    "message_count": 0,
                    "exists": False
                }

            # 获取第一条和最后一条消息
            first_msg_raw = await self.client.lindex(key, 0)
            last_msg_raw = await self.client.lindex(key, -1)

            first_msg = json.loads(first_msg_raw) if first_msg_raw else {}
            last_msg = json.loads(last_msg_raw) if last_msg_raw else {}

            # 获取 TTL
            ttl = await self.client.ttl(key)

            return {
                "session_id": session_id,
                "message_count": message_count,
                "exists": True,
                "first_message_time": first_msg.get("timestamp"),
                "last_message_time": last_msg.get("timestamp"),
                "ttl_seconds": ttl if ttl > 0 else None
            }

        except Exception as e:
            logger.error(f"获取会话摘要失败: {e}")
            return {
                "session_id": session_id,
                "error": str(e)
            }

    async def extend_ttl(self, session_id: str) -> bool:
        """
        延长会话的过期时间

        Args:
            session_id: 会话 ID

        Returns:
            bool: 是否延长成功
        """
        if not self.client:
            await self.connect()

        try:
            key = self._get_session_key(session_id)
            await self.client.expire(key, self.config.ttl_seconds)
            logger.debug(f"会话 {session_id} 的 TTL 已延长")
            return True

        except Exception as e:
            logger.error(f"延长 TTL 失败: {e}")
            return False

    async def get_active_sessions(self) -> List[str]:
        """
        获取所有活跃的会话 ID

        Returns:
            List[str]: 会话 ID 列表
        """
        if not self.client:
            await self.connect()

        try:
            # 扫描所有匹配的键
            pattern = f"{self.config.key_prefix}*"
            sessions = []

            async for key in self.client.scan_iter(match=pattern, count=100):
                # 提取会话 ID
                session_id = key.replace(self.config.key_prefix, "")
                sessions.append(session_id)

            logger.debug(f"找到 {len(sessions)} 个活跃会话")
            return sessions

        except Exception as e:
            logger.error(f"获取活跃会话失败: {e}")
            return []

    async def get_stats(self) -> Dict[str, Any]:
        """
        获取 Redis 短期记忆的统计信息

        Returns:
            Dict: 统计信息，包含活跃会话数、总消息数等
        """
        try:
            sessions = await self.get_active_sessions()
            total_messages = 0

            for session_id in sessions:
                key = self._get_session_key(session_id)
                count = await self.client.llen(key)
                total_messages += count

            return {
                "active_sessions": len(sessions),
                "total_messages": total_messages,
                "avg_messages_per_session": total_messages / len(sessions) if sessions else 0
            }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"error": str(e)}


# 全局 Redis 管理器实例（单例模式）
_global_redis_manager: Optional[RedisContextManager] = None


def get_redis_manager() -> RedisContextManager:
    """
    获取全局 Redis 管理器实例（单例模式）

    Returns:
        RedisContextManager: Redis 管理器实例
    """
    global _global_redis_manager

    if _global_redis_manager is None:
        _global_redis_manager = RedisContextManager()
        logger.info("全局 Redis 管理器已创建")

    return _global_redis_manager
