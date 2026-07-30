"""muye-data 独立服务入口。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


SERVICE_DIR = Path(__file__).resolve().parent
load_dotenv(SERVICE_DIR / ".env")

from src.api import router  # noqa: E402
from src.backends.factory import build_backends  # noqa: E402
from src.clients.llm import MuyeLLMClient  # noqa: E402
from src.config import ServiceSettings, load_data_config  # noqa: E402
from src.contracts import ErrorResponse  # noqa: E402
from src.errors import ConfigurationError, DataServiceError  # noqa: E402
from src.retrieval.service import RetrievalService  # noqa: E402
from src.snapshots import load_resource_snapshot  # noqa: E402


logger = logging.getLogger(__name__)


def _config_path(settings: ServiceSettings) -> Path:
    return settings.config_path if settings.config_path.is_absolute() else SERVICE_DIR / settings.config_path


def _snapshot_path(settings: ServiceSettings) -> Path | None:
    """将快照相对路径解析到服务目录，部署可通过绝对挂载路径覆盖。"""
    if settings.resource_snapshot_path is None:
        return None
    return (
        settings.resource_snapshot_path
        if settings.resource_snapshot_path.is_absolute()
        else SERVICE_DIR / settings.resource_snapshot_path
    )


def build_service(settings: ServiceSettings) -> RetrievalService:
    """从本地配置装配运行时；客户端保持惰性，不在此阶段访问网络。"""
    config = load_data_config(_config_path(settings))
    snapshot_path = _snapshot_path(settings)
    if snapshot_path is not None:
        snapshot = load_resource_snapshot(snapshot_path, known_connections=set(config.connections))
        config = config.model_copy(update={"resources": snapshot.resources})
    backends = build_backends(config)
    llm_client = MuyeLLMClient(
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    service = RetrievalService(
        config=config,
        settings=settings,
        backends=backends,
        llm_client=llm_client,
    )
    if snapshot_path is not None:
        service.configure_resource_snapshot(snapshot_path)
    return service


async def _resource_snapshot_reload_loop(service: RetrievalService, interval_seconds: float) -> None:
    """轮询已挂载 Snapshot；无效候选仅记录错误并继续服务旧资源表。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            service.reload_resource_snapshot()
        except ConfigurationError as exc:
            logger.error("resource snapshot reload rejected: %s", exc)
        except Exception:
            logger.exception("resource snapshot reload failed")


def _trace_id(request: Request, body: Any | None = None) -> str:
    """从安全来源提取 trace ID，格式异常时生成本地值。"""
    candidate = request.headers.get("X-Trace-Id", "").strip()
    if isinstance(body, dict) and isinstance(body.get("trace_id"), str):
        candidate = body["trace_id"].strip() or candidate
    if not candidate or len(candidate) > 128 or not all(
        character.isalnum() or character in "_.:-" for character in candidate
    ):
        return uuid.uuid4().hex
    return candidate


def _error_response(
    *,
    status_code: int,
    error_code: str,
    message: str,
    recoverable: bool,
    trace_id: str,
) -> JSONResponse:
    payload = ErrorResponse(
        error_code=error_code,
        message=message,
        recoverable=recoverable,
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers={"X-Trace-Id": trace_id},
    )


def create_app(
    *,
    service: RetrievalService | Any | None = None,
    settings_override: ServiceSettings | None = None,
) -> FastAPI:
    """创建应用；注入 service 时调用方保留其资源所有权。"""
    settings = settings_override or ServiceSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.started_at = time.monotonic()
        owned_service = service is None
        app.state.retrieval_service = service or build_service(settings)
        reload_task: asyncio.Task[None] | None = None
        if owned_service and settings.resource_snapshot_path is not None:
            reload_task = asyncio.create_task(
                _resource_snapshot_reload_loop(
                    app.state.retrieval_service,
                    settings.resource_snapshot_poll_seconds,
                )
            )
        logger.info("muye-data started port=%s", settings.port)
        try:
            yield
        finally:
            if reload_task is not None:
                reload_task.cancel()
                try:
                    await reload_task
                except asyncio.CancelledError:
                    pass
            if owned_service:
                await app.state.retrieval_service.aclose()
            logger.info("muye-data stopped")

    application = FastAPI(
        title="muye-data",
        description="只读、多数据库兼容的查询、召回、融合与重排服务",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.exception_handler(DataServiceError)
    async def data_service_error_handler(request: Request, exc: DataServiceError) -> JSONResponse:
        trace_id = exc.trace_id or _trace_id(request)
        logger.warning(
            "request failed trace_id=%s path=%s error_code=%s recoverable=%s",
            trace_id,
            request.url.path,
            exc.error_code,
            exc.recoverable,
        )
        return _error_response(
            status_code=exc.status_code,
            error_code=exc.error_code,
            message=exc.message,
            recoverable=exc.recoverable,
            trace_id=trace_id,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            status_code=422,
            error_code="VALIDATION_ERROR",
            message="请求参数校验失败",
            recoverable=False,
            trace_id=_trace_id(request, exc.body),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "METHOD_NOT_ALLOWED" if exc.status_code == 405 else "HTTP_ERROR"
        message = "接口不存在" if exc.status_code == 404 else "请求方法不允许" if exc.status_code == 405 else "HTTP 请求失败"
        return _error_response(
            status_code=exc.status_code,
            error_code=code,
            message=message,
            recoverable=False,
            trace_id=_trace_id(request),
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = _trace_id(request)
        logger.exception(
            "unexpected request failure trace_id=%s path=%s error_type=%s",
            trace_id,
            request.url.path,
            type(exc).__name__,
        )
        return _error_response(
            status_code=500,
            error_code="INTERNAL_ERROR",
            message="服务内部异常",
            recoverable=False,
            trace_id=trace_id,
        )

    application.include_router(router)
    return application


settings = ServiceSettings.from_env()
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app = create_app(settings_override=settings)


def validate_startup_configuration(settings_to_validate: ServiceSettings | None = None) -> list[str]:
    """返回会阻止服务启动的本地配置错误，不访问数据库或 muye-llm。"""
    selected = settings_to_validate or settings
    try:
        config = load_data_config(_config_path(selected))
        build_backends(config)
    except ConfigurationError as exc:
        return [str(exc)]
    return []


def main() -> None:
    """校验本地配置后启动 ASGI 服务。"""
    errors = validate_startup_configuration()
    if errors:
        for error in errors:
            logger.error("启动配置检查失败：%s", error)
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
