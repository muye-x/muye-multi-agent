"""健康检查接口。"""
import time

from fastapi import APIRouter, Request

router = APIRouter()

@router.get("/health", tags=["health"])
async def health(request: Request) -> dict:
    """返回服务状态与从 FastAPI lifespan 开始计算的单调运行时长。"""
    started_at = getattr(request.app.state, "started_at", time.monotonic())
    return {
        "status": "ok",
        "service": "4_llm",
        "uptime": round(time.monotonic() - started_at, 1),
    }
