"""
记忆更新队列
实现防抖机制，避免频繁的 LLM 调用
将多个更新请求合并为一次处理
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class MemoryUpdateTask:
    """记忆更新任务"""
    user_id: str
    session_id: str
    messages: List[Any]
    correction_detected: bool = False
    reinforcement_detected: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


class MemoryUpdateQueue:
    """
    记忆更新队列（防抖机制）

    工作原理：
    1. 当有新的对话时，将更新任务添加到队列
    2. 启动一个定时器（默认 30 秒）
    3. 如果在定时器期间又有新的对话，重置定时器
    4. 定时器到期后，批量处理所有待处理的任务
    """

    def __init__(self, debounce_seconds: int = 30):
        """
        初始化队列

        Args:
            debounce_seconds: 防抖延迟时间（秒）
        """
        self.debounce_seconds = debounce_seconds
        self.pending_tasks: Dict[str, MemoryUpdateTask] = {}  # 按 user_id 保存待处理任务
        self.user_timers: Dict[str, asyncio.Task] = {}  # user_id -> timer_task (每个用户独立定时器)
        self.lock = asyncio.Lock()
        self.processor_callback = None  # 处理回调函数

    def set_processor(self, callback):
        """
        设置处理回调函数

        Args:
            callback: 异步回调函数，接收 MemoryUpdateTask 作为参数
        """
        self.processor_callback = callback

    async def add(
        self,
        user_id: str,
        session_id: str,
        messages: List[Any],
        correction_detected: bool = False,
        reinforcement_detected: bool = False
    ) -> None:
        """
        添加更新任务到队列

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            messages: 消息列表
            correction_detected: 是否检测到纠正信号
            reinforcement_detected: 是否检测到强化信号
        """
        async with self.lock:
            # 创建或更新任务
            task = MemoryUpdateTask(
                user_id=user_id,
                session_id=session_id,
                messages=messages.copy(),  # 复制消息列表
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected
            )

            # 如果该用户已有待处理任务，更新为最新的
            self.pending_tasks[user_id] = task

            # 取消该用户的旧定时器（如果存在）
            if user_id in self.user_timers:
                old_timer = self.user_timers[user_id]
                if not old_timer.done():
                    old_timer.cancel()
                    logger.debug(f"取消用户 {user_id} 的旧定时器")

            # 为该用户创建新的独立定时器
            self.user_timers[user_id] = asyncio.create_task(
                self._process_user_after_delay(user_id)
            )

            logger.debug(
                f"任务已添加到队列: user_id={user_id}, "
                f"队列大小={len(self.pending_tasks)}"
            )

    async def _process_user_after_delay(self, user_id: str) -> None:
        """
        等待防抖时间后处理单个用户的任务

        Args:
            user_id: 用户 ID
        """
        try:
            # 等待防抖时间
            await asyncio.sleep(self.debounce_seconds)

            # 时间到，从队列中取出该用户的任务
            async with self.lock:
                task = self.pending_tasks.pop(user_id, None)
                self.user_timers.pop(user_id, None)

            # 如果任务存在且有处理器，执行处理
            if task and self.processor_callback:
                try:
                    logger.info(f"开始处理用户 {user_id} 的记忆更新任务")
                    await self.processor_callback(task)
                    logger.debug(f"用户 {user_id} 的任务处理完成")
                except Exception as e:
                    logger.error(
                        f"处理用户 {user_id} 的任务失败: {e}",
                        exc_info=True
                    )
            elif not task:
                logger.debug(f"用户 {user_id} 的任务已被取消或不存在")

        except asyncio.CancelledError:
            logger.debug(f"用户 {user_id} 的定时器被取消")
            # 不需要 raise，让任务静默结束

    async def flush(self) -> None:
        """
        立即处理队列中的所有任务（取消所有定时器，立即执行）
        """
        async with self.lock:
            if not self.pending_tasks:
                logger.debug("队列为空，无需处理")
                return

            # 取消所有用户的定时器
            for user_id, timer in self.user_timers.items():
                if not timer.done():
                    timer.cancel()

            tasks_to_process = list(self.pending_tasks.values())
            self.pending_tasks.clear()
            self.user_timers.clear()

            logger.info(f"开始立即处理 {len(tasks_to_process)} 个记忆更新任务")

        # 并行处理所有任务
        if self.processor_callback:
            results = await asyncio.gather(
                *[self.processor_callback(task) for task in tasks_to_process],
                return_exceptions=True
            )

            # 统计结果
            success_count = sum(1 for r in results if not isinstance(r, Exception))
            failed_count = len(results) - success_count
            logger.info(
                f"批量处理完成: 成功={success_count}, 失败={failed_count}"
            )

            # 记录失败的任务
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(
                        f"任务处理失败: user_id={tasks_to_process[i].user_id}, "
                        f"error={result}"
                    )
        else:
            logger.warning("未设置处理回调函数，任务被丢弃")

    async def clear(self) -> None:
        """
        清空队列（不处理，直接丢弃所有任务）
        """
        async with self.lock:
            # 取消所有用户的定时器
            for user_id, timer in self.user_timers.items():
                if not timer.done():
                    timer.cancel()
                    try:
                        await timer
                    except asyncio.CancelledError:
                        pass

            # 清空队列
            count = len(self.pending_tasks)
            self.pending_tasks.clear()
            self.user_timers.clear()

            logger.info(f"队列已清空，丢弃了 {count} 个任务")

    def get_pending_count(self) -> int:
        """
        获取待处理任务数量

        Returns:
            int: 待处理任务数量
        """
        return len(self.pending_tasks)

    def get_pending_users(self) -> List[str]:
        """
        获取待处理的用户 ID 列表

        Returns:
            List[str]: 用户 ID 列表
        """
        return list(self.pending_tasks.keys())


# 全局队列实例（单例模式）
_global_queue: Optional[MemoryUpdateQueue] = None


def get_memory_queue(debounce_seconds: Optional[int] = None) -> MemoryUpdateQueue:
    """
    获取全局记忆更新队列实例（单例模式）

    Args:
        debounce_seconds: 可选的防抖时间，仅在首次创建时有效

    Returns:
        MemoryUpdateQueue: 队列实例
    """
    global _global_queue
    if _global_queue is None:
        from config import get_config
        config = get_config()
        debounce = debounce_seconds if debounce_seconds is not None else config.memory.debounce_seconds
        _global_queue = MemoryUpdateQueue(debounce_seconds=debounce)
        logger.info(f"全局记忆更新队列已创建，防抖时间={debounce}秒")
    return _global_queue
