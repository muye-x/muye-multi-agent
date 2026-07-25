"""Token 使用统计中间件 - 记录 LLM 调用的 token 消耗"""

import logging
from typing import Optional, Dict, Any

from .base import AgentMiddleware

logger = logging.getLogger(__name__)


class TokenUsageMiddleware(AgentMiddleware):
    """记录模型响应中的 token 使用情况"""

    def __init__(
        self,
        system_prompt: Optional[str] = None,
        verbose: bool = False,  # 是否显示完整内容（不截断）
        show_system_prompt_every_time: bool = False  # 是否每次都显示系统提示词
    ):
        super().__init__()
        self.call_count = 0  # 记录 LLM 调用次数
        self.system_prompt = system_prompt  # 保存系统提示词
        self.verbose = verbose  # 详细模式
        self.show_system_prompt_every_time = show_system_prompt_every_time

    def before_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """LLM 调用前 - 可以在这里打断点查看输入"""
        messages = state.get("messages", [])
        logger.debug("LLM 调用前，消息数量: %s", len(messages))

        # 打印最后一条消息
        if messages:
            last_msg = messages[-1]
            logger.debug("最后一条消息类型: %s", type(last_msg).__name__)
            logger.debug("最后一条消息内容: %s", getattr(last_msg, "content", "N/A"))

        # 在这里打断点可以看到 LLM 输入
        return None

    async def abefore_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """LLM 调用前（异步）- 只打印最新的用户输入"""
        self.call_count += 1
        messages = state.get("messages", [])

        logger.info("\n" + "=" * 100)
        logger.info(f"LLM 调用 #{self.call_count} - 开始")
        logger.info("=" * 100)

        # 如果有系统提示词，根据配置决定是否显示
        if self.system_prompt and (self.call_count == 1 or self.show_system_prompt_every_time):
            logger.debug(f"\n{'─' * 100}")
            logger.debug("系统提示词:")
            logger.debug(f"{'─' * 100}")
            logger.debug(f"{self.system_prompt}")
            logger.debug(f"{'─' * 100}\n")

        logger.info(f"消息总数: {len(messages)}")

        # 只打印最新的用户输入
        if messages:
            # 从后往前找最新的 HumanMessage
            latest_user_msg = None
            for msg in reversed(messages):
                if type(msg).__name__ == "HumanMessage":
                    latest_user_msg = msg
                    break

            if latest_user_msg:
                content = getattr(latest_user_msg, 'content', '')
                logger.info(f"\n{'─' * 100}")
                logger.info("最新用户输入:")
                logger.info(f"{'─' * 100}")
                logger.info(f"{content}")
                logger.info(f"{'─' * 100}")

        logger.debug("\n" + "=" * 100)
        logger.debug("准备调用 LLM...")
        logger.debug("=" * 100 + "\n")

        return None

    def after_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """LLM 调用后 - 可以在这里打断点查看输出"""
        return self._log_usage(state)

    async def aafter_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """LLM 调用后（异步）- 可以在这里打断点查看输出"""
        return self._log_usage(state)

    def _log_usage(self, state) -> None:
        """从最后一条消息中提取并记录 token 使用情况"""
        messages = state.get("messages", [])
        if not messages:
            return None

        last = messages[-1]

        logger.info("\n" + "=" * 100)
        logger.info(f"LLM 调用 #{self.call_count} - 完成")
        logger.info("=" * 100)

        # 打印 LLM 响应详情
        msg_type = type(last).__name__
        content = getattr(last, 'content', 'N/A')

        logger.info(f"响应类型: {msg_type}")

        if msg_type == "AIMessage":
            logger.info("AI 回复:")
            if content:
                # 根据 verbose 模式决定是否截断
                if self.verbose:
                    logger.debug(f"{content}")
                else:
                    preview = content[:500] + '...' if len(content) > 500 else content
                    logger.info(f"{preview}")

            # 检查是否有 tool_calls
            if hasattr(last, 'tool_calls') and last.tool_calls:
                tool_calls = last.tool_calls
                logger.info(f"\n工具调用 ({len(tool_calls)} 个):")
                for idx, tc in enumerate(tool_calls, 1):
                    logger.info(f"  {idx}. 工具: {tc.get('name', 'unknown')}")
                    # 根据 verbose 模式决定参数显示详细程度
                    args = tc.get('args', {})
                    if self.verbose:
                        logger.debug(f"     参数: {args}")
                    else:
                        args_str = str(args)
                        args_preview = args_str[:200] + '...' if len(args_str) > 200 else args_str
                        logger.info(f"     参数: {args_preview}")
                    logger.debug(f"     ID: {tc.get('id', 'N/A')}")

        # Token 使用统计
        usage = getattr(last, "usage_metadata", None)
        if usage:
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)

            logger.info("\nToken 使用统计:")
            logger.info(f"  输入 Token:  {input_tokens:,}")
            logger.info(f"  输出 Token:  {output_tokens:,}")
            logger.info(f"  总计 Token:  {total_tokens:,}")

        logger.info("=" * 100 + "\n")

        return None
