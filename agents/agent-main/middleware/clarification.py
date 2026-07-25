"""澄清请求中间件 - 拦截澄清请求并标记为特殊 ToolMessage"""

from typing import Any, Optional, Dict
from collections.abc import Callable, Awaitable

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from .base import AgentMiddleware


class ClarificationMiddleware(AgentMiddleware):
    """拦截澄清工具调用并标记为特殊 ToolMessage"""

    def _format_clarification_message(self, args: dict) -> str:
        """将澄清参数格式化为用户友好的 Markdown 消息

        Args:
            args: 包含澄清详情的工具调用参数

        Returns:
            格式化的 Markdown 字符串
        """
        question = args.get("question", "")
        clarification_type = args.get("clarification_type", "missing_info")
        context = args.get("context")
        options = args.get("options", [])

        # 类型特定图标
        type_icons = {
            "missing_info": "❓",
            "ambiguous_requirement": "🤔",
            "approach_choice": "🔀",
            "risk_confirmation": "⚠️",
            "suggestion": "💡",
        }

        icon = type_icons.get(clarification_type, "❓")

        # 构建 Markdown 格式的消息
        message_parts = []
        message_parts.append(f"## {icon} 需要您的确认\n")

        if context:
            message_parts.append(f"**背景**：{context}\n")

        message_parts.append(f"**问题**：{question}\n")

        if options and len(options) > 0:
            message_parts.append("\n**请选择**：")
            for i, option in enumerate(options, 1):
                message_parts.append(f"{i}. {option}")

        return "\n".join(message_parts)

    def _handle_clarification(self, request: ToolCallRequest) -> ToolMessage:
        """处理澄清请求并返回特殊标记的 ToolMessage

        Args:
            request: 工具调用请求

        Returns:
            包含澄清内容的 ToolMessage（带特殊标记）
        """
        # 提取澄清参数
        args = request.tool_call.get("args", {})
        question = args.get("question", "")

        print("[ClarificationMiddleware] 拦截到澄清请求")
        print(f"[ClarificationMiddleware] 问题: {question}")

        # 格式化澄清消息
        formatted_message = self._format_clarification_message(args)

        # 获取工具调用 ID
        tool_call_id = request.tool_call.get("id", "")

        # 返回特殊标记的 ToolMessage
        # 使用 additional_kwargs 标记这是一个澄清请求
        return ToolMessage(
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
            additional_kwargs={
                "_is_clarification": True,  # 特殊标记
                "clarification_type": args.get("clarification_type", "missing_info"),
                "raw_args": args
            }
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """拦截 ask_clarification 工具调用（同步版本）"""
        if request.tool_call.get("name") != "ask_clarification":
            return handler(request)

        return self._handle_clarification(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        """拦截 ask_clarification 工具调用（异步版本）"""
        if request.tool_call.get("name") != "ask_clarification":
            return await handler(request)

        return self._handle_clarification(request)