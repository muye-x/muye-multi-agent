"""
主 Agent 编排器
"""
import sys
import asyncio
import uuid

# Windows 平台需要设置事件循环策略（支持 psycopg 异步）
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from typing import Dict, Any, Optional, List, Annotated
from collections.abc import Callable
from pydantic import SecretStr
from typing_extensions import TypedDict

from langchain.agents import create_agent
from muye_multi_agent_sdk.integrations.muye_llm import MuyeLlmChatModel
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.message import add_messages
from core.prompts import get_system_prompt
from config import get_config
from middleware import (
    LoopDetectionMiddleware,
    ToolErrorHandlingMiddleware,
    ClarificationMiddleware,
    LLMErrorHandlingMiddleware,
    WorkingMessagesMiddleware,
    PerformanceProfilingMiddleware
)
import logging
from tools.sub_agent.registry import SubAgentRegistry

logger = logging.getLogger(__name__)


# 定义自定义状态模式（包含 user_id 和 session_id）
class AgentState(TypedDict):
    """Agent 状态模式"""
    messages: Annotated[list, add_messages]  # 完整历史（持久化到 checkpointer）
    working_messages: list#Annotated[list, add_messages]  # 工作消息（传给 LLM，不持久化）
    user_id: str
    session_id: str
    actual_query:str
    user_location: dict  # 用户地理位置 {"lat": float, "lng": float}
    uploaded_files: list  # 存储上传的文件信息
    enable_knowledge: bool  # 是否启用知识检索（默认 False）


class MainAgentOrchestrator:
    """主 Agent 编排器（支持多轮对话）"""

    def __init__(
        self,
        llm_config: Optional[Dict[str, Any]] = None,
        checkpointer: BaseCheckpointSaver | None = None,
        enable_middleware: bool = True,
        enable_memory: bool = True,
        sub_agent_registry: SubAgentRegistry | None = None,
        sub_agent_runtime_guard: object | None = None,
        sub_agent_token_provider: Callable[[str], str | SecretStr | None] | None = None,
        sub_agent_citation_recorder: object | None = None,
    ):
        """
        初始化主 Agent

        Args:
            llm_config: LLM 配置（可选，使用全局配置）
            checkpointer: 自定义 checkpointer（可选，使用全局配置）
            enable_middleware: 是否启用中间件（默认 True）
            enable_memory: 是否启用记忆功能（默认 True）
        """
        config = get_config()

        # 初始化 LLM
        llm_config = llm_config or {}
        self.llm = MuyeLlmChatModel(
            base_url=str(llm_config.get('base_url', config.llm.api_base)),
            model_name=str(llm_config.get('model', config.llm.model)),
            temperature=float(llm_config.get('temperature', config.llm.temperature)),
            max_tokens=int(llm_config.get('max_tokens', config.llm.max_tokens)),
            timeout=float(llm_config.get('timeout', 60)),
        )
        logger.info("LLM 初始化完成: 通过 muye-llm model=%s", self.llm.model_name)

        # 加载工具（延迟导入避免循环依赖）
        from core.tools import get_all_tools
        self.sub_agent_registry = sub_agent_registry or SubAgentRegistry([])
        self.tools = get_all_tools(
            self.sub_agent_registry,
            runtime_guard=sub_agent_runtime_guard,
            token_provider=sub_agent_token_provider,
            citation_recorder=sub_agent_citation_recorder,
        )
        logger.info(f"工具加载完成，共 {len(self.tools)} 个工具")

        # Checkpointer 由 AgentManager 的应用生命周期创建和关闭，避免隐式全局连接。
        if checkpointer is None:
            raise ValueError("MainAgentOrchestrator 需要由 AgentManager 注入 checkpointer")
        self.checkpointer = checkpointer
        logger.info(f"Checkpointer 初始化完成: {type(self.checkpointer).__name__}")

        # 初始化中间件
        self.enable_middleware = enable_middleware
        self.enable_memory = enable_memory
        self.middlewares = self._create_middlewares() if enable_middleware else []
        if self.middlewares:
            logger.info(f"中间件初始化完成，共 {len(self.middlewares)} 个中间件")
            for mw in self.middlewares:
                logger.info(f"  - {type(mw).__name__}")

        # 创建 Agent
        self.agent = self._create_agent()
        logger.info("主 Agent 初始化完成")

    def _create_middlewares(self) -> List:
        """按运行阶段创建当前实际启用的中间件。"""
        config = get_config()
        middlewares = []

        if config.profiling.enabled:
            middlewares.append(PerformanceProfilingMiddleware())
            logger.info("已启用性能统计中间件")

        middlewares.append(
            LLMErrorHandlingMiddleware(
                max_retries=config.middleware.llm_max_retries,
                base_delay=config.middleware.llm_base_delay,
                max_delay=config.middleware.llm_max_delay
            )
        )

        from middleware import MessageCompressionMiddleware
        middlewares.append(MessageCompressionMiddleware(llm=self.llm))

        if self.enable_memory:
            from middleware.memory import MemoryMiddleware
            middlewares.append(
                MemoryMiddleware(
                    inject_memory=config.memory.injection_enabled,
                    auto_save=True,
                )
            )

        from middleware import ChartFormatInjectorMiddleware
        middlewares.append(ChartFormatInjectorMiddleware(config={"enabled": True}))

        middlewares.append(WorkingMessagesMiddleware())

        task_mode = config.task_decomposition.mode
        if task_mode == 'todolist':
            try:
                from langchain.agents.middleware import TodoListMiddleware
                middlewares.append(TodoListMiddleware())
                logger.info("已启用 TodoListMiddleware（LangChain 自带）")
            except ImportError:
                logger.warning("无法导入 TodoListMiddleware，请升级 langchain 版本")

        middlewares.append(ClarificationMiddleware())
        middlewares.append(
            LoopDetectionMiddleware(
                warn_threshold=config.middleware.loop_warn_threshold,
                hard_limit=config.middleware.loop_hard_limit
            )
        )
        middlewares.append(ToolErrorHandlingMiddleware())
        return middlewares

    def _create_agent(self):
        """
        创建 Agent 实例

        Returns:
            Agent 实例
        """
        config = get_config()

        agent_params = {
            'model': self.llm,
            'tools': self.tools,
            'checkpointer': self.checkpointer,
            'system_prompt': get_system_prompt(self.sub_agent_registry),
            'state_schema': AgentState,  # 使用自定义状态模式
        }

        # 如果启用了中间件，添加到参数中
        if self.middlewares:
            agent_params['middleware'] = self.middlewares

        return create_agent(**agent_params)

    async def ainvoke(
        self,
        user_input: str,
        session_id: str = "default_session",
        user_id: str = "default_user",
        user_location: Optional[Dict[str, float]] = None,
        additional_kwargs: Optional[Dict[str, Any]] = None,
        enable_knowledge: bool = False,  # 新增参数，默认 False
        trace_id: str | None = None,
        catalog_revision: str = "",
        allowed_agent_ids: frozenset[str] = frozenset(),
        **kwargs
    ) -> Dict[str, Any]:
        """
        异步调用 Agent（支持多轮对话）

        Args:
            user_input: 用户输入
            session_id: 会话ID（用于多轮对话追踪）
            user_id: 用户ID
            user_location: 用户地理位置 {"lat": float, "lng": float}
            additional_kwargs: 额外的消息参数（如文件信息）
            **kwargs: 其他参数

        Returns:
            Agent 执行结果
        """
        logger.info("=" * 80)
        logger.info(f"[MainAgent] 收到用户请求")
        logger.info(f"  会话ID: {session_id}")
        logger.info(f"  用户ID: {user_id}")
        logger.info(f"  用户输入: {user_input}")
        if user_location:
            logger.info(f"  用户位置: lat={user_location.get('lat')}, lng={user_location.get('lng')}")
        logger.info("=" * 80)

        # 构建配置，使用 session_id 作为 thread_id
        trace_id = trace_id or f"main_{uuid.uuid4().hex}"
        config = {
            "configurable": {
                "ori_query": user_input,
                "thread_id": f"{user_id}:{session_id}",
                "user_id": user_id,
                "trace_id": trace_id,
                "catalog_revision": catalog_revision,
                "allowed_agent_ids": tuple(sorted(allowed_agent_ids)),
                "user_location": user_location or {}  # 添加到 configurable 中
            },
            "recursion_limit": 500  # 增加递归限制，默认 25 可能不够
        }

        # 调试日志：打印 config 内容
        logger.info("[DEBUG] 构建的 config:")
        logger.info(f"  - ori_query: {config['configurable'].get('ori_query')}")
        logger.info(f"  - thread_id: {config['configurable'].get('thread_id')}")
        logger.info(f"  - user_id: {config['configurable'].get('user_id')}")
        logger.info(f"  - user_location: {config['configurable'].get('user_location')}")

        # 构建用户消息
        from langchain_core.messages import HumanMessage
        user_message = HumanMessage(
            content=user_input,
            additional_kwargs=additional_kwargs or {}
        )

        # 调用 Agent（将 user_id、session_id、actual_query 和 user_location 放入 state 中）
        logger.info("[MainAgent] 开始执行 Agent 工作流...")
        result = await self.agent.ainvoke(
            {
                "messages": [user_message],
                "user_id": user_id,
                "session_id": session_id,
                "user_location": user_location or {},
                "enable_knowledge": enable_knowledge  # 新增字段
            },
            config=config
        )

        # 分析执行结果
        messages = result.get("messages", [])
        logger.info(f"[MainAgent] Agent 执行完成 - 共生成 {len(messages)} 条消息")

        # 记录工具调用情况
        tool_calls = []
        for msg in messages:
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    tool_calls.append({
                        'name': tool_call.get('name', 'unknown'),
                        'args': tool_call.get('args', {})
                    })

        if tool_calls:
            logger.info(f"[MainAgent] 工具调用记录 - 共调用 {len(tool_calls)} 次工具:")
            for i, tc in enumerate(tool_calls, 1):
                logger.info(f"  {i}. 工具: {tc['name']}")
                logger.info(f"     参数: {tc['args']}")

        # 记录最终回复 - 提取所有 AI 回复内容
        if messages:
            ai_responses = []
            for msg in messages:
                if hasattr(msg, 'type') and msg.type == 'ai':
                    if hasattr(msg, 'content') and msg.content and msg.content.strip():
                        ai_responses.append(msg.content)

            if ai_responses:
                logger.info(f"[MainAgent] AI 回复段数: {len(ai_responses)}")
                for i, response in enumerate(ai_responses, 1):
                    preview = response[:200] + "..." if len(response) > 200 else response
                    logger.info(f"[MainAgent] 回复 #{i}: {preview}")

        logger.info("=" * 80)
        return result

    def invoke(
        self,
        user_input: str,
        session_id: str = "default_session",
        user_id: str = "default_user",
        **kwargs
    ) -> Dict[str, Any]:
        """
        同步调用 Agent（兼容旧代码）

        Args:
            user_input: 用户输入
            session_id: 会话ID
            user_id: 用户ID
            **kwargs: 其他参数

        Returns:
            Agent 执行结果
        """
        import asyncio

        loop = asyncio.get_event_loop()
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        return loop.run_until_complete(
            self.ainvoke(user_input, session_id, user_id, **kwargs)
        )

    async def get_conversation_history(
        self,
        session_id: str,
        user_id: str = "default_user",
    ) -> List[Dict[str, Any]]:
        """
        获取会话历史

        Args:
            session_id: 会话ID

        Returns:
            消息历史列表
        """
        config = {"configurable": {"thread_id": f"{user_id}:{session_id}"}}

        try:
            state = await self.agent.aget_state(config)
            messages = state.values.get("messages", [])

            # 转换为简单格式
            history = []
            for msg in messages:
                if hasattr(msg, 'content'):
                    role = "assistant" if msg.type == "ai" else msg.type
                    history.append({
                        "role": role,
                        "content": msg.content
                    })

            return history

        except Exception as e:
            logger.warning(f"获取会话历史失败: {e}")
            return []

    async def clear_conversation(self, session_id: str, user_id: str = "default_user") -> bool:
        """
        清除会话历史

        Args:
            session_id: 会话ID

        Returns:
            是否成功
        """
        try:
            # MemorySaver 支持直接删除
            if hasattr(self.checkpointer, 'storage'):
                keys_to_delete = [
                    k for k in self.checkpointer.storage.keys()
                    if f"{user_id}:{session_id}" in str(k)
                ]
                for key in keys_to_delete:
                    del self.checkpointer.storage[key]

            logger.info(f"会话 {session_id} 历史已清除")
            return True

        except Exception as e:
            logger.error(f"清除会话历史失败: {e}")
            return False
