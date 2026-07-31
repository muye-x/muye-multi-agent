"""只供 Nginx 反向代理的服务状态与拓扑查询 API。

该进程必须绑定到回环地址。浏览器只读取归一化后的状态，不能获知或访问
服务的内部网络地址。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

FetchJson = Callable[[str, float], Awaitable[Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    """受控服务的展示元数据与固定探测地址。"""

    service_id: str
    name: str
    kind: Literal["gateway", "agent", "llm", "data"]
    base_url: str
    supports_capabilities: bool = False
    default_profiles: tuple[str, ...] = ()


class ServiceStatus(BaseModel):
    """单个服务一次探测的可展示结果。"""

    id: str
    name: str
    kind: Literal["gateway", "agent", "llm", "data"]
    online: bool
    latency_ms: int | None = Field(default=None, ge=0)
    profiles: list[str]
    capability_available: bool
    message: str | None = None


class TopologyEdge(BaseModel):
    """服务间固定依赖关系，用于控制台 Canvas 绘图。"""

    source: str
    target: str
    label: str


class DashboardResponse(BaseModel):
    """控制台概览接口的完整快照。"""

    generated_at: datetime
    services: list[ServiceStatus]
    edges: list[TopologyEdge]


async def _fetch_json(url: str, timeout_seconds: float) -> Mapping[str, object]:
    """请求受控服务的 JSON 健康或能力端点，并拒绝非对象响应。"""
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"请求 {url} 失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"请求 {url} 返回的不是 JSON object")
    return payload


def _service_definitions() -> tuple[ServiceDefinition, ...]:
    """从部署环境读取固定服务地址，禁止由 HTTP 请求覆盖。"""
    definitions = [
        ServiceDefinition(
            "muye-llm",
            "muye-llm",
            "llm",
            os.getenv("MUYE_LLM_URL", "http://127.0.0.1:9850"),
            default_profiles=("internal",),
        ),
    ]
    if os.getenv("MUYE_DATA_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        definitions.append(
            ServiceDefinition(
                "muye-data",
                "muye-data",
                "data",
                os.getenv("MUYE_DATA_URL", "http://127.0.0.1:9840"),
                default_profiles=("internal", "read-only"),
            )
        )
    definitions.extend([
        ServiceDefinition(
            "agent-main",
            "agent-main",
            "gateway",
            os.getenv("MUYE_AGENT_MAIN_URL", "http://127.0.0.1:9860"),
            default_profiles=("gateway",),
        ),
    ])
    return tuple(definitions)


TOPOLOGY_EDGES = (
    TopologyEdge(source="agent-main", target="muye-llm", label="模型调用"),
    TopologyEdge(source="agent-main", target="muye-data", label="按需召回"),
)


async def _probe_service(definition: ServiceDefinition, fetch_json: FetchJson) -> ServiceStatus:
    """异步探测健康状态，并尽量补充 SDK capabilities。"""
    started_at = time.perf_counter()
    try:
        health = await fetch_json(f"{definition.base_url.rstrip('/')}/health", 3.0)
    except RuntimeError as exc:
        logger.info("服务健康检查失败 service=%s error=%s", definition.service_id, exc)
        return ServiceStatus(
            id=definition.service_id,
            name=definition.name,
            kind=definition.kind,
            online=False,
            profiles=list(definition.default_profiles),
            capability_available=False,
            message="健康检查不可用",
        )

    latency_ms = round((time.perf_counter() - started_at) * 1000)
    status = str(health.get("status", "")).lower()
    if status not in {"ok", "healthy", "running"}:
        return ServiceStatus(
            id=definition.service_id,
            name=definition.name,
            kind=definition.kind,
            online=False,
            latency_ms=latency_ms,
            profiles=list(definition.default_profiles),
            capability_available=False,
            message="健康检查返回非正常状态",
        )

    profiles = list(definition.default_profiles)
    capability_available = False
    message: str | None = None
    if definition.supports_capabilities:
        try:
            capabilities = await fetch_json(f"{definition.base_url.rstrip('/')}/capabilities", 3.0)
            received_profiles = capabilities.get("api_profiles")
            if isinstance(received_profiles, list) and all(isinstance(item, str) for item in received_profiles):
                profiles = received_profiles
            capability_available = True
        except RuntimeError as exc:
            message = "能力声明暂不可用"
            logger.info("服务能力查询失败 service=%s error=%s", definition.service_id, exc)

    return ServiceStatus(
        id=definition.service_id,
        name=definition.name,
        kind=definition.kind,
        online=True,
        latency_ms=latency_ms,
        profiles=profiles,
        capability_available=capability_available,
        message=message,
    )


def create_app(
    definitions: tuple[ServiceDefinition, ...] | None = None,
    fetch_json: FetchJson = _fetch_json,
) -> FastAPI:
    """创建管理 API，便于测试注入受控服务和 HTTP fake。"""
    app = FastAPI(title="Muye Gateway Dashboard API", version="1.0.0")
    services = definitions or _service_definitions()

    @app.get("/health")
    async def health() -> dict[str, str]:
        """返回 dashboard-api 自身运行状态，不代表下游服务可用。"""
        return {"status": "ok"}

    @app.get("/internal/auth")
    async def gateway_auth(
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> Response:
        """供 Nginx ``auth_request`` 调用，成功时只回传受信任用户 ID。"""
        session_token = authorization or ""
        control_url = os.getenv("MUYE_CONTROL_BASE_URL", "").rstrip("/")
        gateway_token = os.getenv("MUYE_GATEWAY_CONTROL_TOKEN", "").strip()
        if not control_url or not gateway_token or not session_token:
            raise HTTPException(status_code=401, detail="会话认证不可用")
        try:
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                upstream = await client.post(
                    f"{control_url}/internal/v1/auth/session-introspect",
                    headers={
                        "Authorization": f"Bearer {gateway_token}",
                        "X-Muye-Session-Authorization": session_token,
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("Control session introspection unavailable error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="会话认证暂不可用") from exc
        if upstream.status_code != 200:
            raise HTTPException(status_code=401, detail="登录会话无效")
        payload = upstream.json()
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=503, detail="会话认证响应无效")
        response.headers["X-Muye-User-Id"] = user_id
        return response

    async def dashboard_snapshot() -> DashboardResponse:
        """并发返回所有受控服务的最新健康、能力与拓扑快照。"""
        statuses = await asyncio.gather(
            *(_probe_service(definition, fetch_json) for definition in services)
        )
        service_ids = {definition.service_id for definition in services}
        return DashboardResponse(
            generated_at=datetime.now(UTC),
            services=statuses,
            edges=[
                edge
                for edge in TOPOLOGY_EDGES
                if edge.source in service_ids and edge.target in service_ids
            ],
        )

    @app.get("/services", response_model=DashboardResponse)
    async def list_services() -> DashboardResponse:
        """供 Nginx ``/console/api/`` 反代后的服务状态接口。"""
        return await dashboard_snapshot()

    @app.get("/console/api/services", response_model=DashboardResponse, include_in_schema=False)
    async def local_list_services() -> DashboardResponse:
        """供本地一键启动时控制台同进程访问的服务状态接口。"""
        return await dashboard_snapshot()

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """本地启动时将根路径引导到控制台。"""
        return RedirectResponse(url="/console/")

    static_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard", "web")
    app.mount("/console", StaticFiles(directory=static_root, html=True), name="console")

    return app


app = create_app()
