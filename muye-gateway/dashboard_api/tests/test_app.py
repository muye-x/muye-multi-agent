"""dashboard-api 对受控服务状态的回归测试。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import textwrap
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from dashboard_api.app import ServiceDefinition, create_app

WEB_ROOT = Path(__file__).resolve().parents[2] / "dashboard" / "web"


def _get(app: object, path: str) -> httpx.Response:
    """通过 ASGI transport 发起无网络请求，避免同步 TestClient 的版本耦合。"""

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_services_exposes_health_and_capability_profiles() -> None:
    """SDK Agent 的真实 profile 应覆盖静态展示默认值。"""

    async def fetch_json(url: str, timeout_seconds: float) -> Mapping[str, object]:
        assert timeout_seconds == 3.0
        if url.endswith("/health"):
            return {"status": "ok"}
        return {"api_profiles": ["internal", "public"]}

    app = create_app(
        (
            ServiceDefinition(
                "travel",
                "Travel",
                "agent",
                "http://travel.test",
                supports_capabilities=True,
                default_profiles=("internal",),
            ),
        ),
        fetch_json,
    )

    response = _get(app, "/services")

    assert response.status_code == 200
    service = response.json()["services"][0]
    assert service["online"] is True
    assert service["profiles"] == ["internal", "public"]
    assert service["capability_available"] is True
    assert "/console" in {route.path for route in app.routes}

    console = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert "Muye 运维控制台" in console
    assert "online.html" in console
    assert 'target="_blank"' in console
    assert "模型、协议与运行时" in console
    assert "marked@15.0.7" not in console
    assert "gateway-token" not in console
    assert "输入 Token 后加载状态" not in console

    online_console = (WEB_ROOT / "online.html").read_text(encoding="utf-8")
    assert "Muye 在线体验" in online_console
    assert "marked@15.0.7" in online_console
    assert "dompurify@3.2.4" in online_console
    assert "stop-button" in online_console
    assert "send-button" in online_console


def test_services_marks_unreachable_service_offline() -> None:
    """下游异常应作为离线状态返回，不能导致整个控制台接口失败。"""

    async def fetch_json(url: str, timeout_seconds: float) -> Mapping[str, object]:
        raise RuntimeError("connection refused")

    app = create_app(
        (ServiceDefinition("llm", "LLM", "llm", "http://llm.test"),),
        fetch_json,
    )

    service = _get(app, "/services").json()["services"][0]
    assert service["online"] is False
    assert service["message"] == "健康检查不可用"


def test_online_console_renders_sse_blocks_separately() -> None:
    """不同 block ID 的增量必须写入独立消息块，不能直接拼接。"""

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the dashboard JavaScript behavior test")

    app_js = WEB_ROOT / "app.js"
    script = textwrap.dedent(
        r"""
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");

        class FakeNode {
          constructor() { this.children = []; this.className = ""; this.dataset = {}; this.textContent = ""; }
          append(...nodes) { this.children.push(...nodes); }
          replaceChildren(...nodes) { this.children = [...nodes]; }
        }

        const source = fs.readFileSync(process.argv[1], "utf8");
        const document = {
          body: { dataset: { page: "test" } },
          documentElement: { scrollHeight: 0 },
          createElement: () => new FakeNode(),
          querySelector: () => null,
          querySelectorAll: () => [],
        };
        const window = { addEventListener() {}, lucide: null, marked: null, scrollTo() {} };
        const sandbox = {
          AbortController,
          TextDecoder,
          cancelAnimationFrame() {},
          document,
          fetch: async () => ({ json: async () => ({ generated_at: 0, services: [] }), ok: true }),
          requestAnimationFrame: () => 1,
          setInterval: () => 0,
          window,
        };
        vm.runInNewContext(`${source}\n;globalThis.testApi = { renderMessageBlocks, updateMessageBlock };`, sandbox);

        const message = { content: "", blocks: [] };
        assert.equal(sandbox.testApi.updateMessageBlock(message, { id: "b1", type: "markdown", delta: "好的，" }), true);
        sandbox.testApi.updateMessageBlock(message, { id: "b1", type: "markdown", delta: "先搜索攻略" });
        sandbox.testApi.updateMessageBlock(message, { id: "b2", type: "markdown", delta: "以下是" });
        sandbox.testApi.updateMessageBlock(message, { id: "b2", type: "markdown", delta: "完整方案" });

        assert.deepEqual(JSON.parse(JSON.stringify(message.blocks)), [
          { id: "b1", type: "markdown", content: "好的，先搜索攻略" },
          { id: "b2", type: "markdown", content: "以下是完整方案" },
        ]);
        assert.equal(message.content, "好的，先搜索攻略\n\n以下是完整方案");

        const container = new FakeNode();
        sandbox.testApi.renderMessageBlocks(container, message.blocks, message.content);
        assert.equal(container.children.length, 2);
        assert.equal(container.children[0].dataset.blockId, "b1");
        assert.equal(container.children[0].textContent, "好的，先搜索攻略");
        assert.equal(container.children[1].dataset.blockId, "b2");
        assert.equal(container.children[1].textContent, "以下是完整方案");
        """
    )

    completed = subprocess.run(
        [node, "-e", script, str(app_js)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
