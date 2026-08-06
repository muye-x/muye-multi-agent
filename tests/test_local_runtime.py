"""本地联调进程监督器的会话所有权边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.local_runtime.supervisor import LocalRuntimeSupervisor, ServiceSpec


def test_exclusive_service_rejects_a_healthy_foreign_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带本次会话凭据的服务不能因端口已健康而错误复用。"""

    supervisor = LocalRuntimeSupervisor(workspace_root=tmp_path)
    monkeypatch.setattr(supervisor, "_healthy", lambda _url: True)
    spec = ServiceSpec(
        name="muye-data",
        command=("unreachable-command",),
        cwd=tmp_path,
        health_url="http://127.0.0.1:9840/health",
    )

    with pytest.raises(RuntimeError, match="已由其他进程监听"):
        supervisor.start(spec, reuse_healthy=False)

