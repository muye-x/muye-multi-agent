"""
三层记忆上下文管理器
协调 Redis（短期）、MongoDB（结构化长期）、evermemOS（语义长期）三层记忆
这是整个记忆系统的核心编排器
"""
import asyncio
import logging
import uuid
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from .storage.redis import RedisContextManager
from .storage.mongodb import MongoDBMemoryManager
from .storage.evermem import EvermemAPIClient
from .queue import get_memory_queue, MemoryUpdateTask
from .extractor import MemoryExtractor
from .signal import SignalDetector
from .prompts import format_memory_for_injection
from config import get_config
from utils.timezone import get_china_time
from utils.profiler import RequestProfiler

logger = logging.getLogger(__name__)


class ThreeLayerContextManager:
    """
    三层记忆上下文管理器

    架构：
    - 第一层（Redis）：短期会话记忆，毫秒级访问
    - 第二层（MongoDB）：结构化长期记忆，10-50ms 访问
    - 第三层（evermemOS）：语义长期记忆，100-500ms 访问
    """

    def __init__(self):
        """初始化三层记忆管理器"""
        self.redis_manager = RedisContextManager()
        self.mongodb_manager = MongoDBMemoryManager()
        self.evermem_client = EvermemAPIClient()
        self.memory_extractor = MemoryExtractor()
        self.signal_detector = SignalDetector()
        self.config = get_config().memory
        self._initialized = False
        self.tracker = None  # 延迟初始化

    async def initialize(self) -> None:
        """
        初始化所有层的连接

        Raises:
            Exception: 初始化失败时抛出
        """
        if self._initialized:
            return

        try:
            # 根据配置初始化各层
            init_tasks = []

            if self.config.enable_redis:
                init_tasks.append(self.redis_manager.connect())
                logger.info("Redis 层已启用")

            if self.config.enable_mongodb:
                init_tasks.append(self.mongodb_manager.connect())
                logger.info("MongoDB 层已启用")

            if self.config.enable_evermem:
                init_tasks.append(self.evermem_client.connect())
                logger.info("evermemOS 层已启用")

            # 并行初始化启用的层
            if init_tasks:
                await asyncio.gather(*init_tasks)

            # 初始化 Redis 跟踪器（用于记忆提取去重）
            if self.config.enable_redis:
                from .tracker import get_processed_tracker
                self.tracker = get_processed_tracker(
                    redis_client=self.redis_manager.client,
                    ttl_hours=self.config.redis.tracker_ttl_hours,
                    key_prefix=self.config.redis.tracker_key_prefix
                )
                logger.info(f"Memory 提取跟踪器已初始化（Redis），TTL={self.config.redis.tracker_ttl_hours}小时")

            # 设置队列处理器
            queue = get_memory_queue()
            queue.set_processor(self._process_queue_task)

            self._initialized = True
            logger.info("三层记忆系统初始化完成")

        except Exception as e:
            logger.error(f"三层记忆系统初始化失败: {e}")
            raise

    async def close(self) -> None:
        """关闭所有层的连接"""
        # 在关闭前，先刷新队列中的待处理任务
        queue = get_memory_queue()
        pending_count = queue.get_pending_count()
        if pending_count > 0:
            logger.info(f"关闭前刷新队列，处理 {pending_count} 个待处理任务")
            await queue.flush()

        # 关闭启用的层
        close_tasks = []
        if self.config.enable_redis:
            close_tasks.append(self.redis_manager.close())
        if self.config.enable_mongodb:
            close_tasks.append(self.mongodb_manager.close())
        if self.config.enable_evermem:
            close_tasks.append(self.evermem_client.close())

        if close_tasks:
            await asyncio.gather(*close_tasks)

        self._initialized = False
        logger.info("三层记忆系统已关闭")

    async def load_context(
        self,
        user_id: str,
        session_id: str,
        timeout: float = 3.0
    ) -> Dict[str, Any]:
        """
        加载用户的完整上下文（从三层并行加载）

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            timeout: 超时时间（秒），默认 5 秒

        Returns:
            Dict: 完整的上下文信息
        """
        if not self._initialized:
            await self.initialize()

        try:
            # 根据配置并行从启用的层加载数据
            load_tasks = []
            task_names = []

            if self.config.enable_redis:
                load_tasks.append(self.redis_manager.get_messages(session_id))
                task_names.append('redis')

            if self.config.enable_mongodb:
                load_tasks.append(self.mongodb_manager.get_user_context(user_id))
                task_names.append('mongodb')

            if self.config.enable_evermem:
                load_tasks.append(self.evermem_client.get_facts(user_id, limit=50))
                task_names.append('evermem')

            # 并行加载（带超时保护）
            profiler = RequestProfiler.get_current()
            try:
                if profiler:
                    async with profiler.async_timing("memory_parallel_load"):
                        results = await asyncio.wait_for(
                            asyncio.gather(*load_tasks, return_exceptions=True) if load_tasks else asyncio.sleep(0),
                            timeout=timeout
                        ) if load_tasks else []
                else:
                    results = await asyncio.wait_for(
                        asyncio.gather(*load_tasks, return_exceptions=True) if load_tasks else asyncio.sleep(0),
                        timeout=timeout
                    ) if load_tasks else []
            except asyncio.TimeoutError:
                logger.error(f"加载上下文超时 (>{timeout}s): user_id={user_id}, 尝试加载的层: {task_names}")
                # 超时时返回空上下文
                return {
                    "user_id": user_id,
                    "session_id": session_id,
                    "short_term": {"messages": [], "message_count": 0},
                    "structured_long_term": {},
                    "semantic_long_term": {"facts": [], "fact_count": 0},
                    "error": f"加载超时 (>{timeout}s)"
                }

            # 解析结果
            redis_messages = []
            mongodb_context = {}
            evermem_facts = []

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(f"加载 {task_names[i]} 数据失败: {result}")
                else:
                    if task_names[i] == 'redis':
                        redis_messages = result
                    elif task_names[i] == 'mongodb':
                        mongodb_context = result
                    elif task_names[i] == 'evermem':
                        evermem_facts = result

            # 组合上下文
            context = {
                "user_id": user_id,
                "session_id": session_id,
                "short_term": {
                    "messages": redis_messages,
                    "message_count": len(redis_messages)
                },
                "structured_long_term": mongodb_context,
                "semantic_long_term": {
                    "facts": evermem_facts,
                    "fact_count": len(evermem_facts)
                }
            }

            logger.debug(f"上下文加载完成: user_id={user_id}, "
                        f"短期消息={len(redis_messages)}, "
                        f"语义事实={len(evermem_facts)}")

            return context

        except Exception as e:
            logger.error(f"加载上下文失败: {e}", exc_info=True)
            return {
                "user_id": user_id,
                "session_id": session_id,
                "error": str(e)
            }

    async def save_interaction(
        self,
        user_id: str,
        session_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        保存一次交互
        - 立即保存到 Redis（短期）
        - 将长期记忆更新任务加入队列（防抖处理）

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            user_message: 用户消息
            assistant_message: 助手消息
            metadata: 可选的元数据

        Returns:
            bool: 是否保存成功
        """
        if not self._initialized:
            await self.initialize()

        try:
            # 1. 立即保存到 Redis（如果启用）
            if self.config.enable_redis:
                await self.redis_manager.add_message(
                    session_id=session_id,
                    role="user",
                    content=user_message,
                    metadata=metadata
                )

                await self.redis_manager.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=assistant_message,
                    metadata=metadata
                )

            # 2. 获取当前会话的所有消息
            messages = await self.redis_manager.get_messages(session_id) if self.config.enable_redis else [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message}
            ]

            # 3. 检测信号
            signals = self.signal_detector.analyze_signals(messages)

            # 4. 将长期记忆更新任务加入队列（防抖）
            queue = get_memory_queue()
            await queue.add(
                user_id=user_id,
                session_id=session_id,
                messages=messages,
                correction_detected=signals["has_correction"],
                reinforcement_detected=signals["has_reinforcement"]
            )

            logger.debug(f"交互已保存: user_id={user_id}, session_id={session_id}")
            return True

        except Exception as e:
            logger.error(f"保存交互失败: {e}", exc_info=True)
            return False

    async def _process_queue_task(self, task: MemoryUpdateTask) -> None:
        """
        处理队列中的记忆更新任务
        这是队列的回调函数

        Args:
            task: 记忆更新任务
        """
        try:
            logger.info(f"开始处理记忆更新: user_id={task.user_id}")
            # 0. 检查是否已经处理过这些消息（使用 Redis 跟踪器）
            if self.tracker:
                if await self.tracker.is_processed(task.user_id, task.messages):
                    logger.info(f"消息已处理过，跳过重复提取: user_id={task.user_id}")
                    return

            # 0. 筛选未处理的新消息（使用 Redis 跟踪器）
            messages_to_process = task.messages  # 默认处理全部消息

            if self.tracker:
                # 使用 tracker 的方法获取未处理的消息
                messages_to_process = await self.tracker.get_unprocessed_messages(
                    task.user_id,
                    task.messages
                )

                if not messages_to_process:
                    logger.info(f"所有消息已处理过，跳过重复提取: user_id={task.user_id}")
                    return

                # 记录统计信息
                logger.info(
                    f"筛选出 {len(messages_to_process)} 条新消息 "
                    f"（总消息数: {len(task.messages)}）"
                )

            # 1. 获取当前的 MongoDB 上下文
            current_context = await self.mongodb_manager.get_user_context(task.user_id)

            # 2. 使用 LLM 提取记忆更新（只传入新消息）
            update_data = await self.memory_extractor.extract_memories(
                messages=messages_to_process,
                current_memory=current_context,
                correction_detected=task.correction_detected,
                reinforcement_detected=task.reinforcement_detected
            )

            if not update_data:
                logger.warning(f"记忆提取失败，跳过更新: user_id={task.user_id}")
                return

            # 3. 更新 MongoDB 结构化上下文
            await self._update_structured_context(task.user_id, update_data)

            # 4. 更新 evermemOS 语义事实
            await self._update_semantic_facts(task.user_id, update_data, task.session_id)

            # 5. 标记消息为已处理（标记全部消息，包括之前已处理的）
            if self.tracker:
                await self.tracker.mark_processed(task.user_id, task.messages)

            logger.info(f"记忆更新完成: user_id={task.user_id}")

        except Exception as e:
            logger.error(f"处理记忆更新任务失败: user_id={task.user_id}, error={e}", exc_info=True)

    async def _update_structured_context(
        self,
        user_id: str,
        update_data: Dict[str, Any]
    ) -> None:
        """
        更新 MongoDB 中的结构化上下文（采用 deer-flow 的更新逻辑）

        Args:
            user_id: 用户 ID
            update_data: LLM 提取的更新数据（deer-flow 格式）
        """
        try:
            # 直接传递 update_data 给 mongodb_manager
            # mongodb_manager 会处理 shouldUpdate 标志
            await self.mongodb_manager.update_user_context(user_id, update_data)

            # 处理 facts 的添加和删除
            # 删除旧事实
            facts_to_remove = update_data.get("factsToRemove", [])
            for fact_id in facts_to_remove:
                await self.mongodb_manager.delete_fact(user_id, fact_id)
                logger.debug(f"已删除事实: {fact_id}")

            # 添加新事实
            new_facts = update_data.get("newFacts", [])
            for fact in new_facts:
                fact_id = f"fact_{uuid.uuid4().hex[:8]}"
                await self.mongodb_manager.add_fact(
                    user_id=user_id,
                    fact_id=fact_id,
                    content=fact.get("content", ""),
                    category=fact.get("category", "context"),
                    confidence=fact.get("confidence", 0.5),
                    source="conversation",
                    source_error=fact.get("sourceError")
                )
                logger.debug(f"已添加事实: {fact_id}, category={fact.get('category')}")

            logger.info(f"MongoDB 上下文已更新: user_id={user_id}, "
                       f"新增事实={len(new_facts)}, 删除事实={len(facts_to_remove)}")

        except Exception as e:
            logger.error(f"更新结构化上下文失败: {e}", exc_info=True)

    async def _update_semantic_facts(
        self,
        user_id: str,
        update_data: Dict[str, Any],
        source_session: str
    ) -> None:
        """
        更新 evermemOS 中的语义事实（可选）
        注意：facts 已经在 _update_structured_context 中添加到 MongoDB

        Args:
            user_id: 用户 ID
            update_data: LLM 提取的更新数据
            source_session: 来源会话 ID
        """
        # 检查是否启用 evermemOS
        if not self.config.evermem.api_key:
            logger.debug("evermemOS 未配置，跳过语义事实同步")
            return

        try:
            # 只处理 evermemOS 的同步（如果启用）
            new_facts = update_data.get("newFacts", [])

            for fact_data in new_facts:
                confidence = fact_data.get("confidence", 0.5)

                # 检查置信度阈值
                if confidence < self.config.fact_confidence_threshold:
                    logger.debug(f"事实置信度过低，跳过 evermemOS 同步: {confidence}")
                    continue

                content = fact_data.get("content", "").strip()
                if not content:
                    continue

                # 去重检查（使用语义搜索）
                if await self._is_duplicate_fact(user_id, content):
                    logger.debug(f"事实重复，跳过 evermemOS 同步: {content[:50]}")
                    continue

                # 生成事实 ID
                fact_id = f"fact_{uuid.uuid4().hex[:8]}"

                # 准备元数据
                metadata = {
                    "category": fact_data.get("category", "context"),
                    "confidence": confidence,
                    "source": source_session
                }

                # 添加到 evermemOS（可选）
                success = await self.evermem_client.add_fact(
                    user_id=user_id,
                    memory_id=fact_id,
                    content=content,
                    metadata=metadata
                )

                if success:
                    logger.debug(f"事实已同步到 evermemOS: {fact_id}")

        except Exception as e:
            logger.error(f"更新语义事实失败: {e}", exc_info=True)

    async def _is_duplicate_fact(self, user_id: str, content: str) -> bool:
        """
        使用语义搜索检查事实是否重复

        Args:
            user_id: 用户 ID
            content: 事实内容

        Returns:
            bool: 是否重复
        """
        # 检查是否启用 evermemOS
        if not self.config.evermem.api_key:
            return False

        try:
            # 使用高相似度阈值进行搜索
            results = await self.evermem_client.search_facts(
                user_id=user_id,
                query=content,
                limit=1,
                min_similarity=0.9
            )

            return len(results) > 0

        except Exception as e:
            logger.warning(f"去重检查失败: {e}")
            return False

    async def decay_old_memories(self, user_id: str) -> Dict[str, Any]:
        """
        对旧记忆进行衰减处理
        降低长时间未使用的事实的置信度

        Args:
            user_id: 用户 ID

        Returns:
            Dict: 衰减统计信息
        """
        if not self.config.decay_enabled:
            return {"enabled": False}

        try:
            # 获取所有事实
            facts = await self.mongodb_manager.get_facts(user_id)

            decayed_count = 0
            removed_count = 0
            threshold_date = get_china_time() - timedelta(days=self.config.decay_days_threshold)

            for fact in facts:
                created_at_str = fact.get("createdAt", "")
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                except:
                    continue

                # 检查是否超过阈值
                if created_at < threshold_date:
                    current_confidence = fact.get("confidence", 0.5)
                    new_confidence = current_confidence * (1 - self.config.decay_rate)

                    # 如果置信度过低，删除事实
                    if new_confidence < self.config.fact_confidence_threshold:
                        fact_id = fact.get("id")  # deer-flow 格式使用 "id" 而不是 "fact_id"
                        await self.mongodb_manager.delete_fact(user_id, fact_id)
                        await self.evermem_client.delete_fact(fact_id)
                        removed_count += 1
                    else:
                        # 更新置信度
                        fact_id = fact.get("id")
                        await self.mongodb_manager.update_fact_confidence(user_id, fact_id, new_confidence)
                        # 注意：evermemOS 的置信度更新通过 metadata 实现
                        await self.evermem_client.update_fact(
                            fact_id,
                            metadata={"confidence": new_confidence}
                        )
                        decayed_count += 1

            logger.info(f"记忆衰减完成: user_id={user_id}, 衰减={decayed_count}, 删除={removed_count}")

            return {
                "enabled": True,
                "decayed_count": decayed_count,
                "removed_count": removed_count
            }

        except Exception as e:
            logger.error(f"记忆衰减失败: {e}", exc_info=True)
            return {"enabled": True, "error": str(e)}

    def build_injection_text(
        self,
        user_context: Dict[str, Any],
        facts: List[Dict[str, Any]]
    ) -> str:
        """
        构建用于注入到提示词的记忆文本
        使用优先级策略控制 token 数量

        Args:
            user_context: 用户上下文
            facts: 事实列表

        Returns:
            str: 格式化的记忆文本
        """
        if not self.config.injection_enabled:
            return ""

        return format_memory_for_injection(
            user_context=user_context,
            facts=facts,
            max_tokens=self.config.max_injection_tokens
        )

    async def get_stats(self) -> Dict[str, Any]:
        """
        获取三层记忆系统的统计信息

        Returns:
            Dict: 统计信息
        """
        try:
            redis_stats, mongodb_stats, evermem_stats = await asyncio.gather(
                self.redis_manager.get_stats(),
                self.mongodb_manager.get_stats(),
                self.evermem_client.get_stats(),
                return_exceptions=True
            )

            # 处理异常
            if isinstance(redis_stats, Exception):
                redis_stats = {"error": str(redis_stats)}
            if isinstance(mongodb_stats, Exception):
                mongodb_stats = {"error": str(mongodb_stats)}
            if isinstance(evermem_stats, Exception):
                evermem_stats = {"error": str(evermem_stats)}

            # 获取队列统计
            queue = get_memory_queue()

            return {
                "redis": redis_stats,
                "mongodb": mongodb_stats,
                "evermem": evermem_stats,
                "queue": {
                    "pending_count": queue.get_pending_count(),
                    "pending_users": queue.get_pending_users()
                }
            }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"error": str(e)}


# 全局实例（单例模式）
_global_context_manager: Optional[ThreeLayerContextManager] = None


async def initialize_context_manager() -> ThreeLayerContextManager:
    """
    初始化全局上下文管理器（单例模式）

    Returns:
        ThreeLayerContextManager: 上下文管理器实例
    """
    global _global_context_manager
    if _global_context_manager is None:
        _global_context_manager = ThreeLayerContextManager()
        await _global_context_manager.initialize()
        logger.info("全局三层记忆管理器已初始化")
    return _global_context_manager


def get_context_manager() -> Optional[ThreeLayerContextManager]:
    """
    获取全局上下文管理器实例
    注意：使用前需要先调用 initialize_context_manager()

    Returns:
        Optional[ThreeLayerContextManager]: 上下文管理器实例，未初始化时返回 None
    """
    return _global_context_manager
