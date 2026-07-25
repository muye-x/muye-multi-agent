"""
记忆提取器
使用 LLM 从对话中提取结构化记忆
"""
import json
import logging
from typing import List, Dict, Any, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from config import get_config
from .prompts import MEMORY_EXTRACTION_PROMPT, format_conversation_for_extraction

logger = logging.getLogger(__name__)


class MemoryExtractor:
    """
    记忆提取器
    使用 LLM 分析对话并提取结构化记忆更新
    """

    def __init__(self, model_name: Optional[str] = None):
        """
        初始化记忆提取器

        Args:
            model_name: 可选的模型名称，如果不提供则使用配置中的默认模型
        """
        config = get_config().memory
        self.model_name = model_name or config.extraction_model
        self.temperature = config.extraction_temperature
        self.llm: Optional[ChatOpenAI] = None

    def _get_llm(self) -> ChatOpenAI:
        """
        获取 LLM 实例（懒加载）

        Returns:
            ChatOpenAI: LLM 实例
        """
        if self.llm is None:
            # 获取 LLM 配置
            llm_config = get_config().llm

            self.llm = ChatOpenAI(
                model=self.model_name,
                temperature=self.temperature,
                openai_api_key=llm_config.api_key,
                openai_api_base=llm_config.api_base,
            )
            logger.debug(f"LLM 已初始化: {self.model_name}")
        return self.llm

    async def extract_memories(
        self,
        messages: List[Any],
        current_memory: Dict[str, Any],
        correction_detected: bool = False,
        reinforcement_detected: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        从对话中提取记忆更新（采用 deer-flow 的提取逻辑）

        Args:
            messages: 消息列表
            current_memory: 当前的记忆状态（deer-flow 格式）
            correction_detected: 是否检测到纠正信号
            reinforcement_detected: 是否检测到强化信号

        Returns:
            Optional[Dict]: 提取的更新内容，格式：
                {
                    "user": {
                        "workContext": {"summary": "...", "shouldUpdate": true/false},
                        "personalContext": {"summary": "...", "shouldUpdate": true/false},
                        "topOfMind": {"summary": "...", "shouldUpdate": true/false}
                    },
                    "history": {
                        "recentMonths": {"summary": "...", "shouldUpdate": true/false},
                        "earlierContext": {"summary": "...", "shouldUpdate": true/false},
                        "longTermBackground": {"summary": "...", "shouldUpdate": true/false}
                    },
                    "newFacts": [
                        {"content": "...", "category": "...", "confidence": 0.9, "sourceError": "..."}
                    ],
                    "factsToRemove": ["fact_id_1", "fact_id_2"]
                }
        """
        try:
            logger.info(f"记忆提取输入：{messages}")
            # 格式化对话
            conversation_text = format_conversation_for_extraction(messages)

            if not conversation_text.strip():
                logger.warning("对话内容为空，跳过提取")
                return None

            # 构建 correction_hint
            correction_hint = ""
            if correction_detected:
                correction_hint = (
                    "⚠️ 检测到纠正信号：用户明确纠正了 Agent 的理解或输出。"
                    "将正确方法记录为高置信度事实（≥0.95），类别为 'correction'。"
                    "在 'sourceError' 字段中包含出错原因。"
                )
            if reinforcement_detected:
                reinforcement_hint = (
                    "✅ 检测到强化信号：用户明确确认了 Agent 的方法或输出。"
                    "将验证的方法/偏好记录为高置信度事实（≥0.9），使用合适的类别。"
                )
                correction_hint = (correction_hint + "\n" + reinforcement_hint).strip() if correction_hint else reinforcement_hint

            prompt = MEMORY_EXTRACTION_PROMPT.format(
                current_memory=json.dumps(current_memory, ensure_ascii=False, indent=2),
                conversation=conversation_text,
                correction_hint=correction_hint or "无特殊信号检测到。"
            )

            # 调用 LLM
            llm = self._get_llm()
            response = await llm.ainvoke([HumanMessage(content=prompt)])

            # 提取响应内容
            response_text = response.content.strip()

            # 移除 markdown 代码块标记（如果有）
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                # 移除第一行（```json）和最后一行（```）
                if lines[-1].strip() == "```":
                    response_text = "\n".join(lines[1:-1])
                else:
                    response_text = "\n".join(lines[1:])

            # 解析 JSON
            update_data = json.loads(response_text)

            logger.info("记忆提取成功")
            logger.debug(f"提取结果: {json.dumps(update_data, ensure_ascii=False, indent=2)}")

            return update_data

        except json.JSONDecodeError as e:
            logger.error(f"解析 LLM 响应失败: {e}")
            logger.debug(f"原始响应: {response_text if 'response_text' in locals() else 'N/A'}")
            return None

        except Exception as e:
            logger.error(f"记忆提取失败: {e}", exc_info=True)
            return None

    def extract_memories_sync(
        self,
        messages: List[Any],
        current_memory: Dict[str, Any],
        correction_detected: bool = False,
        reinforcement_detected: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        同步版本的记忆提取（用于非异步环境）

        Args:
            messages: 消息列表
            current_memory: 当前的记忆状态
            correction_detected: 是否检测到纠正信号
            reinforcement_detected: 是否检测到强化信号

        Returns:
            Optional[Dict]: 提取的更新内容，失败时返回 None
        """
        try:
            # 格式化对话
            conversation_text = format_conversation_for_extraction(messages)

            if not conversation_text.strip():
                logger.warning("对话内容为空，跳过提取")
                return None

            # 构建提示词
            correction_hint = ""
            if correction_detected:
                correction_hint = (
                    "⚠️ **检测到纠正信号**：用户明确指出了之前的错误或不当之处。"
                    "请特别关注用户纠正的内容，将正确的做法记录为 category='correction' 的事实，"
                    "并设置较高的置信度（≥0.95）。"
                )
            if reinforcement_detected:
                reinforcement_hint = (
                    "✅ **检测到强化信号**：用户明确肯定了之前的做法或回复。"
                    "请记录用户认可的方法、风格或偏好，将其记录为 category='preference' 或 'behavior' 的事实，"
                    "并设置较高的置信度（≥0.9）。"
                )
                correction_hint = (correction_hint + "\n" + reinforcement_hint).strip() if correction_hint else reinforcement_hint

            prompt = MEMORY_EXTRACTION_PROMPT.format(
                current_memory=json.dumps(current_memory, ensure_ascii=False, indent=2),
                conversation=conversation_text,
                correction_hint=correction_hint or "无特殊信号。"
            )

            # 调用 LLM（同步）
            llm = self._get_llm()
            response = llm.invoke([HumanMessage(content=prompt)])

            # 提取响应内容
            response_text = response.content.strip()

            # 移除 markdown 代码块标记（如果有）
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                if lines[-1].strip() == "```":
                    response_text = "\n".join(lines[1:-1])
                else:
                    response_text = "\n".join(lines[1:])

            # 解析 JSON
            update_data = json.loads(response_text)

            logger.info("记忆提取成功（同步）")
            logger.debug(f"提取结果: {json.dumps(update_data, ensure_ascii=False, indent=2)}")

            return update_data

        except json.JSONDecodeError as e:
            logger.error(f"解析 LLM 响应失败: {e}")
            logger.debug(f"原始响应: {response_text if 'response_text' in locals() else 'N/A'}")
            return None

        except Exception as e:
            logger.error(f"记忆提取失败: {e}", exc_info=True)
            return None
