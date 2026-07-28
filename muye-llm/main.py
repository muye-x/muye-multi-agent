"""
llm 服务入口

功能：多厂商 LLM 统一代理（非流式 + 流式 + Embedding + Rerank）
"""
import logging
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.logger_config import setup_logging
from config.settings import settings
from src.api.chat import router as chat_router
from src.api.health import router as health_router
from src.core.llm_client import MultiLLMClient
from src.utils.exceptions import InvalidRequestException, LLMServiceException

# ── 日志初始化（必须在其他 import 之前） ─────────────────────────────────────
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


# ── 应用生命周期 ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务生命周期管理：创建并确定性关闭 LLM 客户端。"""
    logger.info("[lifespan] muye-llm 服务启动，端口 %s", settings.port)
    app.state.llm_client = MultiLLMClient()
    app.state.started_at = time.monotonic()
    try:
        logger.info(
            "[lifespan] LLM 客户端初始化完成 default_model=%s "
            "default_embedding_model=%s rerank_enabled=%s",
            settings.llm_default_model,
            settings.embed_default_model,
            settings.rerank_enabled,
        )
        yield
    finally:
        await app.state.llm_client.aclose()
        logger.info("[lifespan] muye-llm 服务关闭")


# ── FastAPI 应用 ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="muye-llm — LLM 代理服务",
    description="多厂商 LLM 统一代理：非流式对话 / SSE 流式对话 / Embedding / Rerank",
    version="1.1.0",
    lifespan=lifespan,
)

# CORS（内网微服务间调用，允许所有来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 全局异常处理 ──────────────────────────────────────────────────────────────


@app.exception_handler(InvalidRequestException)
async def invalid_request_handler(request: Request, exc: InvalidRequestException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.code,
        content={"success": False, "code": exc.code, "message": exc.message, "data": None},
    )


@app.exception_handler(LLMServiceException)
async def llm_service_exception_handler(request: Request, exc: LLMServiceException) -> JSONResponse:
    logger.error(f"[LLMServiceException] {exc.message}")
    return JSONResponse(
        status_code=exc.code,
        content={"success": False, "code": exc.code, "message": exc.message, "data": None},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(f"[未处理异常] {exc}")
    return JSONResponse(
        status_code=500,
        content={"success": False, "code": 500, "message": "服务内部异常", "data": None},
    )


# ── 路由注册 ──────────────────────────────────────────────────────────────────

app.include_router(chat_router, prefix="/api/v2", tags=["llm"])
app.include_router(health_router)


def validate_startup_configuration() -> list[str]:
    """返回会阻止 LLM 客户端初始化的缺失配置。"""
    errors: list[str] = []
    if not settings.llm_api_key.strip():
        errors.append("MUYE_LLM_API_KEY 未配置（模型服务 API Key）")
    if not settings.embed_api_key.strip():
        errors.append("MUYE_LLM_EMBED_API_KEY 未配置（Embedding 服务 API Key）")
    if settings.rerank_enabled:
        if not settings.rerank_api_url.strip():
            errors.append("MUYE_LLM_RERANK_API_URL 未配置（Rerank 完整服务 URL）")
        if not settings.rerank_api_key.strip():
            errors.append("MUYE_LLM_RERANK_API_KEY 未配置（Rerank 服务 API Key）")
    return errors


def main() -> None:
    """校验运行配置后启动 ASGI 服务。"""
    configuration_errors = validate_startup_configuration()
    if configuration_errors:
        logger.error("启动配置检查未通过，muye-llm 未启动：")
        for error in configuration_errors:
            logger.error("  - %s", error)
        logger.error("请复制 .env.example 为 .env，填写实际运行值后重新启动。")
        return

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_config=None,
    )


if __name__ == "__main__":
    main()
