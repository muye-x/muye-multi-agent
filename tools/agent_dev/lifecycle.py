"""`agent dev` 本地 Main -> SubAgent 联调运行时。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import subprocess
import time
from typing import Any
from urllib.request import Request, urlopen

import yaml

from contracts.local_dev import LocalDevAgentV1, build_local_dev_registration
from contracts.models import AgentDescriptorV1, ResourceSnapshotV1
from tools.agent_creation.service import creation_environment
from tools.agent_generator.checksums import canonical_checksum, source_tree_checksum
from tools.agent_generator.io import load_json_model, load_yaml_model
from tools.local_runtime.supervisor import LocalRuntimeSupervisor, ServiceSpec


@dataclass(frozen=True, slots=True)
class LocalDataRuntime:
    """本地 Data 进程使用的已确认连接信息。

    ``connection_names`` 必须与当前 Agent resource bindings 在 active Snapshot 中的
    connection 完全一致。Milvus token 只进入子进程环境，绝不写入临时 YAML。
    """

    connection_names: tuple[str, ...]
    milvus_uri: str
    milvus_token: str | None

    def environment(self) -> dict[str, str]:
        """返回只在 token 存在时注入 Data 进程的安全环境变量。"""

        return {"MUYE_LOCAL_DEV_MILVUS_TOKEN": self.milvus_token} if self.milvus_token else {}


class AgentDevLifecycle:
    """为一个已生成 Agent 创建严格隔离的本地联调会话。

    会话只注册一个 loopback SubAgent，不写 BuildRecord、生产 Catalog 或 Control
    grant。注册表与 Data 临时配置位于被 Git 忽略的 ``config/runtime/dev`` 下；
    服务 token 仅保留在本次会话拥有的子进程环境中。
    """

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root.resolve(strict=True)
        self._python = self._root / ".venv" / "bin" / "python"
        if not self._python.is_file():
            raise ValueError(f"Scaffold Python environment is unavailable: {self._python}")

    def run(self, slug: str) -> int:
        """启动联调并在 Ctrl+C 时清理当前会话自有服务。"""

        descriptor, agent_directory = self._load_agent(slug)
        self._ensure_web_dependencies()
        data_runtime = self._local_data_runtime(descriptor)
        runtime_directory = self._runtime_directory(slug)
        runtime_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime_directory, 0o700)
        registration_path = runtime_directory / "registration.json"
        data_config_path = runtime_directory / "muye-data.yaml"
        tokens = self._tokens()
        source_checksum = source_tree_checksum(agent_directory)
        descriptor_checksum = canonical_checksum(descriptor.model_dump(mode="json"))
        agent_port = descriptor.runtime.internal_port
        registration = build_local_dev_registration(
            user_id="local-dev-user",
            agent=LocalDevAgentV1(
                agent_id=descriptor.agent_id,
                slug=descriptor.slug,
                agent_version=descriptor.version,
                tool_name=descriptor.tool_name,
                display_name=descriptor.display_name,
                description=descriptor.description,
                supported_intents=descriptor.supported_intents,
                service_name=f"agent-{descriptor.slug}",
                base_url=f"http://127.0.0.1:{agent_port}",
                timeout_seconds=descriptor.runtime.timeout_seconds,
                internal_protocol_version=descriptor.protocol_version,
                descriptor_checksum=descriptor_checksum,
                source_tree_checksum=source_checksum,
                resource_bindings=descriptor.resources,
                max_concurrency=descriptor.runtime.max_concurrency,
            ),
        )
        self._write_private_json(registration_path, registration.model_dump(mode="json"))
        self._write_data_config(data_config_path, runtime=data_runtime)
        supervisor = LocalRuntimeSupervisor(workspace_root=self._root)
        try:
            self._start_services(
                supervisor=supervisor,
                descriptor=descriptor,
                agent_directory=agent_directory,
                registration_path=registration_path,
                data_config_path=data_config_path,
                data_runtime=data_runtime,
                tokens=tokens,
                descriptor_checksum=descriptor_checksum,
                source_checksum=source_checksum,
            )
            print("\n[dev] Local Gateway: http://127.0.0.1:5173/chat", flush=True)
            print(f"[dev] local catalog: {registration.catalog_revision}", flush=True)
            print("[dev] press Ctrl+C to stop this local development session", flush=True)
            previous_handler = signal.getsignal(signal.SIGINT)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                return 0
            finally:
                signal.signal(signal.SIGINT, previous_handler)
        finally:
            supervisor.stop()
            for path in (registration_path, data_config_path):
                path.unlink(missing_ok=True)

    def _start_services(
        self,
        *,
        supervisor: LocalRuntimeSupervisor,
        descriptor: AgentDescriptorV1,
        agent_directory: Path,
        registration_path: Path,
        data_config_path: Path,
        data_runtime: LocalDataRuntime,
        tokens: dict[str, str],
        descriptor_checksum: str,
        source_checksum: str,
    ) -> None:
        llm_url = "http://127.0.0.1:9850"
        data_url = "http://127.0.0.1:9840"
        main_url = "http://127.0.0.1:9860"
        supervisor.start(
            ServiceSpec(
                name="muye-llm",
                command=(str(self._python), "main.py"),
                cwd=self._root / "muye-llm",
                health_url=f"{llm_url}/health",
                environment={"MUYE_LLM_HOST": "127.0.0.1", "MUYE_LLM_PORT": "9850"},
            )
        )
        supervisor.start(
            ServiceSpec(
                name="muye-data",
                command=(str(self._python), "main.py"),
                cwd=self._root / "muye-data",
                health_url=f"{data_url}/health",
                environment={
                    "MUYE_DATA_HOST": "127.0.0.1",
                    "MUYE_DATA_PORT": "9840",
                    "MUYE_DATA_CONFIG_PATH": str(data_config_path),
                    "MUYE_DATA_RESOURCE_SNAPSHOT_PATH": str(self._root / "config/generated/resource-snapshot.json"),
                    "MUYE_DATA_LLM_BASE_URL": llm_url,
                    "MUYE_DATA_AGENT_CATALOG_PATH": "",
                    "MUYE_DATA_LOCAL_DEV_REGISTRATION_PATH": str(registration_path),
                    "MUYE_DATA_AGENT_TOKENS_JSON": json.dumps({descriptor.agent_id: tokens["agent_data"]}),
                    "MUYE_DATA_AGENT_AUTH_ENABLED": "true",
                    **data_runtime.environment(),
                },
            ),
            # Data 的临时注册表和 service token 属于本次会话，不能复用旧进程。
            reuse_healthy=False,
        )
        # SDK 的 DataAccessContext 要求资源名语法；source checksum 同时绑定本次
        # 源码身份，供 muye-data 在每次请求时重新校验。
        deployment_id = f"deployment-{source_checksum[:12]}"
        supervisor.start(
            ServiceSpec(
                name=f"agent-{descriptor.slug}",
                command=(str(self._python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(descriptor.runtime.internal_port)),
                cwd=agent_directory,
                health_url=f"http://127.0.0.1:{descriptor.runtime.internal_port}/health",
                environment={
                    "MUYE_AGENT_MAIN_TOKEN": tokens["agent_main"],
                    "MUYE_AGENT_CONTROL_TOKEN": tokens["agent_control"],
                    "MUYE_AGENT_DATA_TOKEN": tokens["agent_data"],
                    "MUYE_AGENT_SERVICE_ID": f"agent-{descriptor.slug}",
                    "MUYE_AGENT_DEPLOYMENT_ID": deployment_id,
                    "MUYE_AGENT_DESCRIPTOR_CHECKSUM": descriptor_checksum,
                    "MUYE_AGENT_SOURCE_TREE_CHECKSUM": source_checksum,
                    "MUYE_LLM_BASE_URL": llm_url,
                    "MUYE_SDK_DATA_BASE_URL": data_url,
                },
                ),
                reuse_healthy=False,
            )
        self._verify_capabilities(
            f"http://127.0.0.1:{descriptor.runtime.internal_port}",
            token=tokens["agent_main"],
            agent_id=descriptor.agent_id,
            version=descriptor.version,
            descriptor_checksum=descriptor_checksum,
            source_checksum=source_checksum,
        )
        supervisor.start(
            ServiceSpec(
                name="agent-main (local-dev)",
                command=(str(self._python), "main.py"),
                cwd=self._root / "agents/agent-main",
                health_url=f"{main_url}/health",
                environment={
                    "MUYE_AGENT_HOST": "127.0.0.1",
                    "MUYE_AGENT_PORT": "9860",
                    "MUYE_LLM_BASE_URL": llm_url,
                    "MUYE_LLM_MODEL": descriptor.model_alias,
                    "MUYE_SDK_DATA_BASE_URL": data_url,
                    "MUYE_CATALOG_MODE": "local-dev",
                    "MUYE_CONTROL_BASE_URL": "",
                    "MUYE_LOCAL_DEV_REGISTRATION_PATH": str(registration_path),
                    "MUYE_LOCAL_DEV_USER_ID": "local-dev-user",
                    "MUYE_MAIN_CALLER_TOKEN": tokens["main_caller"],
                    "MUYE_MAIN_AGENT_TOKENS_JSON": json.dumps({descriptor.agent_id: tokens["agent_main"]}),
                    # 联调不能继承生产会话或记忆后端，也不能要求其配置存在。
                    "CHECKPOINTER_BACKEND": "memory",
                    "CHECKPOINTER_SQLITE_PATH": "",
                    "CHECKPOINTER_POSTGRESQL_URI": "",
                    "MEMORY_ENABLED": "false",
                    "MEMORY_ENABLE_REDIS": "false",
                    "MEMORY_ENABLE_MONGODB": "false",
                    "MEMORY_ENABLE_EVERMEM": "false",
                },
            ),
            reuse_healthy=False,
        )
        supervisor.start(
            ServiceSpec(
                name="Web Dev Gateway",
                command=("npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"),
                cwd=self._root / "web",
                health_url="http://127.0.0.1:5173/",
                environment={
                    "VITE_MUYE_LOCAL_DEV": "true",
                    "MUYE_DEV_GATEWAY_MAIN_URL": main_url,
                    "MUYE_DEV_GATEWAY_CALLER_TOKEN": tokens["main_caller"],
                    "MUYE_DEV_GATEWAY_USER_ID": "local-dev-user",
                },
            ),
            reuse_healthy=False,
        )

    def _load_agent(self, slug: str) -> tuple[AgentDescriptorV1, Path]:
        if not slug or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in slug):
            raise ValueError("Agent slug 格式无效")
        directory = self._root / "agents" / f"agent-{slug}"
        descriptor_path = directory / "agent.yaml"
        if directory.is_symlink() or descriptor_path.is_symlink() or not descriptor_path.is_file():
            raise ValueError(f"生成 Agent 不存在或描述文件不安全：{directory}")
        descriptor = load_yaml_model(descriptor_path, AgentDescriptorV1)
        if descriptor.slug != slug or directory.name != f"agent-{slug}":
            raise ValueError("Agent 目录与 descriptor slug 不一致")
        return descriptor, directory

    def _runtime_directory(self, slug: str) -> Path:
        return self._root / "config" / "runtime" / "dev" / slug

    def _ensure_web_dependencies(self) -> None:
        """在首次 local-dev 启动时安装 lockfile 固定的 Web 依赖。"""

        web_directory = self._root / "web"
        vite_binary = web_directory / "node_modules" / ".bin" / "vite"
        if vite_binary.is_file():
            return
        if not (web_directory / "package-lock.json").is_file():
            raise ValueError("Web dependency lockfile 不存在，无法启动 local-dev Gateway")
        npm_path = shutil.which("npm")
        if npm_path is None:
            raise ValueError("未找到 npm；请安装 Node.js 后重新执行 agent dev")
        print("[dev] installing locked Web dependencies", flush=True)
        try:
            subprocess.run((npm_path, "ci"), cwd=web_directory, check=True, timeout=600)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Web 依赖安装失败，退出码：{exc.returncode}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Web 依赖安装超时（10 分钟）") from exc
        if not vite_binary.is_file():
            raise RuntimeError("Web 依赖安装完成，但未找到 Vite 可执行文件")

    def _local_data_runtime(self, descriptor: AgentDescriptorV1) -> LocalDataRuntime:
        """从 active Resource Snapshot 与创建配置恢复当前 Agent 的 Data 连接。"""

        snapshot_path = self._root / "config" / "generated" / "resource-snapshot.json"
        snapshot = load_json_model(snapshot_path, ResourceSnapshotV1)
        resource_ids = {binding.resource_id for binding in descriptor.resources}
        missing_resource_ids = sorted(resource_ids - set(snapshot.resources))
        if missing_resource_ids:
            raise ValueError(
                "当前 Agent 的资源不在 active Resource Snapshot 中："
                + ", ".join(missing_resource_ids)
            )
        connection_names = tuple(sorted({snapshot.resources[resource_id].connection for resource_id in resource_ids}))
        environment = creation_environment(self._root)
        milvus_uri = environment.get("MUYE_KNOWLEDGE_MILVUS_URI", "http://127.0.0.1:19530").strip()
        if not milvus_uri:
            raise ValueError("MUYE_KNOWLEDGE_MILVUS_URI 不能为空")
        milvus_token = environment.get("MUYE_KNOWLEDGE_MILVUS_TOKEN", "").strip() or None
        return LocalDataRuntime(
            connection_names=connection_names,
            milvus_uri=milvus_uri.rstrip("/"),
            milvus_token=milvus_token,
        )

    @staticmethod
    def _tokens() -> dict[str, str]:
        values = {name: secrets.token_urlsafe(32) for name in ("agent_main", "agent_control", "agent_data", "main_caller")}
        if len(set(values.values())) != len(values):
            raise RuntimeError("无法生成互不相同的本地服务 token")
        return values

    @staticmethod
    def _write_private_json(path: Path, value: dict[str, Any]) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    @staticmethod
    def _write_data_config(path: Path, *, runtime: LocalDataRuntime) -> None:
        """写入只含连接定义的临时 Data YAML，不将 token 序列化到磁盘。"""

        connection: dict[str, str] = {"type": "milvus", "uri": runtime.milvus_uri}
        if runtime.milvus_token:
            connection["token_env"] = "MUYE_LOCAL_DEV_MILVUS_TOKEN"
        path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "connections": {name: connection for name in runtime.connection_names},
                    "resources": {},
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    @staticmethod
    def _verify_capabilities(
        base_url: str,
        *,
        token: str,
        agent_id: str,
        version: str,
        descriptor_checksum: str,
        source_checksum: str,
    ) -> None:
        request = Request(f"{base_url}/capabilities", headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(request, timeout=5) as response:  # noqa: S310 - local loopback URL.
                payload = json.loads(response.read().decode("utf-8"))
        except OSError as exc:
            raise RuntimeError("无法读取本地 SubAgent capabilities") from exc
        identity = payload.get("identity") if isinstance(payload, dict) else None
        expected = {
            "agent_id": agent_id,
            "agent_version": version,
            "descriptor_checksum": descriptor_checksum,
            "source_tree_checksum": source_checksum,
        }
        if not isinstance(identity, dict) or any(identity.get(key) != value for key, value in expected.items()):
            raise RuntimeError("本地 SubAgent capabilities identity 与 descriptor 不一致")
