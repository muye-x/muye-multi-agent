"""
主 Agent 工具注册中心
"""
from collections.abc import Callable
from typing import List
from langchain_core.tools import BaseTool
from pydantic import SecretStr
from tools.registry import get_web_tools
import logging

logger = logging.getLogger(__name__)


def get_all_subgraph_tools(
    registry=None,
    *,
    runtime_guard=None,
    token_provider: Callable[[str], str | SecretStr | None] | None = None,
    citation_recorder=None,
) -> List[BaseTool]:
    """
    获取所有子图工作流工具

    Returns:
        工具列表
    """
    from config import get_config
    config = get_config()

    from tools.sub_agent import build_default_registry, build_sub_agent_tools
    selected_registry = registry or build_default_registry()
    tools = build_sub_agent_tools(
        selected_registry,
        runtime_guard=runtime_guard,
        token_provider=token_provider,
        citation_recorder=citation_recorder,
    )

    logger.info(f"已注册 {len(tools)} 个子图工作流工具")
    return tools


def get_auxiliary_tools() -> List[BaseTool]:
    """
    获取辅助工具（非子图工作流）

    Returns:
        辅助工具列表
    """
    from langchain_core.tools import tool
    @tool
    async def ask_clarification(
        question: str,
        clarification_type: str = "missing_info",
        context: str = None,
        options: list = None
    ) -> str:
        """向用户请求澄清信息

        Args:
            question: 要问用户的问题
            clarification_type: 澄清类型 (missing_info/ambiguous_requirement/approach_choice/risk_confirmation/suggestion)
            context: 问题的背景信息
            options: 可选的选项列表

        Returns:
            用户的回答
        """
        # 这个函数实际不会被执行，会被 ClarificationMiddleware 拦截
        return "等待用户回答..."

    return [ask_clarification]


def get_all_tools(
    sub_agent_registry=None,
    *,
    runtime_guard=None,
    token_provider: Callable[[str], str | SecretStr | None] | None = None,
    citation_recorder=None,
) -> List[BaseTool]:
    """
    获取所有工具（子图工作流 + 辅助工具 + 网络工具 + 外部 API 工具）

    Returns:
        完整工具列表
    """
    logger.info("=" * 60)
    logger.info("[工具注册] 开始加载所有工具...")

    subgraph_tools = get_all_subgraph_tools(
        sub_agent_registry,
        runtime_guard=runtime_guard,
        token_provider=token_provider,
        citation_recorder=citation_recorder,
    )
    logger.info(f"[工具注册] 子图工作流工具: {len(subgraph_tools)} 个")
    for tool in subgraph_tools:
        logger.info(f"  - {tool.name}: {tool.description[:50]}...")

    auxiliary_tools = get_auxiliary_tools()
    logger.info(f"[工具注册] 辅助工具: {len(auxiliary_tools)} 个")
    for tool in auxiliary_tools:
        logger.info(f"  - {tool.name}: {tool.description[:50]}...")

    web_tools = get_web_tools()
    logger.info(f"[工具注册] 网络工具: {len(web_tools)} 个")
    for tool in web_tools:
        logger.info(f"  - {tool.name}: {tool.description[:50]}...")
    all_tools = subgraph_tools + auxiliary_tools + web_tools
    logger.info(f"[工具注册] 工具加载完成，共 {len(all_tools)} 个工具")
    logger.info("=" * 60)

    return all_tools
