"""
Working Messages 中间件
在 LLM 调用时使用 working_messages 替代 messages
"""
import logging
from typing import Any, Callable, Awaitable

from .base import AgentMiddleware

logger = logging.getLogger(__name__)


class WorkingMessagesMiddleware(AgentMiddleware):
    """
    Working Messages 中间件

    功能：
    1. 在 awrap_model_call 层面拦截 LLM 调用
    2. 如果 state 中有 working_messages，使用它替代 messages 传递给 LLM
    3. LLM 的响应仍然追加到 messages（持久化）
    """

    async def abefore_model(self, state, runtime):
        """
        在模型调用前检查 working_messages 是否存在
        如果不存在，则使用完整的 messages
        """
        if not state.get("working_messages"):
            logger.debug("[WorkingMessagesMiddleware] working_messages 不存在，使用完整 messages")
            # 不返回任何更新，让其他中间件处理
        return None

    async def awrap_model_call(
        self,
        request: Any,  # LangChain 的 ModelRequest
        handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """
        包装模型调用，使用 working_messages 替代 messages

        Args:
            request: 模型请求（包含 state）
            handler: 原始处理函数

        Returns:
            模型调用结果
        """
        # 从 request 中获取 state
        state = request.state

        # 检查是否有 working_messages
        working_messages = state.get("working_messages")

        logger.info(f"[WorkingMessagesMiddleware] state 中的 working_messages: {len(working_messages) if working_messages else 'None'}")
        logger.info(f"[WorkingMessagesMiddleware] state 中的 messages: {len(state.get('messages', []))}")

        if working_messages:
            # 备份原始 messages
            original_messages = state.get("messages", [])

            logger.debug(f"[WorkingMessagesMiddleware] 使用 working_messages 调用 LLM: {len(working_messages)} 条（原始 {len(original_messages)} 条）")

            # 修复消息序列，确保 tool_calls 配对完整。
            working_messages = self._fix_tool_calls_pairing(working_messages)
            logger.debug(f"[WorkingMessagesMiddleware] 修复后 working_messages: {len(working_messages)} 条")

            request = request.override(messages=working_messages)

            try:
                # 调用 LLM
                result = await handler(request)

                # 恢复原始 messages（但保留 LLM 的响应）
                # LLM 的响应已经通过 add_messages reducer 追加到 state["messages"] 中
                # 我们需要提取新增的消息
                current_messages = state.get("messages", [])

                # 找到新增的消息（在 working_messages 之后的）
                new_messages = current_messages[len(working_messages):]

                # 恢复原始 messages 并追加新消息
                state["messages"] = original_messages + new_messages

                # 清空 working_messages，避免下次调用时重复使用
                state["working_messages"] = []

                logger.debug(f"[WorkingMessagesMiddleware] LLM 调用完成，新增 {len(new_messages)} 条消息，恢复后 {len(state['messages'])} 条")

                return result

            except Exception as e:
                # 出错时恢复原始 messages（通过返回状态更新）
                logger.error(f"[WorkingMessagesMiddleware] LLM 调用失败: {e}")
                # 注意：这里仍需直接修改 state，因为异常时不会应用返回值
                state["messages"] = original_messages
                state["working_messages"] = []
                raise
        else:
            # 没有 working_messages，直接调用
            logger.debug(f"[WorkingMessagesMiddleware] 没有 working_messages，使用原始 messages")
            return await handler(request)

    def _fix_tool_calls_pairing(self, messages: list) -> list:
        """
        修复消息序列，确保最终传给 LLM 的 tool_calls 配对完整

        规则：
        1. AIMessage(tool_calls) 必须紧跟对应的 ToolMessage
        2. 如果中间插入了其他消息（system/human），将其移到配对之后
        3. 如果配对不完整，丢弃不完整的 AIMessage 和部分 ToolMessage
        4. 如果遇到孤立 ToolMessage，直接丢弃

        Args:
            messages: 原始消息列表

        Returns:
            修复后的消息列表
        """
        if not messages:
            return messages

        fixed = []
        i = 0

        while i < len(messages):
            msg = messages[i]
            msg_type = getattr(msg, 'type', None)

            # ToolMessage 只能作为当前 AIMessage(tool_calls) 组的一部分被保留。
            # 顶层遇到 tool 说明它的前置 tool_calls 已经被截断/压缩/过滤掉，必须丢弃。
            if msg_type == 'tool':
                logger.warning("[WorkingMessagesMiddleware] 检测到孤立 ToolMessage，已丢弃")
                i += 1
                continue

            # 检查是否是带 tool_calls 的 AIMessage
            if msg_type == 'ai':
                tool_calls = getattr(msg, 'tool_calls', None) or []

                if tool_calls:
                    # 收集应该跟随的 tool_call_ids
                    expected_ids = {tc.get('id') for tc in tool_calls if isinstance(tc, dict) and 'id' in tc}

                    if not expected_ids:
                        logger.warning(
                            "[WorkingMessagesMiddleware] AIMessage 包含 tool_calls 但缺少有效 id，已丢弃"
                        )
                        i += 1
                        continue

                    # 查找后续的 ToolMessage（跳过中间的干扰消息）
                    tool_messages = []
                    skipped_messages = []  # 被跳过的 system/human 消息
                    j = i + 1

                    while j < len(messages) and expected_ids:
                        next_msg = messages[j]

                        # 找到 ToolMessage
                        if hasattr(next_msg, 'type') and next_msg.type == 'tool':
                            tool_call_id = getattr(next_msg, 'tool_call_id', None)
                            if tool_call_id in expected_ids:
                                tool_messages.append(next_msg)
                                expected_ids.discard(tool_call_id)
                            else:
                                logger.warning(
                                    "[WorkingMessagesMiddleware] 检测到未知 tool_call_id 的 ToolMessage，已丢弃"
                                )

                        # 遇到插入的 system/human 消息
                        elif hasattr(next_msg, 'type') and next_msg.type in ('system', 'human'):
                            skipped_messages.append(next_msg)
                            logger.info(f"[WorkingMessagesMiddleware] 检测到插入的 {next_msg.type} 消息，将移到 tool_calls 配对之后")

                        # 遇到新的 AI 消息，停止查找
                        elif hasattr(next_msg, 'type') and next_msg.type == 'ai':
                            break

                        j += 1

                    # 检查配对是否完整
                    if expected_ids:
                        # 配对不完整：丢弃这个 AIMessage 和找到的部分 ToolMessage
                        logger.warning(
                            f"[WorkingMessagesMiddleware] AIMessage 的 tool_calls 配对不完整，"
                            f"缺失 {len(expected_ids)} 个响应，丢弃整组消息"
                        )
                        # 保留被跳过的消息（可能是重要的警告）
                        fixed.extend(skipped_messages)
                        i = j
                    else:
                        # 配对完整：保留 AIMessage + 所有 ToolMessage + 被跳过的消息
                        fixed.append(msg)
                        fixed.extend(tool_messages)
                        fixed.extend(skipped_messages)
                        i = j
                else:
                    # 普通 AIMessage（无 tool_calls）
                    fixed.append(msg)
                    i += 1
            else:
                # 非 AI 消息
                fixed.append(msg)
                i += 1

        if len(fixed) != len(messages):
            logger.info(f"[WorkingMessagesMiddleware] 修复消息配对: {len(messages)} 条 -> {len(fixed)} 条")

        return fixed
