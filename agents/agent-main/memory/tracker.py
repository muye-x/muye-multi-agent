"""
已处理消息跟踪器（Redis 版本）
用于防止重复提取已经处理过的对话
使用 Redis 持久化存储，支持分布式部署
"""
import hashlib
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ProcessedMessageTracker:
    """
    已处理消息跟踪器（Redis 版本）
    使用消息内容的哈希值来跟踪哪些消息已经被提取过记忆
    """

    def __init__(self, redis_client, ttl_hours: int = 24, key_prefix: str = "memory:processed:"):
        """
        初始化跟踪器

        Args:
            redis_client: Redis 客户端
            ttl_hours: 跟踪记录的过期时间（小时）
            key_prefix: Redis key 前缀
        """
        self.redis = redis_client
        self.ttl_hours = ttl_hours
        self.ttl_seconds = ttl_hours * 3600
        self.key_prefix = key_prefix

    def _hash_messages(self, messages: List[Any]) -> str:
        """
        计算消息列表的哈希值

        已弃用：使用整个对话历史哈希会导致重复提取。
        请使用 _hash_single_message() 进行消息级去重

        Args:
            messages: 消息列表

        Returns:
            str: MD5 哈希值
        """
        contents = []
        for msg in messages:
            if isinstance(msg, dict):
                content = msg.get("content", "")
            else:
                content = getattr(msg, "content", str(msg))
            contents.append(str(content))

        combined = "|".join(contents)
        return hashlib.md5(combined.encode()).hexdigest()

    def _hash_single_message(self, msg: Any) -> str:
        """
        计算单条消息的哈希值（消息级去重）

        Args:
            msg: 单条消息对象

        Returns:
            str: MD5 哈希值
        """
        # 提取消息内容和角色
        if isinstance(msg, dict):
            content = msg.get("content", "")
            role = msg.get("role", msg.get("type", ""))
        else:
            content = getattr(msg, "content", str(msg))
            role = getattr(msg, "type", getattr(msg, "role", ""))

        # 包含角色和内容，避免相同内容不同角色的冲突
        combined = f"{role}:{content}"
        return hashlib.md5(combined.encode()).hexdigest()

    def _get_new_messages(self, messages: List[Any], processed_hashes: set) -> List[Any]:
        """
        筛选出未处理过的新消息

        Args:
            messages: 消息列表
            processed_hashes: 已处理的消息哈希集合

        Returns:
            List[Any]: 未处理的新消息列表
        """
        new_messages = []
        for msg in messages:
            msg_hash = self._hash_single_message(msg)
            if msg_hash not in processed_hashes:
                new_messages.append(msg)
        return new_messages

    def _make_key(self, user_id: str, msg_hash: str) -> str:
        """
        生成 Redis key

        Args:
            user_id: 用户 ID
            msg_hash: 消息哈希

        Returns:
            str: Redis key
        """
        return f"{self.key_prefix}{user_id}:{msg_hash}"

    async def is_processed(self, user_id: str, messages: List[Any]) -> bool:
        """
        检查是否有新消息需要处理（消息级去重）

        Args:
            user_id: 用户 ID
            messages: 消息列表

        Returns:
            bool: True 表示所有消息已处理，False 表示有新消息需要处理
        """
        if not messages:
            return True

        try:
            # 使用新的 key 格式：memory:processed:msgs:{user_id}
            key = f"{self.key_prefix}msgs:{user_id}"

            # 获取已处理的消息哈希集合
            processed_hashes = await self.redis.smembers(key)
            processed_hashes = {h.decode() if isinstance(h, bytes) else h for h in processed_hashes}

            # 检查是否有新消息
            for msg in messages:
                msg_hash = self._hash_single_message(msg)
                if msg_hash not in processed_hashes:
                    logger.debug(f"发现新消息需要处理: user_id={user_id}, hash={msg_hash[:8]}")
                    return False  # 有新消息，需要处理

            logger.debug(f"所有消息已处理过，跳过: user_id={user_id}")
            return True  # 所有消息都处理过

        except Exception as e:
            logger.error(f"检查处理记录失败: {e}")
            return False

    async def mark_processed(self, user_id: str, messages: List[Any], metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        标记消息为已处理（消息级去重）

        Args:
            user_id: 用户 ID
            messages: 消息列表
            metadata: 额外的元数据

        Returns:
            bool: 是否标记成功
        """
        if not messages:
            return False

        try:
            # 使用新的 key 格式：memory:processed:msgs:{user_id}
            key = f"{self.key_prefix}msgs:{user_id}"

            # 计算所有消息的哈希
            msg_hashes = [self._hash_single_message(msg) for msg in messages]

            # 批量添加到 Redis Set
            if msg_hashes:
                await self.redis.sadd(key, *msg_hashes)
                # 设置过期时间
                await self.redis.expire(key, self.ttl_seconds)

            logger.debug(f"标记 {len(msg_hashes)} 条消息为已处理: user_id={user_id}")
            return True

        except Exception as e:
            logger.error(f"标记处理记录失败: {e}")
            return False

    async def get_processed_info(self, user_id: str, messages: List[Any]) -> Optional[Dict[str, Any]]:
        """
        获取处理记录的详细信息（消息级去重版本）

        Args:
            user_id: 用户 ID
            messages: 消息列表

        Returns:
            Optional[Dict]: 处理记录信息，包含已处理和未处理的消息统计
        """
        if not messages:
            return None

        try:
            key = f"{self.key_prefix}msgs:{user_id}"

            # 获取已处理的消息哈希集合
            processed_hashes = await self.redis.smembers(key)
            processed_hashes = {h.decode() if isinstance(h, bytes) else h for h in processed_hashes}

            # 统计已处理和未处理的消息
            processed_count = 0
            unprocessed_count = 0

            for msg in messages:
                msg_hash = self._hash_single_message(msg)
                if msg_hash in processed_hashes:
                    processed_count += 1
                else:
                    unprocessed_count += 1

            return {
                "user_id": user_id,
                "total_messages": len(messages),
                "processed_count": processed_count,
                "unprocessed_count": unprocessed_count,
                "all_processed": unprocessed_count == 0
            }

        except Exception as e:
            logger.error(f"获取处理记录失败: {e}")
            return None

    async def get_unprocessed_messages(self, user_id: str, messages: List[Any]) -> List[Any]:
        """
        获取未处理过的新消息（供上下文管理器使用）

        Args:
            user_id: 用户 ID
            messages: 消息列表

        Returns:
            List[Any]: 未处理的新消息列表
        """
        if not messages:
            return []

        try:
            key = f"{self.key_prefix}msgs:{user_id}"

            # 获取已处理的消息哈希集合
            processed_hashes = await self.redis.smembers(key)
            processed_hashes = {h.decode() if isinstance(h, bytes) else h for h in processed_hashes}

            # 筛选未处理的消息
            return self._get_new_messages(messages, processed_hashes)

        except Exception as e:
            logger.error(f"获取未处理消息失败: {e}")
            # 出错时返回全部消息（容错）
            return messages

    async def clear_user(self, user_id: str) -> int:
        """
        清除用户的所有跟踪记录（消息级去重版本）

        Args:
            user_id: 用户 ID

        Returns:
            int: 删除的记录数
        """
        try:
            # 新格式：直接删除 Set
            key = f"{self.key_prefix}msgs:{user_id}"
            deleted = await self.redis.delete(key)

            # 兼容旧格式：清理旧的 hash key
            pattern = f"{self.key_prefix}{user_id}:*"
            old_keys = []

            cursor = 0
            while True:
                cursor, batch = await self.redis.scan(cursor, match=pattern, count=100)
                old_keys.extend(batch)
                if cursor == 0:
                    break

            if old_keys:
                old_deleted = await self.redis.delete(*old_keys)
                deleted += old_deleted
                logger.info(f"清除了用户的旧格式记录: user_id={user_id}, count={old_deleted}")

            logger.info(f"清除了用户的跟踪记录: user_id={user_id}, total={deleted}")
            return deleted

        except Exception as e:
            logger.error(f"清除用户跟踪记录失败: {e}")
            return 0

    async def get_stats(self) -> Dict[str, Any]:
        """
        获取跟踪器统计信息（消息级去重版本）

        Returns:
            Dict: 统计信息
        """
        try:
            # 统计新格式的记录
            pattern_new = f"{self.key_prefix}msgs:*"
            new_format_users = set()
            total_messages = 0

            cursor = 0
            while True:
                cursor, batch = await self.redis.scan(cursor, match=pattern_new, count=100)

                for key in batch:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    parts = key_str.split(":")
                    if len(parts) >= 3:
                        user_id = parts[2]
                        new_format_users.add(user_id)
                        # 统计该用户的消息数
                        count = await self.redis.scard(key)
                        total_messages += count

                if cursor == 0:
                    break

            # 统计旧格式的记录（兼容）
            pattern_old = f"{self.key_prefix}*"
            old_format_records = 0

            cursor = 0
            while True:
                cursor, batch = await self.redis.scan(cursor, match=pattern_old, count=100)
                for key in batch:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    # 排除新格式的 key
                    if ":msgs:" not in key_str:
                        old_format_records += 1

                if cursor == 0:
                    break

            return {
                "new_format_users": len(new_format_users),
                "total_processed_messages": total_messages,
                "old_format_records": old_format_records,
                "ttl_hours": self.ttl_hours
            }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"error": str(e)}


# 全局跟踪器实例（单例模式）
_global_tracker: Optional[ProcessedMessageTracker] = None


def get_processed_tracker(redis_client=None, ttl_hours: int = 24, key_prefix: str = "memory:processed:") -> ProcessedMessageTracker:
    """
    获取全局已处理消息跟踪器实例（单例模式）

    Args:
        redis_client: Redis 客户端（首次调用时必须提供）
        ttl_hours: 跟踪记录的过期时间（小时）
        key_prefix: Redis key 前缀

    Returns:
        ProcessedMessageTracker: 跟踪器实例
    """
    global _global_tracker

    if _global_tracker is None:
        if redis_client is None:
            raise ValueError("首次调用 get_processed_tracker 必须提供 redis_client")

        _global_tracker = ProcessedMessageTracker(redis_client, ttl_hours, key_prefix)
        logger.info(f"全局消息跟踪器已创建（Redis），TTL={ttl_hours}小时，前缀={key_prefix}")

    return _global_tracker
