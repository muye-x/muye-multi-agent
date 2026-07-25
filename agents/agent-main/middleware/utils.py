"""
中间件工具函数
"""
import logging
from typing import Optional, List, Tuple
from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


async def get_or_create_llm(runtime):
    """
    从 runtime 获取 LLM，如果不存在则创建

    Args:
        runtime: 运行时对象

    Returns:
        LLM 实例
    """
    llm = getattr(runtime, 'llm', None)

    if llm is None:
        from config import get_config
        from langchain_openai import ChatOpenAI
        config = get_config()
        llm = ChatOpenAI(
            model=config.llm.model,
            temperature=config.llm.temperature,
            openai_api_base=config.llm.api_base,
            openai_api_key=config.llm.api_key,
        )
        logger.info("从配置创建 LLM 实例")

    return llm


def split_system_messages(messages: List[BaseMessage]) -> Tuple[List[BaseMessage], List[BaseMessage]]:
    """
    分离系统消息和对话消息

    Args:
        messages: 消息列表

    Returns:
        (系统消息列表, 对话消息列表)
    """
    system_messages = []
    conversation_messages = []

    for m in messages:
        if hasattr(m, 'type') and m.type == 'system':
            system_messages.append(m)
        else:
            conversation_messages.append(m)

    return system_messages, conversation_messages
