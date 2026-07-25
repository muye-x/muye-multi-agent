"""
MultiLLMClient — LLM 客户端核心

功能：
- 指数退避重试
- <think> 标签内容自动剥离（Qwen3 CoT 输出兼容）
- 支持非流式（chat）和流式（chat_stream）两种调用模式
- 支持 JSON 模式解析（chat_json）
"""
import asyncio
import inspect
import json
import logging
import random
import time
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, AsyncIterator

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from config.settings import settings
from src.core.model_registry import ModelRegistry, ModelSelection, ModelSelectionError
from src.core.langsmith_tracing import LangSmithTracer
from src.utils.exceptions import InvalidRequestException
from src.utils.exceptions import LLMCallException

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMCallResult:
    """LLM 文本调用结果，仅保留响应与可观测性元数据。"""

    content: str
    model: str
    provider: str
    base_url: str
    finish_reason: str = ""
    latency_ms: int = 0
    model_alias: str = ""
    model_name: str = ""
    thinking_enabled: bool = False


@dataclass(slots=True)
class LLMStreamEvent:
    """流式调用事件：文本、结构化工具调用或流结束汇总三者之一。"""

    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    result: LLMCallResult | None = None


@dataclass(frozen=True, slots=True)
class LLMNodeConfig:
    """单个 OpenAI compatible 节点的内部配置。"""

    base_url: str
    api_key: str
    extra_body: dict[str, Any]


class ThinkTagFilter:
    """按任意分片边界隐藏 ``<think>...</think>`` 中的推理内容。

    模型流的标签可以被拆到多个 chunk；因此不能对每个 chunk 独立正则匹配。
    该状态机仅用于正常文本，工具调用 JSON 必须绕过它，避免改写参数内容。
    """

    _OPEN_TAG = "<think>"
    _CLOSE_TAG = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside_thinking = False

    @staticmethod
    def _partial_tag_suffix(text: str, tag: str) -> str:
        """返回可能属于下一个 chunk 标签前缀的最长后缀。"""
        max_length = min(len(text), len(tag) - 1)
        for length in range(max_length, 0, -1):
            if text.endswith(tag[:length]):
                return text[-length:]
        return ""

    def feed(self, content: str) -> str:
        """接收文本分片并返回当前可安全输出的可见内容。"""
        self._buffer += content
        visible_parts: list[str] = []

        while self._buffer:
            if self._inside_thinking:
                close_index = self._buffer.find(self._CLOSE_TAG)
                if close_index >= 0:
                    self._buffer = self._buffer[close_index + len(self._CLOSE_TAG):]
                    self._inside_thinking = False
                    continue
                self._buffer = self._partial_tag_suffix(self._buffer, self._CLOSE_TAG)
                break

            open_index = self._buffer.find(self._OPEN_TAG)
            if open_index >= 0:
                visible_parts.append(self._buffer[:open_index])
                self._buffer = self._buffer[open_index + len(self._OPEN_TAG):]
                self._inside_thinking = True
                continue

            suffix = self._partial_tag_suffix(self._buffer, self._OPEN_TAG)
            if suffix:
                visible_parts.append(self._buffer[:-len(suffix)])
                self._buffer = suffix
            else:
                visible_parts.append(self._buffer)
                self._buffer = ""
            break

        return "".join(visible_parts)

    def finalize(self) -> str:
        """结束流并返回剩余的普通文本，不输出未闭合思考块。"""
        if self._inside_thinking:
            self._buffer = ""
            return ""
        remaining = self._buffer
        self._buffer = ""
        return remaining


class ToolCallAccumulator:
    """兼容 OpenAI 流式工具调用 delta 的安全累积器。"""

    def __init__(self) -> None:
        self._tool_calls: dict[int, dict[str, Any]] = {}

    def add(self, delta: Any) -> None:
        """合并单个工具调用 delta，允许 function/name/arguments 分片缺失。"""
        index = int(getattr(delta, "index", 0) or 0)
        tool_call = self._tool_calls.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        tool_call_id = getattr(delta, "id", None)
        if tool_call_id:
            tool_call["id"] += str(tool_call_id)

        function = getattr(delta, "function", None)
        if function is None:
            return
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", None)
        if name:
            tool_call["function"]["name"] += str(name)
        if arguments:
            tool_call["function"]["arguments"] += str(arguments)

    def has_calls(self) -> bool:
        """是否已收到工具调用 delta。"""
        return bool(self._tool_calls)

    def to_list(self) -> list[dict[str, Any]]:
        """返回按上游 index 排序的 OpenAI-compatible 工具调用。"""
        return [self._tool_calls[index] for index in sorted(self._tool_calls)]


def _build_api_config() -> LLMNodeConfig:
    """从 settings 构建唯一的 Chat API 节点配置。"""
    return LLMNodeConfig(
        base_url=settings.llm_api_base_url,
        api_key=settings.llm_api_key,
        extra_body=dict(settings.llm_extra_body),
    )


def _strip_think_tags(text: str) -> str:
    """剥离完整文本中的思考块，保留正文原有空白。"""
    parser = ThinkTagFilter()
    return parser.feed(text) + parser.finalize()


class MultiLLMClient:
    """LLM 调用器，负责重试、流式协议和连接生命周期。

    公开 ``chat``、``chat_stream``、``embed`` 方法保持历史兼容；节点选择、重试、
    thinking 过滤及 SDK 资源管理均封装在本类内部。
    """

    def __init__(
        self,
        *,
        api_config: LLMNodeConfig | None = None,
        client: Any | None = None,
        embedding_client: Any | None = None,
        model_registry: ModelRegistry | None = None,
        tracer: LangSmithTracer | None = None,
    ) -> None:
        self._api_config = api_config or _build_api_config()
        self._client = client or AsyncOpenAI(
            api_key=self._api_config.api_key,
            base_url=self._api_config.base_url,
            timeout=settings.llm_timeout,
            max_retries=0,
        )
        self._embedding_client = embedding_client or AsyncOpenAI(
            api_key=settings.embed_api_key,
            base_url=settings.embed_api_base_url,
            timeout=settings.llm_timeout,
            max_retries=0,
        )
        self._model_registry = model_registry or ModelRegistry(
            settings.llm_models,
            default_model=settings.llm_default_model,
            default_thinking=settings.llm_default_thinking,
        )
        self._tracer = tracer or LangSmithTracer(
            enabled=settings.langsmith_enabled,
            api_key=settings.langsmith_api_key,
            project=settings.langsmith_project,
            endpoint=settings.langsmith_endpoint,
        )

    def _record_trace(
        self,
        *,
        trace_id: str,
        selection: ModelSelection,
        latency_ms: int,
        status: str,
        tool_count: int = 0,
    ) -> None:
        """提交脱敏 LangSmith 元数据；tracer 自身保证 fail-open。"""
        self._tracer.record(
            {
                "trace_id": trace_id,
                "model_alias": selection.id,
                "upstream_model": selection.provider_model,
                "thinking": selection.thinking_enabled,
                "latency_ms": latency_ms,
                "tool_count": tool_count,
                "status": status,
            }
        )

    @property
    def model_registry(self) -> ModelRegistry:
        """返回服务模型白名单，供只读模型列表接口使用。"""
        return self._model_registry

    def resolve_model(
        self,
        model: str | None = None,
        enable_thinking: bool | None = None,
    ) -> ModelSelection:
        """解析请求级模型配置，并将能力错误转换为 HTTP 400 领域异常。"""
        try:
            return self._model_registry.resolve(model, enable_thinking)
        except ModelSelectionError as exc:
            raise InvalidRequestException(str(exc)) from exc

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        """仅将网络、超时、限流和服务端错误纳入重试。"""
        if isinstance(exc, (APITimeoutError, asyncio.TimeoutError, APIConnectionError, RateLimitError)):
            return True
        return isinstance(exc, APIStatusError) and exc.status_code >= 500

    @staticmethod
    async def _close_resource(resource: Any) -> None:
        """兼容 SDK stream/client 的 close 或 aclose 方法。"""
        for method_name in ("aclose", "close"):
            method = getattr(resource, method_name, None)
            if method is None:
                continue
            result = method()
            if inspect.isawaitable(result):
                await result
            return

    async def aclose(self) -> None:
        """释放 Chat、Embedding 客户端持有的 HTTP 连接池，允许幂等调用。"""
        resources = [self._client, self._embedding_client]
        await asyncio.gather(*(self._close_resource(resource) for resource in resources), return_exceptions=True)

    def _build_chat_kwargs(
        self,
        config: LLMNodeConfig,
        selection: ModelSelection,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """构建非流式和流式共享的 OpenAI compatible 请求体。"""
        kwargs: dict[str, Any] = {
            "model": selection.provider_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else settings.llm_default_max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        extra_body = dict(config.extra_body)
        if selection.supports_thinking:
            extra_body["enable_thinking"] = selection.thinking_enabled
        if extra_body:
            kwargs["extra_body"] = extra_body
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    async def _sleep_before_retry(self, attempt: int) -> None:
        """使用带抖动的指数退避，避免节点恢复时出现同步重试。"""
        await asyncio.sleep((2**attempt) + random.random())

    async def _call_once(
        self,
        client: Any,
        config: LLMNodeConfig,
        selection: ModelSelection,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> LLMCallResult:
        """执行一次非流式调用；usage 解析失败时降级估算而不重试模型。"""
        started_at = time.monotonic()
        completion = await asyncio.wait_for(
            client.chat.completions.create(
                **self._build_chat_kwargs(
                    config,
                    selection,
                    messages,
                    max_tokens,
                    temperature,
                    tools,
                    tool_choice,
                    stream=False,
                )
            ),
            timeout=settings.llm_timeout,
        )
        choice = completion.choices[0]
        finish_reason = str(choice.finish_reason or "")
        tool_calls = getattr(choice.message, "tool_calls", None)
        is_tool_call = finish_reason == "tool_calls" and tool_calls
        if is_tool_call:
            content = json.dumps(
                {
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ]
                },
                ensure_ascii=False,
            )
        else:
            raw_content = str(getattr(choice.message, "content", "") or "").strip()
            content = _strip_think_tags(raw_content)

        return LLMCallResult(
            content=content,
            model=selection.provider_model,
            provider="openai-compatible",
            base_url=config.base_url,
            finish_reason=finish_reason,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            model_alias=selection.id,
            model_name=selection.name,
            thinking_enabled=selection.thinking_enabled,
        )

    async def chat_result(
        self,
        messages: list[dict[str, Any]],
        trace_id: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
    ) -> LLMCallResult:
        """调用 LLM；失败时返回 content 为空的结果对象。"""
        started_at = time.monotonic()
        temp = temperature if temperature is not None else settings.llm_default_temperature
        selection = self.resolve_model(model, enable_thinking)
        last_exc: Exception | None = None
        for attempt in range(settings.llm_max_retries):
            try:
                logger.info("[LLM.chat] trace_id=%s attempt=%s", trace_id, attempt + 1)
                result = await self._call_once(
                    self._client,
                    self._api_config,
                    selection,
                    messages,
                    max_tokens,
                    temp,
                    tools,
                    tool_choice,
                )
                result = replace(result, latency_ms=int((time.monotonic() - started_at) * 1000))
                self._record_trace(
                    trace_id=trace_id,
                    selection=selection,
                    latency_ms=result.latency_ms,
                    status="success",
                    tool_count=len(tools or []),
                )
                return result
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable_error(exc):
                    logger.error("[LLM.chat] 不可恢复错误 trace_id=%s error=%s", trace_id, type(exc).__name__)
                    break
                logger.warning("[LLM.chat] 可恢复错误 trace_id=%s error=%s", trace_id, type(exc).__name__)
                if attempt < settings.llm_max_retries - 1:
                    await self._sleep_before_retry(attempt)

        logger.error("[LLM.chat] 所有重试失败 trace_id=%s error=%s", trace_id, type(last_exc).__name__ if last_exc else "unknown")
        result = LLMCallResult(
            content="",
            model="",
            provider="openai-compatible",
            base_url="",
            finish_reason="error",
            latency_ms=int((time.monotonic() - started_at) * 1000),
            model_alias=selection.id,
            model_name=selection.name,
            thinking_enabled=selection.thinking_enabled,
        )
        self._record_trace(
            trace_id=trace_id,
            selection=selection,
            latency_ms=result.latency_ms,
            status="error",
            tool_count=len(tools or []),
        )
        return result

    async def chat(
        self,
        messages: list[dict[str, Any]],
        trace_id: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
    ) -> str:
        """非流式兼容接口，仅返回最终文本。"""
        result = await self.chat_result(
            messages,
            trace_id,
            max_tokens,
            temperature,
            tools,
            tool_choice,
            model,
            enable_thinking,
        )
        return result.content

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        trace_id: str = "",
        max_tokens: int | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
    ) -> dict[str, Any] | None:
        """调用模型并仅在输出为 JSON object 时返回字典。"""
        text = await self.chat(
            messages,
            trace_id=trace_id,
            max_tokens=max_tokens,
            model=model,
            enable_thinking=enable_thinking,
        )
        if not text:
            return None
        cleaned = text.strip()
        for prefix in ("```json", "```"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        if cleaned.startswith("{{") and cleaned.endswith("}}"):
            cleaned = cleaned[1:-1]
        try:
            result = json.loads(cleaned.strip())
        except json.JSONDecodeError as exc:
            logger.warning("[LLM.chat_json] JSON 解析失败 trace_id=%s error=%s", trace_id, exc.msg)
            return None
        return result if isinstance(result, dict) else None

    async def _iter_stream_with_timeout(self, stream: AsyncIterator[Any]) -> AsyncGenerator[Any, None]:
        """为建连后的每个上游 chunk 应用空闲超时。"""
        iterator = stream.__aiter__()
        while True:
            try:
                yield await asyncio.wait_for(anext(iterator), timeout=settings.llm_timeout)
            except StopAsyncIteration:
                return

    async def chat_stream_result(
        self,
        messages: list[dict[str, Any]],
        trace_id: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
    ) -> AsyncGenerator[LLMStreamEvent, None]:
        """流式调用，成功时输出文本事件和一个汇总事件，失败时抛领域异常。"""
        started_at = time.monotonic()
        temp = temperature if temperature is not None else settings.llm_default_temperature
        selection = self.resolve_model(model, enable_thinking)
        last_exc: Exception | None = None
        for attempt in range(settings.llm_max_retries):
            stream: Any | None = None
            visible_parts: list[str] = []
            finish_reason = ""
            parser = ThinkTagFilter()
            tool_calls = ToolCallAccumulator()
            try:
                stream = await asyncio.wait_for(
                    self._client.chat.completions.create(
                        **self._build_chat_kwargs(
                            self._api_config,
                            selection,
                            messages,
                            max_tokens,
                            temp,
                            tools,
                            tool_choice,
                            stream=True,
                        )
                    ),
                    timeout=settings.llm_timeout,
                )
                async for chunk in self._iter_stream_with_timeout(stream):
                    choices = getattr(chunk, "choices", None)
                    if not choices:
                        continue
                    choice = choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = str(choice.finish_reason)
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    for tool_delta in getattr(delta, "tool_calls", None) or []:
                        tool_calls.add(tool_delta)
                    raw_content = str(getattr(delta, "content", "") or "")
                    if raw_content:
                        visible = parser.feed(raw_content)
                        if visible:
                            visible_parts.append(visible)
                            yield LLMStreamEvent(content=visible)

                trailing_content = parser.finalize()
                if trailing_content:
                    visible_parts.append(trailing_content)
                    yield LLMStreamEvent(content=trailing_content)
                if tool_calls.has_calls():
                    yield LLMStreamEvent(tool_calls=tool_calls.to_list())

                output_text = "".join(visible_parts)
                yield LLMStreamEvent(
                    result=LLMCallResult(
                        content=output_text,
                        model=selection.provider_model,
                        provider="openai-compatible",
                        base_url=self._api_config.base_url,
                        finish_reason=finish_reason or "stop",
                        latency_ms=int((time.monotonic() - started_at) * 1000),
                        model_alias=selection.id,
                        model_name=selection.name,
                        thinking_enabled=selection.thinking_enabled,
                    )
                )
                return
            except asyncio.CancelledError:
                logger.info("[LLM.chat_stream] 客户端取消 trace_id=%s", trace_id)
                raise
            except Exception as exc:
                last_exc = exc
                partial_text = "".join(visible_parts)
                if partial_text:
                    yield LLMStreamEvent(
                        result=LLMCallResult(
                            content=partial_text,
                            model=selection.provider_model,
                            provider="openai-compatible",
                            base_url=self._api_config.base_url,
                            finish_reason="error",
                            latency_ms=int((time.monotonic() - started_at) * 1000),
                            model_alias=selection.id,
                            model_name=selection.name,
                            thinking_enabled=selection.thinking_enabled,
                        )
                    )
                    raise LLMCallException("LLM 流式调用中断") from exc
                if not self._is_retryable_error(exc):
                    break
                if attempt < settings.llm_max_retries - 1:
                    await self._sleep_before_retry(attempt)
            finally:
                if stream is not None:
                    await self._close_resource(stream)

        logger.error("[LLM.chat_stream] 所有重试失败 trace_id=%s", trace_id)
        raise LLMCallException("LLM 调用失败") from last_exc

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        trace_id: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
    ) -> AsyncGenerator[str, None]:
        """流式兼容接口，仅输出文本分片。"""
        async for event in self.chat_stream_result(
            messages,
            trace_id,
            max_tokens,
            temperature,
            tools,
            tool_choice,
            model,
            enable_thinking,
        ):
            if event.content:
                yield event.content
            elif event.tool_calls:
                yield json.dumps({"tool_calls": event.tool_calls}, ensure_ascii=False)

    async def embed(self, texts: list[str], trace_id: str = "") -> list[list[float]]:
        """使用复用的 Embedding 客户端调用向量服务，失败时返回空列表。"""
        try:
            response = await asyncio.wait_for(
                self._embedding_client.embeddings.create(model=settings.embed_model, input=texts),
                timeout=settings.llm_timeout,
            )
            return [item.embedding for item in response.data]
        except Exception as exc:
            logger.error("[LLM.embed] 失败 trace_id=%s error=%s", trace_id, type(exc).__name__)
            return []


def get_llm_client() -> MultiLLMClient:
    """创建并返回 MultiLLMClient 实例（每次调用返回新实例）。"""
    return MultiLLMClient()
