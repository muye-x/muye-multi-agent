"""
FastAPI 服务端 - 支持流式/非流式对话、多轮对话、记忆管理
"""
import asyncio
import sys
import logging
from secrets import compare_digest
from contextlib import asynccontextmanager

# Windows 平台需要设置事件循环策略（支持 psycopg 异步）
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from muye_multi_agent_sdk import ChannelInvokeRequest, ChannelInvokeResponse, ChannelTextMessage

from core.orchestrator import AgentManager
from config import ResourceManager, get_config
from config.logger_config import init_default_logger
from config.middleware import ClientIPMiddleware
from api.routes import chat_router
from api.trusted_context import trusted_user_id

# 导入收藏路由

# 初始化统一日志系统
init_default_logger()
logger = logging.getLogger(__name__)


# ===== FastAPI 应用 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("=" * 60)
    logger.info("启动 minAgent API 服务")
    logger.info("=" * 60)

    # 初始化 Agent 管理器
    manager = await AgentManager.get_instance()

    yield

    # 关闭时清理资源
    logger.info("关闭 API 服务，清理资源...")

    await manager.cleanup()
    await ResourceManager.cleanup()
    logger.info("资源清理完成")


app = FastAPI(
    title="minAgent API",
    description="支持流式/非流式对话、多轮对话、记忆管理的 AI Agent API",
    version="2.0.0",
    lifespan=lifespan
)

# 注册 IP 中间件（必须在 CORS 之前）
app.add_middleware(ClientIPMiddleware)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(chat_router)


@app.post("/internal/v1/channels/invoke", response_model=ChannelInvokeResponse, include_in_schema=False)
async def channel_invoke(request: ChannelInvokeRequest, raw_request: Request) -> ChannelInvokeResponse:
    """执行 channels 服务提交的标准化文本请求，沿用绑定用户的授权。"""
    token = get_config().catalog.channels_caller_token
    authorization = raw_request.headers.get("authorization", "")
    actual = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not token or not compare_digest(actual, token):
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_ERROR", "message": "Channel caller 无效"})
    try:
        manager = await AgentManager.get_instance()
        result = await manager.chat(
            user_input=request.message.content,
            user_id=request.user_id,
            session_id=request.session_id,
            files=None,
            user_location=None,
            enable_knowledge=True,
            user_informations=None,
            trusted_user_id=request.user_id,
        )
        content = result.get("messages", [])[-1].content if result.get("messages") else ""
        if isinstance(content, str) and content.strip():
            return ChannelInvokeResponse(status="success", trace_id=request.trace_id, message=ChannelTextMessage(content=content))
        return ChannelInvokeResponse(status="error", trace_id=request.trace_id, error={"code": "INVALID_AGENT_RESPONSE", "message": "MainAgent 未返回文本", "recoverable": False})
    except Exception:
        logger.exception("Channel 调用 MainAgent 失败 [channel=%s trace_id=%s]", request.channel, request.trace_id)
        return ChannelInvokeResponse(status="error", trace_id=request.trace_id, error={"code": "AGENT_UNAVAILABLE", "message": "Agent 调用失败", "recoverable": True})


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "minAgent API",
        "version": "2.0.0",
        "status": "running"
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy"}


@app.post("/internal/v1/agents/{agent_id}/smoke")
async def smoke_agent(agent_id: str, request: Request):
    """仅供可信 Gateway/部署链路验证授权后的 Main -> Sub 调用。"""
    user_id = trusted_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "AUTHORIZATION_ERROR", "message": "Control Catalog 未启用"},
        )
    manager = await AgentManager.get_instance()
    try:
        return await manager.smoke_sub_agent(agent_id=agent_id, trusted_user_id=user_id)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTHORIZATION_ERROR", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "AGENT_NOT_READY", "message": str(exc)},
        ) from exc


# ===== 启动服务 =====
if __name__ == "__main__":
    config = get_config()
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        workers=config.server.workers,
        log_level=config.server.log_level,
        access_log=config.server.access_log
    )
