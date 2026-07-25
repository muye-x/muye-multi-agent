"""
消息压缩中间件
在 Agent 执行前对历史消息进行压缩处理
"""
import logging
from typing import Any, Dict, Optional
from langchain_core.messages import SystemMessage

from .base import AgentMiddleware
from core.compressor import MessageCompressor
from config import get_config
from utils.profiler import RequestProfiler

logger = logging.getLogger(__name__)


class MessageCompressionMiddleware(AgentMiddleware):
    """
    消息压缩中间件

    功能：
    1. 在 Agent 执行前（before_agent）：检查消息历史长度
    2. 如果超过阈值：将冷数据压缩成摘要，保留热数据完整
    3. 如果未超过阈值：直接使用完整历史
    """

    def __init__(self, llm=None):
        """
        初始化消息压缩中间件

        Args:
            llm: LLM 实例（用于压缩，可选）
        """
        super().__init__()
        self.compressor = MessageCompressor(llm)
        self.config = get_config().compression
        self._compression_cache = {}  # 缓存压缩结果

    async def abefore_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        """
        在 Agent 执行前压缩消息（已废弃，逻辑移至 abefore_model）

        Args:
            state: Agent 状态（包含 messages）
            runtime: 运行时信息

        Returns:
            None
        """
        # 不在这里处理，改为在 abefore_model 中处理
        # 因为 abefore_agent 的返回值不会影响到 abefore_model 看到的 state
        return None

    async def abefore_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """
        在模型调用前压缩消息到 working_messages（不影响持久化的 messages）

        Args:
            state: Agent 状态（包含 messages）
            runtime: 运行时信息

        Returns:
            更新后的状态（包含 working_messages）
        """
        # 根据配置的模式选择处理方式
        mode = self.config.mode

        if mode == 'none':
            # 不处理，直接复制 messages 到 working_messages
            logger.info(f"[CompressionMiddleware] mode=none，返回完整 messages: {len(state.get('messages', []))} 条")
            working_messages = state.get("messages", [])
            return {"working_messages": working_messages}
        elif mode == 'truncate':
            # 截断模式：保留最近 N 轮对话
            return await self._truncate_by_turns(state)
        elif mode == 'compress':
            # 压缩模式：压缩旧消息
            if not self.config.enable_compression:
                # 如果禁用压缩，降级为简单截断
                return await self._simple_trim(state)
            return await self._compress_messages(state)
        else:
            logger.warning(f"[CompressionMiddleware] 未知的压缩模式: {mode}，使用截断模式")
            return await self._truncate_by_turns(state)

    async def _compress_messages(self, state) -> Optional[Dict[str, Any]]:
        """
        压缩消息到 working_messages（不修改持久化的 messages）

        Args:
            state: Agent 状态

        Returns:
            包含 working_messages 的更新
        """
        try:
            messages = state.get("messages", [])
            if not messages:
                return {"working_messages": []}

            # 计算对话轮次
            total_turns = self.compressor.count_turns(messages)

            # 如果轮次少，直接返回完整历史
            if total_turns <= self.config.compression_threshold:
                logger.debug(f"[CompressionMiddleware] 对话轮次 {total_turns} 未达到压缩阈值，使用完整历史")
                logger.info(f"[CompressionMiddleware] 返回完整 messages: {len(messages)} 条")
                return {"working_messages": messages}

            # 分割冷热数据
            cold_messages, hot_messages = self.compressor.split_messages_by_window(
                messages,
                self.config.hot_window_size
            )

            if not cold_messages:
                # 没有冷数据，直接返回完整历史
                return {"working_messages": messages}

            # 生成缓存键（使用更安全的分隔符）
            session_id = state.get("session_id", "default")
            cache_key = f"{session_id}::{total_turns}"

            # 检查缓存
            if cache_key in self._compression_cache:
                summary = self._compression_cache[cache_key]
                logger.debug(f"[CompressionMiddleware] 使用缓存的压缩摘要")
            else:
                # 压缩冷数据
                cold_turns = self.compressor.count_turns(cold_messages)
                logger.info(f"[CompressionMiddleware] 开始压缩第 1-{cold_turns} 轮对话...")

                profiler = RequestProfiler.get_current()
                if profiler:
                    async with profiler.async_timing("message_compress"):
                        summary = await self.compressor.compress_messages(
                            cold_messages,
                            start_turn=1,
                            end_turn=cold_turns
                        )
                else:
                    summary = await self.compressor.compress_messages(
                        cold_messages,
                        start_turn=1,
                        end_turn=cold_turns
                    )

                # 缓存结果（限制缓存大小）
                if len(self._compression_cache) > 100:
                    # 清理最旧的缓存
                    oldest_key = next(iter(self._compression_cache))
                    del self._compression_cache[oldest_key]

                self._compression_cache[cache_key] = summary

            # 构建 working_messages：系统消息 + 压缩摘要 + 热数据
            system_messages = [m for m in messages if hasattr(m, 'type') and m.type == 'system']
            summary_message = SystemMessage(content=f"[历史对话摘要]\n{summary}\n[/历史对话摘要]")

            working_messages = system_messages + [summary_message] + hot_messages

            logger.debug(f"[CompressionMiddleware] 消息压缩完成: {len(messages)} 条 -> {len(working_messages)} 条（压缩 {len(cold_messages)} 条，保留 {len(hot_messages)} 条）")
            logger.info(f"[CompressionMiddleware] 返回 working_messages: {len(working_messages)} 条")

            # 返回 working_messages（不修改 messages）
            return {"working_messages": working_messages}

        except Exception as e:
            logger.error(f"消息压缩失败: {e}", exc_info=True)
            # 失败时返回完整历史
            working_messages = state.get("messages", [])
            return {"working_messages": working_messages}

    async def _truncate_by_turns(self, state) -> Optional[Dict[str, Any]]:
        """
        按轮次截断策略：保留最近 N 轮对话到 working_messages

        Args:
            state: Agent 状态

        Returns:
            包含 working_messages 的更新
        """
        messages = state.get("messages", [])
        keep_turns = self.config.keep_recent_turns

        if keep_turns <= 0 or not messages:
            # 不限制或没有消息
            return {"working_messages": messages}

        # 分离系统消息和对话消息
        from middleware.utils import split_system_messages
        system_messages, conversation_messages = split_system_messages(messages)

        if len(system_messages)>1:
            system_messages = [system_messages[-1]]
        # 计算对话轮次
        total_turns = self.compressor.count_turns(conversation_messages)

        if total_turns <= keep_turns:
            # 轮次未超过限制，不需要截断
            logger.debug(f"[CompressionMiddleware] 对话轮次 {total_turns} 未超过限制 {keep_turns}，保留完整历史")
            return {"working_messages": messages}

        # 找到最近 N 轮对话的起始位置
        # 从后往前数，找到第 keep_turns 轮的开始位置
        turn_count = 0
        start_index = len(conversation_messages)

        for i in range(len(conversation_messages) - 1, -1, -1):
            msg = conversation_messages[i]
            # 用户消息标志着新一轮的开始
            if hasattr(msg, 'type') and msg.type in ('human', 'user'):
                turn_count += 1
                if turn_count == keep_turns:
                    start_index = i
                    break

        # 构建 working_messages：系统消息 + 最近 N 轮对话
        recent_messages = conversation_messages[start_index:]
        working_messages = system_messages + recent_messages

        # 修复 tool_calls 配对关系
        working_messages = self._fix_tool_calls_pairing(working_messages)

        logger.debug(f"[CompressionMiddleware] 按轮次截断: {len(messages)} 条 -> {len(working_messages)} 条（保留最近 {keep_turns}/{total_turns} 轮）")

        # 返回 working_messages
        return {"working_messages": working_messages}


    async def _simple_trim(self, state) -> Optional[Dict[str, Any]]:
        """
        简单截断策略（禁用压缩时使用）到 working_messages

        Args:
            state: Agent 状态

        Returns:
            包含 working_messages 的更新
        """
        messages = state.get("messages", [])
        max_messages = self.config.max_messages

        if len(messages) <= max_messages:
            return {"working_messages": messages}

        # 分离系统消息和其他消息
        from middleware.utils import split_system_messages
        system_messages, other_messages = split_system_messages(messages)

        # 计算需要保留的非系统消息数量
        keep_count = max_messages - len(system_messages)
        if keep_count <= 0:
            logger.warning(f"[CompressionMiddleware] 系统消息过多 ({len(system_messages)})，无法截断")
            return {"working_messages": messages}

        # 保留最近的非系统消息
        recent_messages = other_messages[-keep_count:]
        working_messages = system_messages + recent_messages

        # 修复 tool_calls 配对关系
        working_messages = self._fix_tool_calls_pairing(working_messages)

        logger.debug(f"[CompressionMiddleware] 消息截断: {len(messages)} 条 -> {len(working_messages)} 条")

        # 返回 working_messages
        return {"working_messages": working_messages}

    def _fix_tool_calls_pairing(self, messages: list) -> list:
        """
        修复截断后的消息序列，确保 tool_calls 和 tool 消息配对完整

        规则：
        1. 如果 assistant 消息包含 tool_calls，但后面没有足够的 tool 消息
        2. 则删除该 assistant 消息（避免 400 错误）

        Args:
            messages: 截断后的消息列表

        Returns:
            修复后的消息列表
        """
        if not messages:
            return messages

        fixed_messages = []
        i = 0

        while i < len(messages):
            msg = messages[i]

            # 检查是否是带 tool_calls 的 assistant 消息
            if hasattr(msg, 'type') and msg.type == 'ai':
                # 检查是否有 tool_calls
                tool_calls = getattr(msg, 'tool_calls', None) or []

                if tool_calls:
                    # 统计后续的 tool 消息数量
                    tool_call_ids = {tc.get('id') for tc in tool_calls if isinstance(tc, dict) and 'id' in tc}

                    # 查找紧跟的 tool 消息
                    tool_responses = []
                    j = i + 1
                    while j < len(messages) and hasattr(messages[j], 'type') and messages[j].type == 'tool':
                        tool_responses.append(messages[j])
                        j += 1

                    # 检查 tool_call_id 是否都有响应
                    response_ids = {getattr(tr, 'tool_call_id', None) for tr in tool_responses}

                    if tool_call_ids.issubset(response_ids):
                        # 配对完整，保留所有消息
                        fixed_messages.append(msg)
                        fixed_messages.extend(tool_responses)
                        i = j
                    else:
                        # 配对不完整，丢弃这个 assistant 消息和不完整的 tool 消息
                        logger.warning(
                            f"[CompressionMiddleware] 检测到不完整的 tool_calls 配对，"
                            f"丢弃 {len(tool_calls)} 个 tool_calls 和 {len(tool_responses)} 个 tool 响应"
                        )
                        i = j  # 跳过所有相关消息
                else:
                    # 普通 assistant 消息，直接保留
                    fixed_messages.append(msg)
                    i += 1
            else:
                # 非 assistant 消息，直接保留
                fixed_messages.append(msg)
                i += 1

        if len(fixed_messages) != len(messages):
            logger.info(f"[CompressionMiddleware] 修复消息配对: {len(messages)} 条 -> {len(fixed_messages)} 条")

        return fixed_messages

    async def close(self):
        """清理资源"""
        self._compression_cache.clear()
        logger.info("消息压缩中间件已关闭")
