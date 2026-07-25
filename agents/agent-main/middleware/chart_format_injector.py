"""
图表格式动态注入中间件

根据用户查询动态注入需要的图表格式说明
"""
from typing import Dict, Any, Optional
import logging

from middleware.base import AgentMiddleware
from core.chart_formats import detect_needed_charts, build_chart_format_prompt

logger = logging.getLogger(__name__)


class ChartFormatInjectorMiddleware(AgentMiddleware):
    """图表格式动态注入中间件"""

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__()
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)

    async def abefore_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """
        在调用 LLM 前智能检测并注入图表格式

        Args:
            state: Agent 状态（包含 messages）
            runtime: 运行时信息

        Returns:
            修改后的状态字典（如果需要注入）
        """
        if not self.enabled:
            return None

        try:
            # 获取 working_messages（如果存在）或 messages
            working_messages = state.get("working_messages") or state.get("messages", [])

            if not working_messages:
                return None

            # 过滤掉旧的图表格式注入消息
            filtered_messages = [
                msg for msg in working_messages
                if not (hasattr(msg, 'additional_kwargs') and
                       msg.additional_kwargs.get("_is_chart_format_injection"))
            ]

            # 如果过滤掉了消息，记录日志
            if len(filtered_messages) < len(working_messages):
                logger.debug(f"已过滤掉 {len(working_messages) - len(filtered_messages)} 条旧的图表格式注入")

            # 获取最后一条用户消息（从过滤后的消息中获取）
            user_message = None
            for msg in reversed(filtered_messages):
                if hasattr(msg, 'type') and msg.type == 'human':
                    user_message = msg.content
                    break
                elif isinstance(msg, dict) and msg.get('role') == 'user':
                    user_message = msg.get('content', '')
                    break

            if not user_message:
                return None

            # 智能检测需要的图表（基于当前用户输入）
            from core.chart_formats import detect_needed_charts_smart, build_basic_chart_prompt, build_chart_format_prompt

            chart_info = detect_needed_charts_smart(user_message)

            # 如果不需要图表，返回过滤后的消息列表（清理旧注入）
            if not chart_info["need_charts"]:
                logger.debug("未检测到图表需求")
                # 如果过滤掉了旧注入，返回过滤后的 working_messages
                if len(filtered_messages) < len(working_messages):
                    logger.info("清理旧的图表格式注入")
                    return {"working_messages": filtered_messages}
                return None

            logger.info(f"检测到图表需求 - 基础: {chart_info['basic']}, 高级: {chart_info['advanced']}")

            # 构建注入内容（基于当前需求）
            injected_content = """**🚨 图表格式强制规则**：
1. labels 必须是数组格式：["标签1", "标签2", "标签3"]，禁止使用字符串 "标签1,标签2,标签3"
2. data 必须是数组格式：[100, 200, 300]，禁止使用字符串 "100,200,300"
3. 所有表格类数据只输出标准 Markdown 表格，严禁 HTML 标签

**错误示例**：
<chart>
{
  "type": "pie",
  "labels": "品牌A,品牌B,品牌C",  ❌ 错误！
  "data": "35,25,20"  ❌ 错误！
}
</chart>

**正确示例**：
<chart>
{
  "type": "pie",
  "labels": ["品牌A", "品牌B", "品牌C"],  ✅ 正确
  "data": [35, 25, 20]  ✅ 正确
}
</chart>
"""

            # 注入基础图表
            if chart_info["basic"]:
                injected_content += build_basic_chart_prompt(chart_info["basic"])

            # 注入高级图表
            if chart_info["advanced"]:
                injected_content += build_chart_format_prompt(chart_info["advanced"])

            if not injected_content:
                # 如果过滤掉了旧注入，返回过滤后的 working_messages
                if len(filtered_messages) < len(working_messages):
                    return {"working_messages": filtered_messages}
                return None

            # 注入到消息列表（智能避免破坏 tool_calls 配对）
            from langchain_core.messages import SystemMessage

            # 找到安全的插入位置
            insert_index = self._find_safe_insert_position(filtered_messages)

            # 创建带标记的图表格式消息
            chart_format_message = SystemMessage(
                content=injected_content,
                additional_kwargs={"_is_chart_format_injection": True}
            )

            # 创建新的消息列表
            new_working_messages = filtered_messages[:insert_index] + [
                chart_format_message
            ] + filtered_messages[insert_index:]

            logger.info(f"已注入新的图表格式说明到 working_messages（带标记）")

            # 返回修改后的 working_messages（不影响持久化的 messages）
            return {"working_messages": new_working_messages}

        except Exception as e:
            logger.error(f"图表格式注入失败: {e}", exc_info=True)
            return None

    def _find_safe_insert_position(self, messages: list) -> int:
        """
        找到安全的插入位置，避免破坏 tool_calls 和 tool 消息的配对

        规则：
        1. 如果最后一条是 tool 消息，检查前一条是否为 ai(tool_calls)
        2. 如果是配对关系，则在 ai(tool_calls) 之前插入
        3. 否则在最后一条用户消息之前插入

        Args:
            messages: 消息列表

        Returns:
            安全的插入位置索引
        """
        if len(messages) < 2:
            return max(0, len(messages) - 1)

        # 检查最后两条消息
        last_msg = messages[-1]
        second_last = messages[-2]

        # 场景1：最后是 tool 消息，倒数第二是 ai(tool_calls)
        if (hasattr(last_msg, 'type') and last_msg.type == 'tool' and
            hasattr(second_last, 'type') and second_last.type == 'ai' and
            hasattr(second_last, 'tool_calls') and second_last.tool_calls):

            # 在 ai(tool_calls) 之前插入
            insert_pos = len(messages) - 2
            logger.debug(f"[ChartFormatInjector] 检测到 tool_calls 配对，插入位置调整到索引 {insert_pos}")
            return insert_pos

        # 场景2：最后是 ai(tool_calls)（工具还未执行）
        if (hasattr(last_msg, 'type') and last_msg.type == 'ai' and
            hasattr(last_msg, 'tool_calls') and last_msg.tool_calls):

            # 在 ai(tool_calls) 之前插入
            insert_pos = len(messages) - 1
            logger.debug(f"[ChartFormatInjector] 检测到待执行的 tool_calls，插入位置调整到索引 {insert_pos}")
            return insert_pos

        # 默认：在最后一条消息之前插入
        return len(messages) - 1
