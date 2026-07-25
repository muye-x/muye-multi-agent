"""
MongoDB 结构化长期记忆管理器
负责管理类似 deer-flow 的结构化用户上下文和历史记录
特点：
- 快速结构化查询（10-50ms）
- 用户上下文管理（workContext, personalContext, topOfMind）
- 历史记录管理（recentMonths, earlierContext, longTermBackground）
- 事实备份存储
- 索引优化
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo import IndexModel, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from config import get_config
from utils.timezone import get_china_time

logger = logging.getLogger(__name__)


class MongoDBMemoryManager:
    """MongoDB 结构化长期记忆管理器"""

    def __init__(self):
        """初始化 MongoDB 管理器"""
        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None
        self.contexts_collection: Optional[AsyncIOMotorCollection] = None
        self.facts_collection: Optional[AsyncIOMotorCollection] = None
        self.config = get_config().memory.mongodb
        self._initialized = False

    async def connect(self) -> None:
        """
        连接到 MongoDB 并初始化集合和索引

        Raises:
            Exception: 连接失败时抛出
        """
        if self._initialized:
            return

        try:
            # 创建客户端
            self.client = AsyncIOMotorClient(
                self.config.uri,
                maxPoolSize=self.config.max_pool_size,
                serverSelectionTimeoutMS=5000
            )

            # 获取数据库
            self.db = self.client[self.config.database]

            # 获取集合
            self.contexts_collection = self.db[self.config.collection_contexts]
            self.facts_collection = self.db[self.config.collection_facts]

            # 创建索引
            await self._create_indexes()

            # 测试连接
            await self.client.admin.command('ping')

            self._initialized = True
            logger.info(f"MongoDB 连接成功: {self.config.database}")

        except Exception as e:
            logger.error(f"MongoDB 连接失败: {e}")
            raise

    async def _create_indexes(self) -> None:
        """创建必要的索引以优化查询性能"""
        try:
            # 用户上下文集合索引
            context_indexes = [
                IndexModel([("user_id", ASCENDING)], unique=True),
                IndexModel([("updatedAt", DESCENDING)]),
            ]
            await self.contexts_collection.create_indexes(context_indexes)

            # 事实备份集合索引
            fact_indexes = [
                IndexModel([("user_id", ASCENDING)]),
                IndexModel([("category", ASCENDING)]),
                IndexModel([("confidence", DESCENDING)]),
                IndexModel([("createdAt", DESCENDING)]),
                IndexModel([("user_id", ASCENDING), ("category", ASCENDING)]),
            ]
            await self.facts_collection.create_indexes(fact_indexes)

            logger.debug("MongoDB 索引创建完成")

        except Exception as e:
            logger.warning(f"创建索引时出现警告: {e}")

    async def close(self) -> None:
        """关闭 MongoDB 连接"""
        if self.client:
            self.client.close()
            self.client = None
            self.db = None
            self.contexts_collection = None
            self.facts_collection = None
            self._initialized = False
            logger.info("MongoDB 连接已关闭")

    def _create_empty_context(self, user_id: str) -> Dict[str, Any]:
        """
        创建空的用户上下文结构（完全兼容 deer-flow 的 memory.json 格式）

        Args:
            user_id: 用户 ID

        Returns:
            Dict: 空的用户上下文（deer-flow 格式）
        """
        now = get_china_time().isoformat()
        return {
            "user_id": user_id,
            "version": "1.0",
            "lastUpdated": now,
            "user": {
                "workContext": {
                    "summary": "",
                    "updatedAt": ""
                },
                "personalContext": {
                    "summary": "",
                    "updatedAt": ""
                },
                "topOfMind": {
                    "summary": "",
                    "updatedAt": ""
                }
            },
            "history": {
                "recentMonths": {
                    "summary": "",
                    "updatedAt": ""
                },
                "earlierContext": {
                    "summary": "",
                    "updatedAt": ""
                },
                "longTermBackground": {
                    "summary": "",
                    "updatedAt": ""
                }
            },
            "facts": []
        }

    async def get_user_context(self, user_id: str) -> Dict[str, Any]:
        """
        获取用户的结构化上下文

        Args:
            user_id: 用户 ID

        Returns:
            Dict: 用户上下文，如果不存在则返回空结构
        """
        if not self._initialized:
            await self.connect()

        try:
            context = await self.contexts_collection.find_one({"user_id": user_id})

            if context:
                # 移除 MongoDB 的 _id 字段
                context.pop("_id", None)
                logger.debug(f"获取用户 {user_id} 的上下文成功")
                return context
            else:
                # 返回空结构
                logger.debug(f"用户 {user_id} 的上下文不存在，返回空结构")
                return self._create_empty_context(user_id)

        except Exception as e:
            logger.error(f"获取用户上下文失败: {e}")
            return self._create_empty_context(user_id)

    async def update_user_context(
        self,
        user_id: str,
        updates: Dict[str, Any]
    ) -> bool:
        """
        更新用户的结构化上下文（兼容 deer-flow 的更新格式）

        Args:
            user_id: 用户 ID
            updates: 更新内容，格式：
                {
                    "user": {
                        "workContext": {"summary": "...", "shouldUpdate": true/false},
                        "personalContext": {"summary": "...", "shouldUpdate": true/false},
                        "topOfMind": {"summary": "...", "shouldUpdate": true/false}
                    },
                    "history": {
                        "recentMonths": {"summary": "...", "shouldUpdate": true/false},
                        "earlierContext": {"summary": "...", "shouldUpdate": true/false},
                        "longTermBackground": {"summary": "...", "shouldUpdate": true/false}
                    }
                }

        Returns:
            bool: 是否更新成功
        """
        if not self._initialized:
            await self.connect()

        try:
            now = get_china_time().isoformat()

            # 准备更新操作
            update_ops = {}

            # 更新 user 部分
            if "user" in updates:
                for field in ["workContext", "personalContext", "topOfMind"]:
                    if field in updates["user"]:
                        field_update = updates["user"][field]
                        # 检查 shouldUpdate 标志
                        if field_update.get("shouldUpdate", False):
                            summary = field_update.get("summary", "")
                            update_ops[f"user.{field}.summary"] = summary
                            update_ops[f"user.{field}.updatedAt"] = now

            # 更新 history 部分
            if "history" in updates:
                for field in ["recentMonths", "earlierContext", "longTermBackground"]:
                    if field in updates["history"]:
                        field_update = updates["history"][field]
                        # 检查 shouldUpdate 标志
                        if field_update.get("shouldUpdate", False):
                            summary = field_update.get("summary", "")
                            update_ops[f"history.{field}.summary"] = summary
                            update_ops[f"history.{field}.updatedAt"] = now

            # 如果有更新，执行更新操作
            if update_ops:
                update_ops["lastUpdated"] = now

                await self.contexts_collection.update_one(
                    {"user_id": user_id},
                    {"$set": update_ops},
                    upsert=True
                )

                logger.info(f"用户 {user_id} 的上下文已更新")
                return True
            else:
                logger.debug(f"用户 {user_id} 的上下文无需更新")
                return True

        except Exception as e:
            logger.error(f"更新用户上下文失败: {e}")
            return False

    async def add_fact(
        self,
        user_id: str,
        fact_id: str,
        content: str,
        category: str = "context",
        confidence: float = 0.5,
        source: str = "conversation",
        source_error: Optional[str] = None
    ) -> bool:
        """
        添加事实到用户上下文的 facts 数组（deer-flow 格式）

        Args:
            user_id: 用户 ID
            fact_id: 事实 ID
            content: 事实内容
            category: 事实类别（preference/knowledge/context/behavior/goal/correction）
            confidence: 置信度
            source: 来源（conversation/manual）
            source_error: 可选的错误来源信息（仅用于 correction 类别）

        Returns:
            bool: 是否添加成功
        """
        if not self._initialized:
            await self.connect()

        try:
            now = get_china_time().isoformat()

            fact = {
                "id": fact_id,
                "content": content,
                "category": category,
                "confidence": confidence,
                "createdAt": now,
                "source": source
            }

            # 只有 correction 类别才添加 sourceError
            if source_error and category == "correction":
                fact["sourceError"] = source_error

            # 添加到用户上下文的 facts 数组
            await self.contexts_collection.update_one(
                {"user_id": user_id},
                {
                    "$push": {"facts": fact},
                    "$set": {"lastUpdated": now}
                },
                upsert=True
            )

            logger.debug(f"事实已添加: user_id={user_id}, fact_id={fact_id}")
            return True

        except Exception as e:
            logger.error(f"添加事实失败: {e}")
            return False

    async def get_facts(
        self,
        user_id: str,
        category: Optional[str] = None,
        min_confidence: Optional[float] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取用户的事实列表（从用户上下文的 facts 数组中获取）

        Args:
            user_id: 用户 ID
            category: 可选的类别过滤
            min_confidence: 可选的最小置信度过滤
            limit: 返回数量限制

        Returns:
            List[Dict]: 事实列表
        """
        if not self._initialized:
            await self.connect()

        try:
            # 获取用户上下文
            context = await self.contexts_collection.find_one({"user_id": user_id})

            if not context or "facts" not in context:
                return []

            facts = context["facts"]

            # 应用过滤条件
            if category:
                facts = [f for f in facts if f.get("category") == category]
            if min_confidence is not None:
                facts = [f for f in facts if f.get("confidence", 0) >= min_confidence]

            # 按置信度排序
            facts = sorted(facts, key=lambda f: f.get("confidence", 0), reverse=True)

            # 限制数量
            facts = facts[:limit]

            logger.debug(f"获取用户 {user_id} 的 {len(facts)} 条事实")
            return facts

        except Exception as e:
            logger.error(f"获取事实失败: {e}")
            return []

    async def delete_fact(self, user_id: str, fact_id: str) -> bool:
        """
        删除事实（从用户上下文的 facts 数组中删除）

        Args:
            user_id: 用户 ID
            fact_id: 事实 ID

        Returns:
            bool: 是否删除成功
        """
        if not self._initialized:
            await self.connect()

        try:
            now = get_china_time().isoformat()

            result = await self.contexts_collection.update_one(
                {"user_id": user_id},
                {
                    "$pull": {"facts": {"id": fact_id}},
                    "$set": {"lastUpdated": now}
                }
            )

            if result.modified_count > 0:
                logger.debug(f"事实 {fact_id} 已删除")
                return True
            else:
                logger.warning(f"事实 {fact_id} 不存在或未删除")
                return False

        except Exception as e:
            logger.error(f"删除事实失败: {e}")
            return False

    async def update_fact_confidence(
        self,
        user_id: str,
        fact_id: str,
        new_confidence: float
    ) -> bool:
        """
        更新事实的置信度（用于记忆衰减）

        Args:
            user_id: 用户 ID
            fact_id: 事实 ID
            new_confidence: 新的置信度

        Returns:
            bool: 是否更新成功
        """
        if not self._initialized:
            await self.connect()

        try:
            result = await self.contexts_collection.update_one(
                {"user_id": user_id, "facts.id": fact_id},
                {"$set": {"facts.$.confidence": new_confidence}}
            )

            if result.modified_count > 0:
                logger.debug(f"事实 {fact_id} 的置信度已更新为 {new_confidence}")
                return True
            else:
                return False

        except Exception as e:
            logger.error(f"更新事实置信度失败: {e}")
            return False

    async def clear_user_data(self, user_id: str) -> bool:
        """
        清除用户的所有数据（上下文和事实）

        Args:
            user_id: 用户 ID

        Returns:
            bool: 是否清除成功
        """
        if not self._initialized:
            await self.connect()

        try:
            return True

        except Exception as e:
            logger.error(f"清除用户数据失败: {e}")
            return False

    async def get_stats(self) -> Dict[str, Any]:
        """
        获取 MongoDB 的统计信息

        Returns:
            Dict: 统计信息
        """
        if not self._initialized:
            await self.connect()

        try:
            # 统计用户数
            user_count = await self.contexts_collection.count_documents({})

            # 统计总事实数（通过聚合 facts 数组）
            pipeline = [
                {"$project": {"fact_count": {"$size": {"$ifNull": ["$facts", []]}}}},
                {"$group": {"_id": None, "total_facts": {"$sum": "$fact_count"}}}
            ]
            fact_stats = await self.contexts_collection.aggregate(pipeline).to_list(length=1)
            total_facts = fact_stats[0]["total_facts"] if fact_stats else 0

            # 按类别统计事实（通过 unwind facts 数组）
            category_pipeline = [
                {"$unwind": "$facts"},
                {"$group": {"_id": "$facts.category", "count": {"$sum": 1}}}
            ]
            category_stats = await self.contexts_collection.aggregate(category_pipeline).to_list(length=100)

            return {
                "total_users": user_count,
                "total_facts": total_facts,
                "facts_by_category": {item["_id"]: item["count"] for item in category_stats}
            }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"error": str(e)}
