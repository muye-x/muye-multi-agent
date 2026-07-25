"""
信号检测器
负责检测用户消息中的纠正和强化信号
用于指导记忆提取过程，提高记忆质量
"""
import re
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class SignalDetector:
    """用户反馈信号检测器"""

    # 纠正信号的正则表达式模式（中文 + 英文）
    CORRECTION_PATTERNS = [
        # 英文纠正模式
        re.compile(r"\bthat(?:'s| is) (?:wrong|incorrect)\b", re.IGNORECASE),
        re.compile(r"\byou misunderstood\b", re.IGNORECASE),
        re.compile(r"\btry again\b", re.IGNORECASE),
        re.compile(r"\bredo\b", re.IGNORECASE),
        re.compile(r"\bnot what i (?:meant|wanted)\b", re.IGNORECASE),
        re.compile(r"\bno[,.]?\s+(?:that's|that is) not (?:right|correct)\b", re.IGNORECASE),

        # 中文纠正模式
        re.compile(r"不对"),
        re.compile(r"不是这样"),
        re.compile(r"你理解错了"),
        re.compile(r"你理解有误"),
        re.compile(r"重试"),
        re.compile(r"重新来"),
        re.compile(r"换一种"),
        re.compile(r"改用"),
        re.compile(r"不是我想要的"),
        re.compile(r"错误"),
        re.compile(r"有问题"),
    ]

    # 强化信号的正则表达式模式（中文 + 英文）
    REINFORCEMENT_PATTERNS = [
        # 英文强化模式
        re.compile(r"\byes[,.]?\s+(?:exactly|perfect|that(?:'s| is) (?:right|correct|it))\b", re.IGNORECASE),
        re.compile(r"\bperfect(?:[.!?]|$)", re.IGNORECASE),
        re.compile(r"\bexactly\s+(?:right|correct)\b", re.IGNORECASE),
        re.compile(r"\bthat(?:'s| is)\s+(?:exactly\s+)?(?:right|correct|what i (?:wanted|needed|meant))\b", re.IGNORECASE),
        re.compile(r"\bkeep\s+(?:doing\s+)?that\b", re.IGNORECASE),
        re.compile(r"\bjust\s+(?:like\s+)?(?:that|this)\b", re.IGNORECASE),
        re.compile(r"\bthis is (?:great|helpful|good)\b(?:[.!?]|$)", re.IGNORECASE),
        re.compile(r"\bthis is what i wanted\b(?:[.!?]|$)", re.IGNORECASE),

        # 中文强化模式
        re.compile(r"对[，,]?\s*就是这样(?:[。！？!?.]|$)"),
        re.compile(r"完全正确(?:[。！？!?.]|$)"),
        re.compile(r"(?:对[，,]?\s*)?就是这个意思(?:[。！？!?.]|$)"),
        re.compile(r"正是我想要的(?:[。！？!?.]|$)"),
        re.compile(r"继续保持(?:[。！？!?.]|$)"),
        re.compile(r"很好"),
        re.compile(r"不错"),
        re.compile(r"非常好"),
        re.compile(r"太棒了"),
    ]

    @staticmethod
    def _extract_message_text(message: Any) -> str:
        """
        从消息对象中提取纯文本内容

        Args:
            message: 消息对象（可能是字典或对象）

        Returns:
            str: 提取的文本内容
        """
        # 如果是字典
        if isinstance(message, dict):
            content = message.get("content", "")
        # 如果是对象
        else:
            content = getattr(message, "content", "")

        # 处理列表类型的内容（多模态消息）
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict):
                    text_val = part.get("text")
                    if isinstance(text_val, str):
                        text_parts.append(text_val)
            return " ".join(text_parts)

        return str(content)

    @classmethod
    def detect_correction(cls, messages: List[Any]) -> bool:
        """
        检测最近的消息中是否包含纠正信号

        Args:
            messages: 消息列表

        Returns:
            bool: 是否检测到纠正信号
        """
        # 只检查最近的 6 条用户消息
        recent_user_msgs = []
        for msg in reversed(messages):
            msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
            msg_role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)

            if msg_type == "human" or msg_role == "user":
                recent_user_msgs.append(msg)
                if len(recent_user_msgs) >= 6:
                    break

        # 检查每条消息
        for msg in recent_user_msgs:
            content = cls._extract_message_text(msg).strip()
            if not content:
                continue

            # 检查是否匹配任何纠正模式
            for pattern in cls.CORRECTION_PATTERNS:
                if pattern.search(content):
                    logger.info(f"检测到纠正信号: {content[:50]}...")
                    return True

        return False

    @classmethod
    def detect_reinforcement(cls, messages: List[Any]) -> bool:
        """
        检测最近的消息中是否包含强化信号

        Args:
            messages: 消息列表

        Returns:
            bool: 是否检测到强化信号
        """
        # 只检查最近的 6 条用户消息
        recent_user_msgs = []
        for msg in reversed(messages):
            msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
            msg_role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)

            if msg_type == "human" or msg_role == "user":
                recent_user_msgs.append(msg)
                if len(recent_user_msgs) >= 6:
                    break

        # 检查每条消息
        for msg in recent_user_msgs:
            content = cls._extract_message_text(msg).strip()
            if not content:
                continue

            # 检查是否匹配任何强化模式
            for pattern in cls.REINFORCEMENT_PATTERNS:
                if pattern.search(content):
                    logger.info(f"检测到强化信号: {content[:50]}...")
                    return True

        return False

    @classmethod
    def extract_correction_context(cls, messages: List[Any]) -> Optional[str]:
        """
        提取纠正信号的上下文（用户纠正 + 之前的错误回复）

        Args:
            messages: 消息列表

        Returns:
            Optional[str]: 纠正上下文描述，如果没有纠正信号则返回 None
        """
        # 找到最近的纠正消息
        correction_msg = None
        for msg in reversed(messages):
            msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
            msg_role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)

            if msg_type == "human" or msg_role == "user":
                content = cls._extract_message_text(msg).strip()
                for pattern in cls.CORRECTION_PATTERNS:
                    if pattern.search(content):
                        correction_msg = msg
                        break
                if correction_msg:
                    break

        if not correction_msg:
            return None

        # 找到纠正消息之前的 AI 回复
        found_correction = False
        previous_ai_msg = None
        for msg in reversed(messages):
            if msg == correction_msg:
                found_correction = True
                continue

            if found_correction:
                msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
                msg_role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)

                if msg_type == "ai" or msg_role == "assistant":
                    previous_ai_msg = msg
                    break

        if not previous_ai_msg:
            return None

        # 构造上下文描述
        correction_content = cls._extract_message_text(correction_msg)
        previous_content = cls._extract_message_text(previous_ai_msg)

        context = f"用户纠正: {correction_content[:100]}\n之前的错误回复: {previous_content[:100]}"
        return context

    @classmethod
    def analyze_signals(cls, messages: List[Any]) -> Dict[str, Any]:
        """
        综合分析消息中的所有信号

        Args:
            messages: 消息列表

        Returns:
            Dict: 信号分析结果
        """
        has_correction = cls.detect_correction(messages)
        has_reinforcement = cls.detect_reinforcement(messages)
        correction_context = None

        if has_correction:
            correction_context = cls.extract_correction_context(messages)

        return {
            "has_correction": has_correction,
            "has_reinforcement": has_reinforcement,
            "correction_context": correction_context,
            "signal_type": (
                "correction" if has_correction else
                "reinforcement" if has_reinforcement else
                "none"
            )
        }
