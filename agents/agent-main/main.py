"""
FastAPI 服务端 - 支持流式/非流式对话、多轮对话、记忆管理
"""
import asyncio
import sys
import logging
from contextlib import asynccontextmanager

# Windows 平台需要设置事件循环策略（支持 psycopg 异步）
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from core.orchestrator import AgentManager
from config import ResourceManager, get_config
from config.logger_config import init_default_logger
from config.middleware import ClientIPMiddleware
from api.routes import chat_router

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
