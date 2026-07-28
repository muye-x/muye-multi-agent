"""muye-llm 对话、流式对话、Embedding 与模型能力接口。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config.settings import settings
from src.core.llm_client import LLMCallException, LLMStreamEvent, MultiLLMClient
from src.utils.exceptions import InvalidRequestException

logger = logging.getLogger(__name__)
router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatMessage(BaseModel):
    """OpenAI-compatible 单条对话消息。"""

    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatRequest(BaseModel):
    """模型请求。已移除的 usage_context 会被严格拒绝。"""

    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage]
    trace_id: str = Field(default="")
    model: str | None = None
    enable_thinking: bool | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None


class EmbedRequest(BaseModel):
    """Embedding 请求。"""

    model_config = ConfigDict(extra="forbid")
    texts: list[str] = Field(min_length=1)
    trace_id: str = ""
    model: str | None = None


class RerankRequest(BaseModel):
    """严格校验的 Rerank 请求；候选正文不会写入日志或响应。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=settings.rerank_max_query_chars)
    documents: list[str] = Field(
        min_length=1,
        max_length=settings.rerank_max_documents,
    )
    top_n: int = Field(ge=1)
    model: str | None = None
    trace_id: str = Field(default="", max_length=128)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """拒绝仅含空白的 query，同时保留原文用于模型语义。"""
        if not value.strip():
            raise ValueError("query 不能为空")
        return value

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, values: list[str]) -> list[str]:
        """逐项校验候选文本为空及单文档长度限制。"""
        for value in values:
            if not value.strip():
                raise ValueError("documents 不能包含空文本")
            if len(value) > settings.rerank_max_document_chars:
                raise ValueError("document 超过长度限制")
        return values

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        """限制日志关联 ID 的字符集，避免控制字符进入日志。"""
        normalized = value.strip()
        if normalized and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
            normalized,
        ) is None:
            raise ValueError("trace_id 格式无效")
        return normalized

    @model_validator(mode="after")
    def validate_request_budget(self) -> "RerankRequest":
        """校验结果数不超过候选数，并限制请求正文总字符数。"""
        if self.top_n > len(self.documents):
            raise ValueError("top_n 不能大于 documents 数量")
        total_chars = len(self.query) + sum(len(document) for document in self.documents)
        if total_chars > settings.rerank_max_total_chars:
            raise ValueError("Rerank 请求文本总量超过限制")
        return self


class ApiResponse(BaseModel):
    success: bool
    code: int
    message: str
    data: dict[str, Any]
    timestamp: str


ChatResponse = ApiResponse
EmbedResponse = ApiResponse
RerankResponse = ApiResponse


def _get_client(request: Request) -> MultiLLMClient:
    client = getattr(request.app.state, "llm_client", None)
    if client is None:
        raise RuntimeError("LLM client is not initialized")
    return client


@router.get("/models", response_model=ApiResponse, summary="模型能力列表")
async def list_models(request: Request) -> ApiResponse:
    """只返回可公开的模型别名与 thinking 能力。"""

    registry = _get_client(request).model_registry
    models = [
        {"id": model.id, "name": model.name, "supports_thinking": model.supports_thinking,
         "is_default": model.id == registry.default_model}
        for model in registry.models
    ]
    embedding_registry = _get_client(request).embedding_model_registry
    embedding_models = [
        {
            "id": model.id,
            "name": model.name,
            "dimensions": model.dimensions,
            "is_default": model.id == embedding_registry.default_model,
        }
        for model in embedding_registry.models
    ]
    rerank_client = _get_client(request).rerank_client
    rerank_registry = rerank_client.model_registry
    rerank_models = (
        [
            {
                "id": model.id,
                "name": model.name,
                "is_default": model.id == rerank_registry.default_model,
            }
            for model in rerank_registry.models
        ]
        if rerank_client.enabled
        else []
    )
    return ApiResponse(
        success=True,
        code=200,
        message="ok",
        data={
            "default_model": registry.default_model,
            "default_thinking": registry.default_thinking,
            "models": models,
            "default_embedding_model": embedding_registry.default_model,
            "embedding_models": embedding_models,
            "default_rerank_model": (
                rerank_registry.default_model if rerank_client.enabled else None
            ),
            "rerank_models": rerank_models,
        },
        timestamp=_now_iso(),
    )


@router.post("/chat", response_model=ChatResponse, summary="非流式 LLM 对话")
async def chat(request_data: ChatRequest, request: Request) -> ChatResponse:
    """调用单一 OpenAI-compatible 上游，不执行计费或用量上报。"""

    if not request_data.messages:
        raise InvalidRequestException("messages 不能为空")
    client = _get_client(request)
    client.resolve_model(request_data.model, request_data.enable_thinking)
    result = await client.chat_result(
        [message.model_dump() for message in request_data.messages],
        trace_id=request_data.trace_id,
        max_tokens=request_data.max_tokens,
        temperature=request_data.temperature,
        tools=request_data.tools,
        tool_choice=request_data.tool_choice,
        model=request_data.model,
        enable_thinking=request_data.enable_thinking,
    )
    if not result.content:
        return ChatResponse(success=False, code=502, message="LLM 返回空内容", data={}, timestamp=_now_iso())
    return ChatResponse(success=True, code=200, message="ok", data={"content": result.content}, timestamp=_now_iso())


@router.post("/chat/stream", summary="SSE 流式 LLM 对话", response_class=StreamingResponse)
async def chat_stream(request_data: ChatRequest, request: Request) -> StreamingResponse:
    """按 token、error、done 的固定 SSE 顺序转发上游输出。"""

    if not request_data.messages:
        raise InvalidRequestException("messages 不能为空")
    client = _get_client(request)
    client.resolve_model(request_data.model, request_data.enable_thinking)

    async def generate() -> Any:
        upstream = client.chat_stream_result(
            [message.model_dump() for message in request_data.messages], trace_id=request_data.trace_id,
            max_tokens=request_data.max_tokens, temperature=request_data.temperature,
            tools=request_data.tools, tool_choice=request_data.tool_choice, model=request_data.model,
            enable_thinking=request_data.enable_thinking,
        )
        final_event: LLMStreamEvent | None = None
        try:
            async for event in upstream:
                if event.result is not None:
                    final_event = event
                elif event.content:
                    yield f"event: token\ndata: {json.dumps({'content': event.content}, ensure_ascii=False)}\n\n".encode()
                elif event.tool_calls:
                    yield f"event: tool_calls\ndata: {json.dumps({'tool_calls': event.tool_calls}, ensure_ascii=False)}\n\n".encode()
            reason = final_event.result.finish_reason if final_event and final_event.result else "stop"
            yield f"event: done\ndata: {json.dumps({'finish_reason': reason})}\n\n".encode()
        except LLMCallException as exc:
            logger.warning("stream upstream failure trace_id=%s type=%s", request_data.trace_id, type(exc).__name__)
            yield 'event: error\ndata: {"message":"流式生成服务内部异常"}\n\n'.encode("utf-8")
            yield b'event: done\ndata: {"finish_reason":"error"}\n\n'
        finally:
            close = getattr(upstream, "aclose", None)
            if close is not None:
                await close()

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Trace-Id": request_data.trace_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/embed", response_model=EmbedResponse, summary="文本 Embedding")
async def embed(request_data: EmbedRequest, request: Request) -> EmbedResponse:
    """生成向量；上游异常由统一异常处理器投影。"""

    result = await _get_client(request).embed_result(
        request_data.texts,
        trace_id=request_data.trace_id,
        model=request_data.model,
    )
    if result is None:
        return EmbedResponse(success=False, code=502, message="Embedding 服务调用失败", data={}, timestamp=_now_iso())
    return EmbedResponse(
        success=True,
        code=200,
        message="ok",
        data={
            "embeddings": [list(embedding) for embedding in result.embeddings],
            "count": len(result.embeddings),
            "model": result.model_alias,
            "dimensions": result.dimensions,
        },
        timestamp=_now_iso(),
    )


@router.post("/rerank", response_model=RerankResponse, summary="候选文档 Rerank")
async def rerank(request_data: RerankRequest, request: Request) -> RerankResponse:
    """调用已注册的 Rerank 模型，仅返回原候选索引和相关性分数。"""
    result = await _get_client(request).rerank(
        query=request_data.query,
        documents=request_data.documents,
        top_n=request_data.top_n,
        model=request_data.model,
        trace_id=request_data.trace_id,
    )
    return RerankResponse(
        success=True,
        code=200,
        message="ok",
        data={
            "model": result.model_alias,
            "results": [
                {"index": item.index, "score": item.score} for item in result.items
            ],
            "count": len(result.items),
        },
        timestamp=_now_iso(),
    )
