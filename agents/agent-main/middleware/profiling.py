"""性能统计中间件 - 自动记录各阶段耗时"""

import logging
from typing import Optional, Dict, Any

from .base import AgentMiddleware
from utils.profiler import RequestProfiler

logger = logging.getLogger(__name__)


class PerformanceProfilingMiddleware(AgentMiddleware):
    """性能统计中间件

    自动在中间件管道的各个钩子点记录耗时：
    - abefore_model / aafter_model: 记录每次 LLM 调用耗时
    - awrap_tool_call: 记录每次工具执行耗时
    """

    def __init__(self):
        super().__init__()
        self._llm_call_count = 0
        self._tool_call_count = 0

    async def abefore_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        """重置计数器"""
        self._llm_call_count = 0
        self._tool_call_count = 0
        return None

    async def abefore_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """LLM 调用前 - 开始计时"""
        self._llm_call_count += 1
        profiler = RequestProfiler.get_current()
        if profiler:
            profiler.start(f"llm_call #{self._llm_call_count}")
        return None

    async def aafter_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        """LLM 调用后 - 停止计时"""
        profiler = RequestProfiler.get_current()
        if profiler and self._llm_call_count > 0:
            profiler.stop(f"llm_call #{self._llm_call_count}")
        return None

    async def awrap_tool_call(self, request, handler):
        """工具调用 - 记录执行耗时"""
        self._tool_call_count += 1
        tool_name = getattr(request, 'name', f'tool_{self._tool_call_count}')
        tag = f"tool_{tool_name}"

        profiler = RequestProfiler.get_current()
        if profiler:
            profiler.start(tag)

        try:
            result = await handler(request)
            return result
        finally:
            if profiler:
                profiler.stop(tag)
