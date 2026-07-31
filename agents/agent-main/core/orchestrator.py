"""
Agent 管理器 - 单例模式管理 Agent 实例
"""
import asyncio
import logging
import uuid
from contextlib import AsyncExitStack
from typing import Optional, Dict, Any

from pydantic import SecretStr

from core import MainAgentOrchestrator
from core.message_builder import MessageBuilder
from tools.sub_agent.tools import SUB_AGENT_TOOL_TAG
from tools.sub_agent.catalog import (
    AuthorizedCatalogView,
    CatalogProvider,
    CatalogUnavailableError,
    HttpControlPlaneClient,
)
from tools.sub_agent.registry import SubAgentRegistry
from tools.sub_agent.runtime import SubAgentRuntimeGuard
from tools.sub_agent.tools import build_sub_agent_tools
from utils.profiler import RequestProfiler
from muye_multi_agent_sdk import AgentRequest, ContextConfig
from muye_multi_agent_sdk.runtime import CheckpointerManager, ExecutionManager, ExecutionOptions, SessionAcquireTimeoutError
logger = logging.getLogger(__name__)

STREAM_LOCK_WAIT_TIMEOUT_SECONDS = 60.0
STREAM_IDLE_TIMEOUT_SECONDS = 60.0
STREAM_MAX_HOLD_TIMEOUT_SECONDS = 300.0


def _is_sub_agent_tool_event(event: dict[str, Any]) -> bool:
    """判断 LangChain 工具事件是否已有 custom 子 Agent 生命周期。"""
    tags = event.get("tags")
    return isinstance(tags, (list, tuple, set)) and SUB_AGENT_TOOL_TAG in tags


class AgentManager:
    """Agent 管理器 - 单例模式"""

    _instance: Optional['AgentManager'] = None
    _lock = asyncio.Lock()

    def __init__(self):
        self.agent: Optional[MainAgentOrchestrator] = None
        self.checkpointer = None
        self.checkpointer_manager: CheckpointerManager | None = None
        self.message_builder = MessageBuilder()
        self.initialized = False
        self.execution_manager = ExecutionManager()
        self.catalog_provider: CatalogProvider | None = None
        self.sub_agent_runtime_guard: SubAgentRuntimeGuard | None = None
        self._authorized_agents: dict[tuple[str, tuple[str, ...]], MainAgentOrchestrator] = {}
        self._agent_token_provider = None
        self._citation_recorder = None
        self._memory_enabled = False

    @classmethod
    async def get_instance(cls) -> 'AgentManager':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance.initialize()
        return cls._instance

    async def initialize(self):
        """根据运行配置初始化 Agent 和其持久化依赖。"""
        if not self.initialized:
            from config import get_config

            config = get_config()
            self._memory_enabled = config.memory.enabled
            logger.info("初始化 MainAgent（记忆功能：%s）...", "启用" if config.memory.enabled else "关闭")

            self.checkpointer_manager = CheckpointerManager(self._checkpointer_config())
            self.checkpointer = await self.checkpointer_manager.get(enabled=True)
            if self.checkpointer is None:
                raise RuntimeError("CheckpointerManager 未返回 checkpointer")

            catalog_config = config.catalog
            control_client = None
            if catalog_config.enabled:
                control_client = HttpControlPlaneClient(
                    base_url=catalog_config.control_base_url,
                    service_token=SecretStr(catalog_config.main_service_token),
                    timeout_seconds=catalog_config.control_timeout_seconds,
                )
            self.catalog_provider = CatalogProvider(control_client, poll_seconds=catalog_config.poll_seconds)
            await self.catalog_provider.start()
            self.sub_agent_runtime_guard = SubAgentRuntimeGuard(
                failure_threshold=catalog_config.circuit_failure_threshold,
                recovery_seconds=catalog_config.circuit_recovery_seconds,
            )
            self._agent_token_provider = catalog_config.agent_token
            if control_client is not None:
                self._citation_recorder = control_client.record_citation

            # 空 Catalog 也必须健康启动；授权视图按请求创建并缓存对应 graph。
            self.agent = MainAgentOrchestrator(
                enable_memory=config.memory.enabled,
                checkpointer=self.checkpointer,
                sub_agent_registry=SubAgentRegistry([]),
                sub_agent_runtime_guard=self.sub_agent_runtime_guard,
                sub_agent_token_provider=self._agent_token_provider,
                sub_agent_citation_recorder=self._citation_recorder,
            )
            self.initialized = True
            logger.info("MainAgent 初始化完成")

    async def cleanup(self):
        """清理资源"""
        if self.catalog_provider is not None:
            await self.catalog_provider.close()
            self.catalog_provider = None
        self._authorized_agents.clear()
        if self.checkpointer_manager is not None:
            await self.checkpointer_manager.close()
            self.checkpointer_manager = None
            self.checkpointer = None
            logger.info("Checkpointer 已关闭")

    @staticmethod
    def _checkpointer_config() -> ContextConfig:
        """将主服务既有环境配置映射为 SDK checkpointer 生命周期配置。"""
        from config import get_config

        source = get_config().checkpointer
        backend = source.backend.strip().lower()
        if backend not in {"memory", "sqlite", "postgres"}:
            raise ValueError(f"不支持的 CHECKPOINTER_BACKEND: {source.backend!r}")
        return ContextConfig(
            enabled_profiles={"internal"},
            backend=backend,
            sqlite_path=source.sqlite_path,
            postgres_uri=source.postgresql_uri or None,
            postgres_pool_min_size=source.postgres_pool_size,
            postgres_pool_max_size=source.postgres_pool_size + source.postgres_max_overflow,
            postgres_pool_timeout_seconds=source.postgres_pool_timeout,
            postgres_pool_max_lifetime_seconds=source.postgres_pool_recycle,
        )

    @staticmethod
    def _execution_request(user_id: str, session_id: str) -> AgentRequest:
        """构造仅供 SDK 并发控制使用的可信请求上下文。

        主服务原先按 session_id 互斥，不区分用户；因此显式覆盖 SDK 默认锁键以
        保持既有会话隔离范围。
        """
        return AgentRequest(task="main agent execution", context={"user_id": user_id, "session_id": session_id})

    async def chat(
        self,
        user_input: str,
        user_id: str,
        session_id: str,
        files: Optional[list] = None,
        user_location: Optional[Dict[str, Any]] = None,
        enable_knowledge: bool = False,
        user_informations: Optional[Dict[str, Any]] = None,
        trusted_user_id: str | None = None,
    ) -> Dict[str, Any]:
        """执行对话（非流式）"""
        if not self.initialized:
            raise RuntimeError("Agent 未初始化")

        # 记录当前请求的必要上下文。
        logger.info(f"[{session_id}] 收到非流式对话请求")
        logger.info(f"[{session_id}]   user_id: {user_id}")
        logger.info(f"[{session_id}]   user_input: {user_input[:200]}{'...' if len(user_input) > 200 else ''}")
        logger.info(f"[{session_id}]   files: {len(files) if files else 0} 个")
        logger.info(f"[{session_id}]   user_location: {user_location}")
        logger.info(f"[{session_id}]   enable_knowledge: {enable_knowledge}")
        if user_informations:
            logger.info(f"[{session_id}]   user_informations: {user_informations}")

        # 初始化性能追踪器。
        from config import get_config
        import time
        config = get_config()
        trace_id = f"chat_{uuid.uuid4().hex}"
        profiler = None
        if config.profiling.enabled:
            profiler = RequestProfiler(request_id=f"chat_{int(time.time() * 1000)}", session_id=session_id)
            profiler.activate()

        if profiler:
            profiler.start("session_lock_wait")
        options = ExecutionOptions(execution_key=f"main:{user_id}:{session_id}", wait_for_session=True)
        async with self.execution_manager.acquire("agent-main", self._execution_request(user_id, session_id), options):
            if profiler:
                profiler.stop("session_lock_wait")
            logger.info(f"[{session_id}] 获取 session 锁（非流式）")

            try:
                request_agent, catalog_view = await self._agent_for_request(trusted_user_id)
                # 构建消息，如果有文件则注入到 additional_kwargs
                additional_kwargs = {}
                if files:
                    additional_kwargs["files"] = files
                    logger.info(f"注入文件信息: {len(files)} 个文件")

                user_message = self.message_builder.build_user_message(
                    user_input=user_input,
                    additional_kwargs=additional_kwargs,
                    user_informations=user_informations,
                )

                result = await request_agent.ainvoke(
                    user_input=user_message.content,
                    user_id=user_id,
                    session_id=session_id,
                    user_location=user_location,
                    additional_kwargs=additional_kwargs,
                    enable_knowledge=enable_knowledge,
                    trace_id=trace_id,
                    catalog_revision=catalog_view.catalog_revision,
                    allowed_agent_ids=catalog_view.allowed_agent_ids,
                )
                return result

            finally:
                logger.info(f"[{session_id}] 释放 session 锁（非流式）")

                # 输出性能报告。
                if profiler:
                    profiler.log_summary(slow_threshold_ms=config.profiling.slow_threshold_ms)
                    profiler.deactivate()


    async def chat_stream(
        self,
        user_input: str,
        user_id: str,
        session_id: str,
        files: Optional[list] = None,
        user_location: Optional[Dict[str, Any]] = None,
        enable_knowledge: bool = False,
        user_informations: Optional[Dict[str, Any]] = None,
        trusted_user_id: str | None = None,
    ):
        """执行对话（流式输出，使用 Block Stream V2 协议）"""
        if not self.initialized:
            raise RuntimeError("Agent 未初始化")

        # 记录当前请求的必要上下文。
        logger.info(f"[{session_id}] 收到流式对话请求")
        logger.info(f"[{session_id}]   user_id: {user_id}")
        logger.info(f"[{session_id}]   user_input: {user_input[:200]}{'...' if len(user_input) > 200 else ''}")
        logger.info(f"[{session_id}]   files: {len(files) if files else 0} 个")
        logger.info(f"[{session_id}]   user_location: {user_location}")
        logger.info(f"[{session_id}]   enable_knowledge: {enable_knowledge}")
        if user_informations:
            logger.info(f"[{session_id}]   user_informations: {user_informations}")

        # 在锁外完成初始化，避免阻塞 HTTP 响应。
        from api.stream_protocol import BlockType, EventNormalizer
        from config import get_config
        import time
        trace_id = f"stream_{uuid.uuid4().hex}"

        # 初始化性能追踪器。
        config = get_config()
        profiler = None
        if config.profiling.enabled:
            profiler = RequestProfiler(request_id=f"stream_{int(time.time() * 1000)}", session_id=session_id)
            profiler.activate()

        additional_kwargs = {}
        if files:
            additional_kwargs["files"] = files

        user_message = self.message_builder.build_user_message(
            user_input=user_input,
            additional_kwargs=additional_kwargs,
            user_informations=user_informations,
        )
        request_agent, catalog_view = await self._agent_for_request(trusted_user_id)

        normalizer = EventNormalizer(
            session_id=session_id,
            stream_id=f"stream_{int(time.time() * 1000)}",
            user_id=user_id
        )
        # 获取配置
        config = get_config()

        # 记录工具调用时间
        tool_start_times = {}
        tool_counter = 0
        model_block_ids: dict[str, str] = {}

        # 先发送 session_start，让客户端确认连接已建立。
        yield normalizer.session_start(model=config.llm.model).to_sse()

        execution_options = ExecutionOptions(
            execution_key=f"main:{user_id}:{session_id}",
            wait_timeout_seconds=STREAM_LOCK_WAIT_TIMEOUT_SECONDS,
            idle_timeout_seconds=STREAM_IDLE_TIMEOUT_SECONDS,
            max_hold_timeout_seconds=STREAM_MAX_HOLD_TIMEOUT_SECONDS,
        )
        execution_stack = AsyncExitStack()
        try:
            if profiler:
                profiler.start("session_lock_wait")
            active_run = await execution_stack.enter_async_context(
                self.execution_manager.acquire(
                    "agent-main",
                    self._execution_request(user_id, session_id),
                    execution_options,
                )
            )
            if profiler:
                profiler.stop("session_lock_wait")

            logger.info(f"[{session_id}] 获取 SDK session 执行权（流式，带 watchdog）")

            try:
                # 使用 astream_events 获取流式输出
                logger.info(f"[{session_id}] astream_events() 开始")
                async for event in request_agent.agent.astream_events(
                            {
                                "messages": [user_message],
                                "user_id": user_id,
                                "session_id": session_id,
                                "actual_query": user_input,
                                "user_location": user_location or {},
                                "enable_knowledge": enable_knowledge,
                            },
                            config={
                                "configurable": {
                                    "thread_id": f"{user_id}:{session_id}",
                                    "user_id": user_id,
                                    "trace_id": trace_id,
                                    "catalog_revision": catalog_view.catalog_revision,
                                    "allowed_agent_ids": tuple(sorted(catalog_view.allowed_agent_ids)),
                                    "ori_query": user_input,  # 传递原始用户输入（未注入时间的）
                                    "user_location": user_location or {}
                                },
                                "recursion_limit": 500
                            },
                            version="v2",
                            stream_mode=["values", "custom"]  # 启用 custom 模式以捕获 StreamWriter 事件
                        ):
                            # 每个 LangGraph 事件均视为一次活跃心跳。
                            active_run.touch()

                            event_type = event["event"]

                            # 1. AI 消息流：逐个透传模型的原始文本增量，避免按 Block 缓冲。
                            if event_type == "on_chat_model_stream":
                                # 检查 tags，过滤内部视觉模型的流式输出
                                event_tags = event.get("tags", [])
                                if "skip_stream" in event_tags or "vision_internal" in event_tags:
                                    # 跳过内部视觉模型的输出，不发送到前端
                                    continue

                                chunk = event["data"]["chunk"]
                                content = getattr(chunk, "content", None)
                                if isinstance(content, str) and content:
                                    model_run_id = str(event.get("run_id") or "default")
                                    block_id = model_block_ids.setdefault(
                                        model_run_id,
                                        f"b{len(model_block_ids) + 1}",
                                    )
                                    yield normalizer.block_delta(
                                        block_id, BlockType.MARKDOWN, content
                                    ).to_sse()

                            # 2. 工具调用开始
                            elif event_type == "on_tool_start":
                                if _is_sub_agent_tool_event(event):
                                    continue
                                tool_name = event.get("name", "unknown")
                                tool_input = event["data"].get("input", {})

                                # StreamWriter 工具跳过默认处理（它们会自己发射 start 事件和计算耗时）
                                tool_counter += 1
                                tool_id = f"tool_{tool_counter}"
                                tool_start_times[tool_name] = {
                                    "time": time.time(),
                                    "id": tool_id
                                }

                                # 使用 Block Stream V2 协议
                                logger.info(f"工具调用开始: {tool_name}, ID: {tool_id}, 输入: {tool_input}")
                                tool_start_event = normalizer.tool_start(tool_id, tool_name, tool_input)
                                yield tool_start_event.to_sse()

                            # 3. 工具调用完成
                            elif event_type == "on_tool_end":
                                if _is_sub_agent_tool_event(event):
                                    continue
                                tool_name = event.get("name", "unknown")
                                tool_output = event["data"].get("output", "")

                                logger.debug("on_tool_end 事件字段: %s", list(event.keys()))
                                logger.debug("on_tool_end tags: %s", event.get("tags"))
                                logger.debug("on_tool_end metadata: %s", event.get("metadata"))

                                # 计算耗时
                                duration_ms = None
                                tool_id = None
                                if tool_name in tool_start_times:
                                    tool_info = tool_start_times[tool_name]
                                    duration_ms = int((time.time() - tool_info["time"]) * 1000)
                                    tool_id = tool_info["id"]
                                    del tool_start_times[tool_name]

                                if tool_id:
                                    logger.info(f"工具调用完成: {tool_name}, ID: {tool_id}, 耗时: {duration_ms}ms")
                                    tool_complete_event = normalizer.tool_complete(tool_id, tool_name, duration_ms)
                                    yield tool_complete_event.to_sse()

                            # 4. 工具调用失败
                            elif event_type == "on_tool_error":
                                if _is_sub_agent_tool_event(event):
                                    continue
                                tool_name = event.get("name", "unknown")
                                error = str(event["data"].get("error", "未知错误"))

                                tool_id = None
                                if tool_name in tool_start_times:
                                    tool_id = tool_start_times[tool_name]["id"]
                                    del tool_start_times[tool_name]

                                if tool_id:
                                    yield normalizer.tool_error(
                                        tool_id, tool_name, "TOOL_ERROR", error
                                    ).to_sse()

                            # 5. 自定义工具进度事件
                            elif event_type == "on_chain_stream":
                                try:
                                    chunk = event.get("data", {}).get("chunk")

                                    # 检查是否是自定义事件（tuple 格式）
                                    if isinstance(chunk, tuple) and len(chunk) == 2:
                                        event_name, custom_data = chunk

                                        logger.info(f"[Orchestrator] 收到自定义事件: {event_name}")
                                        logger.debug(f"[Orchestrator] 自定义事件数据: {custom_data}")

                                        # 处理工具进度事件
                                        if event_name == "custom":
                                            tool_id = custom_data.get("tool_id")
                                            tool_name = custom_data.get("tool_name")
                                            status = custom_data.get("status")
                                            log = custom_data.get("log")
                                            progress = custom_data.get("progress")

                                            logger.info(f"[Orchestrator] 工具进度: {tool_name} - {status} - {log} ({progress}%)")

                                            # 根据状态转发不同的事件
                                            if status == "start":
                                                # 工具开始事件
                                                tool_input = custom_data.get("input", {})

                                                logger.info(f"[Orchestrator] 转发工具开始事件: {tool_name}, ID: {tool_id}")
                                                yield normalizer.tool_start(tool_id, tool_name, tool_input).to_sse()

                                            elif status == "running":
                                                # 转发进度事件
                                                yield normalizer.tool_running(
                                                    tool_id, tool_name, log, progress
                                                ).to_sse()

                                            elif status == "result":
                                                blocks = custom_data.get("blocks", [])
                                                if isinstance(blocks, list):
                                                    yield normalizer.tool_result(
                                                        tool_id, tool_name, blocks
                                                    ).to_sse()

                                            elif status == "complete":
                                                # 转发完成事件
                                                # 使用工具自己计算的 duration_ms
                                                duration_ms = custom_data.get("duration_ms", 0)
                                                logger.info(f"[Orchestrator] 转发工具完成事件: {tool_name}, ID: {tool_id}, 耗时: {duration_ms}ms")
                                                yield normalizer.tool_complete(
                                                    tool_id, tool_name, duration_ms
                                                ).to_sse()

                                            elif status == "error":
                                                # 转发错误事件
                                                yield normalizer.tool_error(
                                                    tool_id, tool_name, "TOOL_ERROR", log
                                                ).to_sse()
                                    else:
                                        # 不是自定义事件，忽略
                                        logger.debug(f"忽略 on_chain_stream 事件（非自定义事件）")

                                except Exception as e:
                                    logger.error(f"[Orchestrator] 处理 on_chain_stream 事件失败: {e}", exc_info=True)

                            # 8. 其他事件类型（记录日志但不处理）
                            elif event_type == "on_chat_model_end":
                                # LLM 输出完成
                                pass

                            elif event_type in ["on_chain_end","on_chat_model_start",
                                               "on_retriever_start", "on_retriever_end"]:
                                # 这些事件用于内部流程控制，不需要发送给前端
                                logger.debug(f"忽略事件: {event_type}")
                                pass

                            else:
                                # 未知事件类型，记录警告
                                logger.warning(f"未处理的事件类型: {event_type}, 数据: {event.get('data', {})}")

                logger.info(f"[{session_id}] astream_events() 完成")

                # 发送完成与会话结束事件
                active_run.touch()
                yield normalizer.done().to_sse()
                yield normalizer.session_end().to_sse()

            except asyncio.CancelledError:
                reason = active_run.watchdog_reason or "cancelled"
                logger.error(f"[{session_id}] 流式任务被取消，原因: {reason}")
                # 发送友好的错误提示给前端
                yield normalizer.error(
                    error_code="BUSY",
                    error_message="当前使用人数较多，Muye 正在忙，请稍后再试。",
                    error_details={"reason": "server_busy", "retry_after": 10}
                ).to_sse()
                # 发送 Done & Session End，让前端正常结束
                yield normalizer.done().to_sse()
                yield normalizer.session_end().to_sse()

            except Exception as e:
                logger.error(f"[{session_id}] 流式对话异常: {e}", exc_info=True)
                # 使用 Block Stream V2 错误事件
                yield normalizer.error(
                    error_code="STREAM_ERROR",
                    error_message=str(e),
                    error_details={"type": type(e).__name__}
                ).to_sse()
                # 异常情况下也发送 Done & Session End
                yield normalizer.done().to_sse()
                yield normalizer.session_end().to_sse()

            finally:
                await execution_stack.aclose()

                # 输出性能报告。
                if profiler:
                    profiler.log_summary(slow_threshold_ms=config.profiling.slow_threshold_ms)
                    profiler.deactivate()

        except SessionAcquireTimeoutError:
            logger.error(f"[{session_id}] 获取 session 锁超时（{STREAM_LOCK_WAIT_TIMEOUT_SECONDS:g}秒）")
            yield normalizer.error(
                error_code="LOCK_TIMEOUT",
                error_message="等待前一个请求完成超时，请稍后重试",
                error_details={"timeout_seconds": STREAM_LOCK_WAIT_TIMEOUT_SECONDS}
            ).to_sse()
            yield normalizer.done().to_sse()
            yield normalizer.session_end().to_sse()
            if profiler:
                profiler.log_summary(slow_threshold_ms=config.profiling.slow_threshold_ms)
                profiler.deactivate()

    async def _agent_for_request(self, trusted_user_id: str | None) -> tuple[MainAgentOrchestrator, AuthorizedCatalogView]:
        """构造请求级授权 graph；Control/授权失败时只返回零 SubAgent 的基础 graph。"""
        if self.agent is None:
            raise RuntimeError("Agent 未初始化")
        provider = self.catalog_provider
        if provider is None or trusted_user_id is None:
            snapshot = provider.snapshot if provider is not None else None
            revision = snapshot.catalog_revision if snapshot is not None else "catalog-unconfigured"
            checksum = snapshot.catalog_checksum if snapshot is not None else "0" * 64
            return self.agent, AuthorizedCatalogView(
                user_id=trusted_user_id or "",
                catalog_revision=revision,
                catalog_checksum=checksum,
                allowed_agent_ids=frozenset(),
                registry=SubAgentRegistry([]),
            )
        try:
            view = await provider.authorized_view(trusted_user_id)
        except CatalogUnavailableError:
            logger.exception("Catalog 或授权解析失败，本次请求不暴露任何 SubAgent")
            snapshot = provider.snapshot
            return self.agent, AuthorizedCatalogView(
                user_id=trusted_user_id,
                catalog_revision=snapshot.catalog_revision,
                catalog_checksum=snapshot.catalog_checksum,
                allowed_agent_ids=frozenset(),
                registry=SubAgentRegistry([]),
            )
        key = (view.catalog_checksum, tuple(sorted(view.allowed_agent_ids)))
        cached = self._authorized_agents.get(key)
        if cached is not None:
            return cached, view
        if len(self._authorized_agents) >= 64:
            self._authorized_agents.clear()
        runtime = MainAgentOrchestrator(
            enable_memory=self._memory_enabled,
            checkpointer=self.checkpointer,
            sub_agent_registry=view.registry,
            sub_agent_runtime_guard=self.sub_agent_runtime_guard,
            sub_agent_token_provider=self._agent_token_provider,
            sub_agent_citation_recorder=self._citation_recorder,
        )
        self._authorized_agents[key] = runtime
        return runtime, view

    async def smoke_sub_agent(self, *, agent_id: str, trusted_user_id: str) -> dict[str, str]:
        """经请求级 grant/Catalog 投影调用目标工具，供部署后确定性 smoke 使用。"""
        if not self.initialized or self.catalog_provider is None:
            raise RuntimeError("MainAgent Catalog 尚未初始化")
        view = await self.catalog_provider.authorized_view(trusted_user_id)
        if agent_id not in view.allowed_agent_ids:
            raise PermissionError("smoke 用户未获目标 Agent 授权")
        descriptor = view.registry.get_by_agent_id(agent_id)
        tools = build_sub_agent_tools(
            SubAgentRegistry([descriptor]),
            runtime_guard=self.sub_agent_runtime_guard,
            token_provider=self._agent_token_provider,
            citation_recorder=self._citation_recorder,
        )
        result = await tools[0].ainvoke(
            {"task": "执行部署后只读健康 smoke，并返回简短成功结果。"},
            config={
                "configurable": {
                    "user_id": trusted_user_id,
                    "session_id": f"deploy-smoke-{uuid.uuid4().hex}",
                    "trace_id": f"deploy_smoke_{uuid.uuid4().hex}",
                    "catalog_revision": view.catalog_revision,
                    "allowed_agent_ids": tuple(sorted(view.allowed_agent_ids)),
                }
            },
        )
        if result.startswith("["):
            raise RuntimeError("目标 Agent smoke 未通过")
        return {"agent_id": agent_id, "status": "passed"}
