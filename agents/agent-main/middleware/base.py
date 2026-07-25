from typing import Optional, Dict, Any

from langchain.agents.middleware import AgentMiddleware as BaseAgentMiddleware

'''
  ┌────────────────┬──────────────┬────────────────────┬────────────────────────┐
  │      钩子       │   执行时机    │      主要用途        │       返回值影响         │
  ├────────────────┼──────────────┼────────────────────┼────────────────────────┤
  │ before_agent   │ Agent 开始前  │ 全局上下文注入        │ 注入消息到开头           │
  ├────────────────┼──────────────┼────────────────────┼────────────────────────┤
  │ before_model   │ LLM 调用前    │ 动态提示注入         │ 注入消息到当前位置         │
  ├────────────────┼──────────────┼────────────────────┼────────────────────────┤
  │ after_model    │ LLM 响应后    │ 循环检测、输出检查     │ 可修改/清空 tool_calls   │
  ├────────────────┼──────────────┼────────────────────┼────────────────────────┤
  │ wrap_tool_call │ 工具执行时     │ 工具拦截、参数修改    │ 拦截或放行工具            │
  ├────────────────┼──────────────┼────────────────────┼────────────────────────┤
  │ after_agent    │ Agent 完成后  │ 清理、日志           │ 添加最终消息             │
  └────────────────┴──────────────┴────────────────────┴────────────────────────┘
'''


# ===== 定义中间件基类=====
class AgentMiddleware(BaseAgentMiddleware):
    """中间件基类 - 包含同步和异步钩子"""

    # ===== 同步钩子 =====
    def before_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None

    def before_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None

    def after_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None

    def wrap_model_call(self, request, handler) -> Any:
        """包装模型调用（同步）"""
        return handler(request)

    def wrap_tool_call(self, request, handler) -> Any:
        return handler(request)

    def after_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None

    # ===== 异步钩子 =====
    async def abefore_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None

    async def abefore_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None

    async def aafter_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None

    async def awrap_model_call(self, request, handler) -> Any:
        """包装模型调用（异步）"""
        return await handler(request)

    async def awrap_tool_call(self, request, handler) -> Any:
        return await handler(request)

    async def aafter_agent(self, state, runtime) -> Optional[Dict[str, Any]]:
        return None