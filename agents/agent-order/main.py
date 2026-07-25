"""Order Graph 参考服务 ASGI 入口。"""

from __future__ import annotations

import os

import uvicorn

from muye_multi_agent_sdk import AgentConfig, create_app

from order_agent import OrderGraphAgent

config = AgentConfig.from_env()
app = create_app(OrderGraphAgent(config))

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("MUYE_AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("MUYE_AGENT_PORT", "8012")),
    )
