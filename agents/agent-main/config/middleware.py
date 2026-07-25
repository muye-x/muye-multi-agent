from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from config.logger_config import client_ip_ctx
import logging

logger = logging.getLogger(__name__)


class ClientIPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 优先从 X-Forwarded-For 获取（Nginx / K8s）
        x_forwarded_for = request.headers.get("x-forwarded-for")
        if x_forwarded_for:
            client_ip = x_forwarded_for.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else "unknown"

        # 写入 ContextVar
        token = client_ip_ctx.set(client_ip)

        try:
            logger.info(f"Request start: {request.method} {request.url.path}")
            response = await call_next(request)
            logger.info(
                f"Request end: {request.method} {request.url.path} "
                f"status={response.status_code}"
            )
            return response
        finally:
            # 防止协程污染
            client_ip_ctx.reset(token)
