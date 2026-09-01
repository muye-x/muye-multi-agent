"""knowledge-agent-runtime 容器入口。"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .app import create_app
from .bundle import load_bundle
from .service import RetrievalEvidence, RuntimeBackend, RuntimeService


class UnconfiguredBackend(RuntimeBackend):
    """在 Core 检索/模型适配未注入时 fail-closed，绝不回退到本地工具。"""

    async def retrieve(self, *, resource_id: str, query: str, top_k: int, pipeline: str) -> list[RetrievalEvidence]:
        raise RuntimeError("Core Runtime backend 尚未配置")

    async def answer(self, *, system_instruction: str, task: str, evidence: list[RetrievalEvidence], max_tokens: int) -> str:
        raise RuntimeError("Core Runtime backend 尚未配置")


def main() -> None:
    """仅从平台注入的 Bundle 挂载点启动 Runtime。"""

    bundle_directory = Path(os.environ.get("MUYE_RUNTIME_BUNDLE_DIR", "/run/muye/bundle"))
    bundle = load_bundle(bundle_directory)
    app = create_app(RuntimeService(bundle, UnconfiguredBackend()))
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":  # pragma: no cover
    main()
