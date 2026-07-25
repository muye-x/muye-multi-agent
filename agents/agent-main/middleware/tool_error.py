"""工具错误处理中间件 - 将工具执行异常转换为错误消息，使运行可以继续"""

import logging
from typing import Any
from collections.abc import Callable, Awaitable

from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from .base import AgentMiddleware

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"


class ToolErrorHandlingMiddleware(AgentMiddleware):
    """将工具异常转换为错误 ToolMessage，使运行可以继续"""

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        """构建错误消息"""
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__

        # 限制错误详情长度
        if len(detail) > 500:
            detail = detail[:497] + "..."

        content = (
            f"错误: 工具 '{tool_name}' 执行失败，"
            f"异常类型 {exc.__class__.__name__}: {detail}。"
            f"请使用可用的上下文继续，或选择替代工具。"
        )

        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """同步版本：捕获工具执行异常"""
        try:
            return handler(request)
        except GraphBubbleUp:
            # 保留 LangGraph 控制流信号（interrupt/pause/resume）
            raise
        except Exception as exc:
            logger.exception(
                "工具执行失败 (同步): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id")
            )
            return self._build_error_message(request, exc)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """异步版本：捕获工具执行异常"""
        try:
            return await handler(request)
        except GraphBubbleUp:
            # 保留 LangGraph 控制流信号（interrupt/pause/resume）
            raise
        except Exception as exc:
            logger.exception(
                "工具执行失败 (异步): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id")
            )
            return self._build_error_message(request, exc)
