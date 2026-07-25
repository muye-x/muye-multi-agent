"""
记忆中间件
在 Agent 执行前后自动加载和保存记忆
"""
import asyncio
import logging
from typing import Any, Dict, Optional
from langchain_core.messages import SystemMessage

from .base import AgentMiddleware
from memory.context import ThreeLayerContextManager
from utils.profiler import RequestProfiler

logger = logging.getLogger(__name__)


class MemoryMiddleware(AgentMiddleware):
    """
    记忆中间件

    功能：
    1. 在 Agent 执行前（before_agent）：加载用户的三层记忆并注入到上下文
    2. 在 Agent 执行后（after_agent）：保存本轮交互到记忆系统
    """

    def __init__(
        self,
        context_manager: Optional[ThreeLayerContextManager] = None,
        inject_memory: bool = True,
        auto_save: bool = True
    ):
        """
        初始化记忆中间件

        Args:
            context_manager: 三层记忆管理器（可选，不提供则创建新实例）
            inject_memory: 是否将记忆注入到提示词（默认 True）
            auto_save: 是否自动保存交互（默认 True）
        """
        super().__init__()
        self.context_manager = context_manager or ThreeLayerContextManager()
        self.inject_memory = inject_memory
        self.auto_save = auto_save
        self._initialized = False

    async def _ensure_initialized(self):
        """确保记忆系统已初始化"""
        if not self._initialized:
            await self.context_manager.initialize()
            self._initialized = True

    async def abefore_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        """
        在 Agent 执行前的钩子

        注意：记忆注入已改为在 abefore_model 中动态注入到系统提示词

        Args:
            state: Agent 状态（包含 messages, user_id, session_id）
            runtime: 运行时信息

        Returns:
            None（不修改状态）
        """
        profiler = RequestProfiler.get_current()
        if profiler:
            async with profiler.async_timing("memory_init"):
                await self._ensure_initialized()
        else:
            await self._ensure_initialized()

        # 记忆注入已移至 abefore_model，这里不再需要注入到 messages
        # 只记录日志用于调试
        if self.inject_memory:
            user_id = state.get("user_id", "default_user")
            session_id = state.get("session_id", "default_session")
            logger.debug(f"[MemoryMiddleware] 记忆将通过 abefore_model 动态注入: user_id={user_id}, session_id={session_id}")

        return None

    async def abefore_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """
        在模型调用前动态注入记忆到 working_messages

        这个钩子在每次 LLM 调用前执行，将记忆注入到 working_messages
        不影响持久化的 messages

        Args:
            state: Agent 状态
            runtime: 运行时信息

        Returns:
            包含更新后 working_messages 的字典
        """
        await self._ensure_initialized()

        if not self.inject_memory:
            return None

        try:
            from langchain_core.messages import SystemMessage

            # 从 state 中提取 user_id 和 session_id
            user_id = state.get("user_id", "default_user")
            session_id = state.get("session_id", "default_session")

            logger.debug(f"[MemoryMiddleware] abefore_model - user_id={user_id}, session_id={session_id}")


            # 加载用户的三层记忆（最新的）
            profiler = RequestProfiler.get_current()
            if profiler:
                async with profiler.async_timing("memory_load"):
                    context = await self.context_manager.load_context(user_id, session_id)
            else:
                context = await self.context_manager.load_context(user_id, session_id)

            # 构建记忆注入文本
            memory_text = self.context_manager.build_injection_text(
                user_context=context.get("structured_long_term", {}),
                facts=context.get("structured_long_term", {}).get("facts", [])
            )

            if not memory_text:
                logger.debug(f"[MemoryMiddleware] 没有记忆需要注入")
                return None

            logger.info(f"[MemoryMiddleware] 在 before_model 中动态注入记忆，长度: {len(memory_text)}")

            # 获取 working_messages（如果存在）或 messages
            working_messages = state.get("working_messages") or state.get("messages", [])

            # 过滤掉旧的记忆消息（如果有）
            filtered_messages = [
                msg for msg in working_messages
                if not (hasattr(msg, 'additional_kwargs') and
                       msg.additional_kwargs.get("_is_memory_injection"))
            ]

            # 构建新的记忆消息（使用 SystemMessage，添加特殊标记）
            memory_message = SystemMessage(
                content=f"<memory>\n{memory_text}\n</memory>",
                additional_kwargs={"_is_memory_injection": True}  # 添加标记，用于识别
            )

            # 将记忆消息插入到系统消息之后
            # 找到最后一个系统消息的位置
            last_system_index = -1
            for i, msg in enumerate(filtered_messages):
                if hasattr(msg, 'type') and msg.type == 'system':
                    last_system_index = i

            # 插入记忆消息
            if last_system_index >= 0:
                new_working_messages = (
                    filtered_messages[:last_system_index + 1] +
                    [memory_message] +
                    filtered_messages[last_system_index + 1:]
                )
            else:
                # 没有系统消息，插入到开头
                new_working_messages = [memory_message] + filtered_messages

            logger.debug(f"[MemoryMiddleware] 注入记忆后 working_messages 数量: {len(new_working_messages)}")

            return {"working_messages": new_working_messages}

        except Exception as e:
            logger.error(f"动态注入记忆失败: {e}", exc_info=True)

        return None

    async def aafter_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """
        在模型调用后清理临时注入的记忆消息

        Args:
            state: Agent 状态
            runtime: 运行时信息

        Returns:
            清理后的状态
        """
        if not self.inject_memory:
            return None

        try:
            messages = state.get("messages", [])
            if not messages:
                return None

            # 过滤掉记忆注入消息（通过 additional_kwargs 标记识别）
            cleaned_messages = []
            removed_count = 0

            for msg in messages:
                # 检查是否是记忆注入消息
                is_memory = False
                if hasattr(msg, 'additional_kwargs'):
                    is_memory = msg.additional_kwargs.get("_is_memory_injection", False)

                # 也检查内容（兼容旧的注入方式）
                if not is_memory and hasattr(msg, 'type') and msg.type == 'system':
                    if hasattr(msg, 'content') and '<memory>' in msg.content:
                        is_memory = True

                if is_memory:
                    removed_count += 1
                    logger.debug(f"[MemoryMiddleware] 清理记忆注入消息")
                else:
                    cleaned_messages.append(msg)

            if removed_count > 0:
                logger.info(f"[MemoryMiddleware] 清理了 {removed_count} 条记忆注入消息")
                return {"messages": cleaned_messages}

        except Exception as e:
            logger.error(f"清理记忆消息失败: {e}", exc_info=True)

        return None

    async def _save_interaction_from_state(self, state, runtime) -> None:
        """
        后台任务：从 state 中提取消息并保存交互

        这个方法包含原来 aafter_agent 的所有逻辑，完全在后台执行

        Args:
            state: Agent 状态
            runtime: 运行时信息
        """
        try:
            # 从 state 中提取 user_id 和 session_id
            user_id = state.get("user_id", "default_user")
            session_id = state.get("session_id", "default_session")

            logger.info(f"[MemoryMiddleware] 后台任务：开始处理交互 user_id={user_id}, session_id={session_id}")

            # 提取最后一轮对话
            messages = state.get("messages", [])
            if not messages:
                logger.info(f"[MemoryMiddleware] 后台任务：没有消息需要保存")
                return

            user_message = None
            assistant_message = None

            # 从后往前找最近的一轮对话
            for msg in reversed(messages):
                if hasattr(msg, 'type'):
                    if msg.type == 'ai' and assistant_message is None:
                        assistant_message = msg.content
                    elif msg.type in ('human', 'user') and user_message is None:
                        # 跳过我们注入的记忆消息
                        content = msg.content
                        # 处理多模态内容（list）和文本内容（str）
                        if isinstance(content, str):
                            if not content.startswith("[用户记忆上下文]"):
                                user_message = content
                        elif isinstance(content, list):
                            # 提取文本部分
                            text_parts = [
                                block.get("text", "")
                                for block in content
                                if isinstance(block, dict) and block.get("type") == "text"
                            ]
                            text_content = " ".join(text_parts)
                            if text_content and not text_content.startswith("[用户记忆上下文]"):
                                user_message = text_content

                # 找到一轮完整对话就停止
                if user_message and assistant_message:
                    break

            # 如果找到了完整的一轮对话，检查是否值得保存
            if user_message and assistant_message:
                # 过滤无效对话（寒暄、简单问候等）
                if not self._is_valuable_conversation(user_message, assistant_message):
                    logger.info(f"[MemoryMiddleware] 后台任务：对话无实质内容，跳过保存: {user_message[:30]}...")
                    return

                logger.info(f"[MemoryMiddleware] 后台任务：保存交互: user_id={user_id}, session_id={session_id}")
                logger.info(f"[MemoryMiddleware] 后台任务：用户消息: {user_message[:50]}...")
                logger.info(f"[MemoryMiddleware] 后台任务：AI消息: {assistant_message[:50]}...")

                await self.context_manager.save_interaction(
                    user_id=user_id,
                    session_id=session_id,
                    user_message=user_message,
                    assistant_message=assistant_message
                )
                logger.info(f"[MemoryMiddleware] 后台任务：交互已保存 user_id={user_id}")
            else:
                logger.warning(f"[MemoryMiddleware] 后台任务：未找到完整对话: user_message={user_message is not None}, assistant_message={assistant_message is not None}")

        except Exception as e:
            logger.error(f"[MemoryMiddleware] 后台任务：保存交互失败 error={e}", exc_info=True)

    async def _save_interaction_background(
        self,
        user_id: str,
        session_id: str,
        user_message: str,
        assistant_message: str
    ) -> None:
        """
        后台任务：异步保存交互到记忆系统（方案 C 使用）

        这个方法在后台执行，不阻塞主流程（不阻塞锁释放）

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            user_message: 用户消息
            assistant_message: AI 回复
        """
        try:
            logger.info(f"[MemoryMiddleware] 后台任务：开始保存交互 user_id={user_id}")
            await self.context_manager.save_interaction(
                user_id=user_id,
                session_id=session_id,
                user_message=user_message,
                assistant_message=assistant_message
            )
            logger.info(f"[MemoryMiddleware] 后台任务：交互已保存 user_id={user_id}")
        except Exception as e:
            logger.error(f"[MemoryMiddleware] 后台任务：保存交互失败 user_id={user_id}, error={e}", exc_info=True)

    async def aafter_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        """
        在 Agent 执行后保存交互

        Args:
            state: Agent 状态（包含 messages, user_id, session_id）
            runtime: 运行时信息

        Returns:
            None（不修改状态）
        """
        if not self.auto_save:
            return None

        # 在主流程中提取当前轮次，再将持久化任务放入后台，避免阻塞 Agent 响应。
        try:
            # 从 state 中提取 user_id 和 session_id
            user_id = state.get("user_id", "default_user")
            session_id = state.get("session_id", "default_session")

            logger.info(f"[MemoryMiddleware] aafter_agent - user_id: {user_id}, session_id: {session_id}")

            # 提取最后一轮对话
            messages = state.get("messages", [])
            if not messages:
                logger.info(f"[MemoryMiddleware] 没有消息需要保存")
                return None

            user_message = None
            assistant_message = None

            # 从后往前找最近的一轮对话
            for msg in reversed(messages):
                if hasattr(msg, 'type'):
                    if msg.type == 'ai' and assistant_message is None:
                        assistant_message = msg.content
                    elif msg.type in ('human', 'user') and user_message is None:
                        # 跳过我们注入的记忆消息
                        content = msg.content
                        # 处理多模态内容（list）和文本内容（str）
                        if isinstance(content, str):
                            if not content.startswith("[用户记忆上下文]"):
                                user_message = content
                        elif isinstance(content, list):
                            # 提取文本部分
                            text_parts = [
                                block.get("text", "")
                                for block in content
                                if isinstance(block, dict) and block.get("type") == "text"
                            ]
                            text_content = " ".join(text_parts)
                            if text_content and not text_content.startswith("[用户记忆上下文]"):
                                user_message = text_content

                # 找到一轮完整对话就停止
                if user_message and assistant_message:
                    break

            # 如果找到了完整的一轮对话，检查是否值得保存
            if user_message and assistant_message:
                # 过滤无效对话（寒暄、简单问候等）
                if not self._is_valuable_conversation(user_message, assistant_message):
                    logger.info(f"[MemoryMiddleware] 对话无实质内容，跳过保存: {user_message[:30]}...")
                    return None

                logger.info(f"[MemoryMiddleware] 保存交互: user_id={user_id}, session_id={session_id}")
                logger.info(f"[MemoryMiddleware] 用户消息: {user_message[:50]}...")
                logger.info(f"[MemoryMiddleware] AI消息: {assistant_message[:50]}...")

                asyncio.create_task(
                    self._save_interaction_background(
                        user_id=user_id,
                        session_id=session_id,
                        user_message=user_message,
                        assistant_message=assistant_message
                    )
                )
                logger.info(f"[MemoryMiddleware] 交互保存任务已提交到后台")
            else:
                logger.warning(f"[MemoryMiddleware] 未找到完整对话: user_message={user_message is not None}, assistant_message={assistant_message is not None}")

        except Exception as e:
            logger.error(f"保存交互失败: {e}", exc_info=True)

        return None

    def _is_valuable_conversation(self, user_message: str, assistant_message: str) -> bool:
        """
        判断对话是否有价值，值得保存到记忆系统

        过滤规则：
        1. 简单问候（你好、hi、hello 等）
        2. 简单感谢（谢谢、thanks 等）
        3. 确认词（好的、ok、嗯 等）
        4. 过短的对话（用户消息 < 3 字符）

        Args:
            user_message: 用户消息
            assistant_message: AI 回复

        Returns:
            bool: True 表示有价值，False 表示无价值
        """
        # 去除空白字符
        user_msg = user_message.strip()
        assistant_msg = assistant_message.strip()

        # 过短的消息
        if len(user_msg) < 3:
            return False

        # 简单问候模式（中英文）
        greetings = [
            "你好", "您好", "hi", "hello", "hey", "早上好", "下午好", "晚上好",
            "早安", "晚安", "good morning", "good afternoon", "good evening"
        ]
        if user_msg.lower() in greetings:
            return False

        # 简单感谢模式
        thanks = [
            "谢谢", "谢了", "多谢", "感谢", "thanks", "thank you", "thx"
        ]
        if user_msg.lower() in thanks:
            return False

        # 简单确认词
        confirmations = [
            "好", "好的", "ok", "okay", "嗯", "嗯嗯", "行", "可以", "是的", "对",
            "yes", "yeah", "yep", "sure", "alright"
        ]
        if user_msg.lower() in confirmations:
            return False

        # 简单否定词
        negations = [
            "不", "不用", "不要", "算了", "no", "nope", "nah"
        ]
        if user_msg.lower() in negations:
            return False

        # 如果通过了所有过滤器，认为是有价值的对话
        return True

    async def close(self):
        """关闭记忆系统（刷新队列并关闭连接）"""
        if self._initialized:
            await self.context_manager.close()
            self._initialized = False
            logger.info("记忆中间件已关闭")
