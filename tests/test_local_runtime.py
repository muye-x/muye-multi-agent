"""本地联调进程监督器的会话所有权边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from muye_multi_agent_sdk import AgentIdentity
from muye_multi_agent_sdk.integrations.muye_data import DataAccessContext

from tools.local_runtime.supervisor import LocalRuntimeSupervisor, ServiceSpec


def test_local_dev_deployment_identity_is_accepted_by_sdk_data_context() -> None:
    """local-dev 运行身份必须满足 SDK 的 ResourceName 边界。"""

    context = DataAccessContext(
        service_id="agent-local-handbook",
        deployment_id="deployment-bbbbbbbbbbbb",
        agent=AgentIdentity(
            agent_id="agent_local_handbook",
            agent_version="1.0.0",
            descriptor_checksum="a" * 64,
            source_tree_checksum="b" * 64,
        ),
    )

    assert context.deployment_id == "deployment-bbbbbbbbbbbb"


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
