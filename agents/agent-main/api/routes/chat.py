"""
对话路由 - 处理聊天相关的 API 端点
"""
import logging
from typing import Dict, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.models import ChatRequest, ChatResponse
from core.orchestrator import AgentManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.post("/", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    对话接口（非流式）

    Args:
        request: 对话请求

    Returns:
        对话响应
    """
    try:
        manager = await AgentManager.get_instance()

        # 转换 user_location 为字典
        user_location = None
        if request.user_location:
            user_location = {
                "lat": request.user_location.lat,
                "lng": request.user_location.lng
            }
        user_informations = None
        if request.user_informations:
            user_informations = {
                "name": request.user_informations.name
            }
        # 执行对话
        result = await manager.chat(
            user_input=request.user_input,
            user_id=request.user_id,
            session_id=request.session_id,
            files=request.files,
            user_location=user_location,
            enable_knowledge=request.enable_knowledge,  # 新增传递
            user_informations=user_informations
        )

        # 提取最终响应
        final_message = result.get("messages", [])[-1].content if result.get("messages") else "无响应"

        return ChatResponse(
            success=True,
            user_id=request.user_id,
            session_id=request.session_id,
            message=final_message
        )

    except Exception as e:
        logger.error(f"对话异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stream")
async def chat_stream(request: ChatRequest):
    """
    对话接口（流式输出，包含工具调用过程）

    Args:
        request: 对话请求

    Returns:
        SSE 流式响应
    """
    try:
        manager = await AgentManager.get_instance()

        # 转换 user_location 为字典
        user_location = None
        if request.user_location:
            user_location = {
                "lat": request.user_location.lat,
                "lng": request.user_location.lng
            }
        user_informations = None
        if request.user_informations:
            user_informations = {
                "name": request.user_informations.name
            }
        async def generate():
            """生成流式响应"""
            try:
                async for chunk in manager.chat_stream(
                    user_input=request.user_input,
                    user_id=request.user_id,
                    session_id=request.session_id,
                    files=request.files,
                    user_location=user_location,
                    enable_knowledge=request.enable_knowledge,  # 新增传递
                    user_informations=user_informations
                ):
                    logger.info(chunk)
                    yield chunk
            except Exception as e:
                logger.error(f"流式对话异常: {e}", exc_info=True)
                from api.stream_protocol import EventNormalizer

                normalizer = EventNormalizer(request.session_id, f"stream_error_{request.session_id}", request.user_id)
                yield normalizer.error("STREAM_ERROR", "流式请求初始化失败", {"type": type(e).__name__}).to_sse()
                yield normalizer.done().to_sse()
                yield normalizer.session_end().to_sse()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:
        logger.error(f"流式对话初始化异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{session_id}")
async def get_history(session_id: str):
    """
    获取会话历史

    Args:
        session_id: 会话ID

    Returns:
        历史消息列表
    """
    try:
        manager = await AgentManager.get_instance()
        history = await manager.agent.get_conversation_history(session_id)

        return {
            "success": True,
            "session_id": session_id,
            "history": history
        }

    except Exception as e:
        logger.error(f"获取历史异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/history/{session_id}")
async def clear_history(session_id: str):
    """
    清除会话历史

    Args:
        session_id: 会话ID

    Returns:
        操作结果
    """
    try:
        manager = await AgentManager.get_instance()
        success = await manager.agent.clear_conversation(session_id)

        return {
            "success": success,
            "session_id": session_id,
            "message": "会话历史已清除" if success else "清除失败"
        }

    except Exception as e:
        logger.error(f"清除历史异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
