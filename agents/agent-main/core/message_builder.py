"""
消息构建器中间件
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum

from langchain_core.messages import HumanMessage

from .message_rules import MessageRules

logger = logging.getLogger(__name__)


class InjectionLevel(Enum):
    """时间注入级别"""
    NONE = "none"           # 不注入
    MEDIUM = "medium"       # 轻提示
    STRONG = "strong"       # 强提示


class MessageBuilder:
    """消息构建器 - 负责处理用户输入并构建消息对象"""

    def __init__(self):
        self.rules = MessageRules()

    def build_user_message(
        self,
        user_input: str,
        additional_kwargs: Optional[Dict[str, Any]] = None,
        user_informations: Optional[Dict[str, Any]] = None
    ) -> HumanMessage:
        """
        构建用户消息对象

        Args:
            user_input: 用户原始输入
            additional_kwargs: 额外的消息参数（如文件信息）
            user_informations: 用户自定义信息（如 {"name": "小助手", "style": "专业"}）

        Returns:
            处理后的 HumanMessage 对象
        """
        # 判断是否需要注入时间
        injection_level = self._should_inject_time(user_input)

        # 根据注入级别处理消息
        processed_input = self._process_input(user_input, injection_level)

        # 将调用方提供的用户信息注入消息上下文。
        if user_informations:
            processed_input = self._inject_user_informations(processed_input, user_informations)
            logger.info(f"消息注入用户自定义信息: {user_informations}")

        # 构建消息对象
        message = HumanMessage(
            content=processed_input,
            additional_kwargs=additional_kwargs or {}
        )

        # 记录日志
        if injection_level != InjectionLevel.NONE:
            logger.info(f"消息注入时间信息 - 级别: {injection_level.value}")

        return message

    def _should_inject_time(self, user_input: str) -> InjectionLevel:
        """
        判断是否需要注入时间信息

        Args:
            user_input: 用户输入

        Returns:
            注入级别
        """
        # 1. 检查排除关键词（优先级最高）
        for keyword in self.rules.get_exclude_keywords():
            if keyword in user_input:
                logger.debug(f"检测到排除关键词: {keyword}，不注入时间")
                return InjectionLevel.NONE

        # 2. 检查强时效性关键词
        for keyword in self.rules.get_strong_keywords():
            if keyword in user_input:
                logger.debug(f"检测到强时效性关键词: {keyword}，使用强提示")
                return InjectionLevel.STRONG

        # 3. 检查中等时效性关键词
        for keyword in self.rules.get_medium_keywords():
            if keyword in user_input:
                logger.debug(f"检测到中等时效性关键词: {keyword}，使用轻提示")
                return InjectionLevel.MEDIUM

        # 4. 默认不注入
        return InjectionLevel.NONE

    def _process_input(self, user_input: str, injection_level: InjectionLevel) -> str:
        """
        根据注入级别处理用户输入

        Args:
            user_input: 原始用户输入
            injection_level: 注入级别

        Returns:
            处理后的输入文本
        """
        if injection_level == InjectionLevel.NONE:
            return user_input

        # 获取当前日期
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 根据级别选择模板
        if injection_level == InjectionLevel.STRONG:
            template = self.rules.get_strong_template()
        else:  # 中等注入级别
            template = self.rules.get_medium_template()

        # 格式化模板
        processed = template.format(date=current_date, user_input=user_input)

        return processed

    def _inject_user_informations(self, user_input: str, user_informations: Dict[str, Any]) -> str:
        """
        注入用户自定义信息到用户输入前

        Args:
            user_input: 用户输入
            user_informations: 用户自定义信息字典

        Returns:
            注入信息后的输入
        """
        # 构建身份提示段落
        identity_parts = []

        # 处理 AI 名称
        if "name" in user_informations and user_informations["name"]:
            ai_name = user_informations["name"]
            identity_parts.append(f'AI助手的名字是"{ai_name}"，当用户询问"你是谁"、"你叫什么名字"时，回答引用"我是{ai_name}，很高兴为您服务！"')

        # 回复风格字段保留给后续扩展。
        if "style" in user_informations and user_informations["style"]:
            style = user_informations["style"]
            identity_parts.append(f'你的回复风格是：{style}')

        # 如果没有任何自定义信息，直接返回原输入
        if not identity_parts:
            return user_input

        # 拼接身份提示
        identity_prompt = "\n".join(identity_parts)

        # 注入到用户输入前
        final_input = f"""
【重要提示】
{identity_prompt}

---

{user_input}
"""
        return final_input

    def build_simple_message(self, content: str) -> HumanMessage:
        """
        构建简单消息（不进行时间注入判断）

        Args:
            content: 消息内容

        Returns:
            HumanMessage 对象
        """
        return HumanMessage(content=content)
