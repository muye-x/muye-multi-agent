"""
消息压缩器
负责将历史对话压缩成摘要，节省 LLM 上下文空间
"""
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# 对话压缩提示词
CONVERSATION_SUMMARY_PROMPT = """你是一个对话历史压缩助手。请将以下对话历史压缩成简洁的摘要。

对话历史：
{conversation_history}

压缩要求：
1. 保留关键信息：用户的主要需求、讨论的技术话题、已解决的问题
2. 去除冗余：重复的澄清、失败的尝试、无关的闲聊
3. 结构化输出：按时间顺序或主题分类
4. 控制长度：500-1000 tokens

输出格式（Markdown）：
## 对话摘要（第 {start_turn}-{end_turn} 轮）

### 主要话题
- 话题1：简要描述
- 话题2：简要描述

### 关键决策
- 决策1：背景 + 结果
- 决策2：背景 + 结果

### 技术上下文
- 使用的技术栈
- 遇到的问题和解决方案

请直接输出摘要，不要添加额外的解释。
"""


class MessageCompressor:
    """消息压缩器"""

    def __init__(self, llm: Optional[ChatOpenAI] = None):
        """
        初始化压缩器

        Args:
            llm: LLM 实例（可选，不提供则使用默认配置）
        """
        if llm is None:
            from config import get_config
            config = get_config()
            llm = ChatOpenAI(
                model=config.llm.model,
                temperature=0.3,  # 压缩任务使用较低温度
                max_tokens=2000,
                openai_api_base=config.llm.api_base,
                openai_api_key=config.llm.api_key,
            )
        self.llm = llm

    def should_compress(
        self,
        total_turns: int,
        last_compression_turn: Optional[int],
        compression_threshold: int,
        compression_interval: int
    ) -> bool:
        """
        判断是否需要重新压缩

        Args:
            total_turns: 总轮次
            last_compression_turn: 上次压缩的轮次
            compression_threshold: 压缩阈值
            compression_interval: 压缩间隔

        Returns:
            bool: 是否需要压缩
        """
        if total_turns <= compression_threshold:
            return False

        if last_compression_turn is None:
            return True  # 首次压缩

        # 每 N 轮重新压缩一次
        return (total_turns - last_compression_turn) >= compression_interval

    async def compress_messages(
        self,
        messages: List[BaseMessage],
        start_turn: int,
        end_turn: int
    ) -> str:
        """
        压缩消息列表为摘要

        Args:
            messages: 消息列表
            start_turn: 起始轮次
            end_turn: 结束轮次

        Returns:
            str: 压缩后的摘要
        """
        try:
            # 格式化对话历史
            conversation_text = self._format_messages(messages)

            # 构建压缩提示词
            prompt = CONVERSATION_SUMMARY_PROMPT.format(
                conversation_history=conversation_text,
                start_turn=start_turn,
                end_turn=end_turn
            )

            # 调用 LLM 压缩
            logger.info(f"[MessageCompressor] 开始压缩第 {start_turn}-{end_turn} 轮对话...")
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])

            summary = response.content.strip()
            logger.info(f"[MessageCompressor] 压缩完成，摘要长度: {len(summary)} 字符")

            return summary

        except Exception as e:
            logger.error(f"压缩消息失败: {e}", exc_info=True)
            # 降级方案：返回简单的统计信息
            return self._fallback_summary(messages, start_turn, end_turn)

    def _format_messages(self, messages: List[BaseMessage]) -> str:
        """
        格式化消息列表为文本

        Args:
            messages: 消息列表

        Returns:
            str: 格式化后的文本
        """
        from config import get_config
        config = get_config()
        max_length = config.content_processing.compression_summary_length

        lines = []
        for msg in messages:
            # 跳过系统消息和工具消息
            if msg.type in ('system', 'tool'):
                continue

            content = msg.content
            if not content or not content.strip():
                continue

            # 截断过长的消息
            if len(content) > max_length:
                content = content[:max_length] + "..."

            if msg.type in ('human', 'user'):
                lines.append(f"User: {content}")
            elif msg.type in ('ai', 'assistant'):
                lines.append(f"Assistant: {content}")

        return "\n\n".join(lines)

    def _fallback_summary(
        self,
        messages: List[BaseMessage],
        start_turn: int,
        end_turn: int
    ) -> str:
        """
        降级方案：生成简单的统计摘要

        Args:
            messages: 消息列表
            start_turn: 起始轮次
            end_turn: 结束轮次

        Returns:
            str: 简单摘要
        """
        user_count = sum(1 for m in messages if m.type in ('human', 'user'))
        ai_count = sum(1 for m in messages if m.type in ('ai', 'assistant'))

        return f"""## 对话摘要（第 {start_turn}-{end_turn} 轮）

本段对话包含 {user_count} 条用户消息和 {ai_count} 条 AI 回复。
由于压缩失败，详细内容已省略。
"""

    def split_messages_by_window(
        self,
        messages: List[BaseMessage],
        hot_window_size: int
    ) -> Tuple[List[BaseMessage], List[BaseMessage]]:
        """
        将消息分为冷数据和热数据

        Args:
            messages: 消息列表
            hot_window_size: 热数据窗口大小（轮次）

        Returns:
            Tuple[冷数据消息, 热数据消息]
        """
        # 过滤出用户和 AI 消息（用于计算轮次）
        conversation_messages = [
            m for m in messages
            if m.type in ('human', 'user', 'ai', 'assistant')
        ]

        # 计算总轮次（一轮 = 一对用户+AI消息）
        total_turns = len([m for m in conversation_messages if m.type in ('human', 'user')])

        if total_turns <= hot_window_size:
            # 不需要分割
            return [], messages

        # 计算热数据的起始位置
        # 保留最近的 hot_window_size 轮对话
        hot_start_index = len(conversation_messages) - (hot_window_size * 2)
        if hot_start_index < 0:
            hot_start_index = 0

        # 找到对应的原始消息索引
        conversation_index = 0
        split_index = 0
        for i, msg in enumerate(messages):
            if msg.type in ('human', 'user', 'ai', 'assistant'):
                if conversation_index >= hot_start_index:
                    split_index = i
                    break
                conversation_index += 1

        cold_messages = messages[:split_index]
        hot_messages = messages[split_index:]

        logger.info(f"[MessageCompressor] 消息分割: 冷数据 {len(cold_messages)} 条, 热数据 {len(hot_messages)} 条")

        return cold_messages, hot_messages

    def count_turns(self, messages: List[BaseMessage]) -> int:
        """
        计算对话轮次

        Args:
            messages: 消息列表

        Returns:
            int: 轮次数
        """
        return len([m for m in messages if m.type in ('human', 'user')])
