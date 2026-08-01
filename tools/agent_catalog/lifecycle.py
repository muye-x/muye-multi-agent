"""Agent 镜像构建与 Catalog/Compose 显式部署生命周期。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from contracts.catalog import build_catalog_snapshot, canonical_checksum, validate_catalog_snapshot_checksum
from contracts.models import AgentBuildRecordV1, AgentCatalogSnapshotV1, AgentDescriptorV1
from tools.agent_generator.checksums import canonical_checksum as descriptor_checksum
from tools.agent_generator.checksums import source_tree_checksum
from tools.agent_generator.generator import AgentGenerator, GeneratorPaths
from tools.agent_generator.io import load_json_model, load_yaml_model

from .generator import AgentCatalogGenerator, CatalogPaths


_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_BASE_IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:([a-f0-9]{64})")
_IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")
_PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
BUILDER_VERSION = "2.0.0"


class CommandRunner(Protocol):
    """不经过 shell 的外部命令执行边界。"""

    def __call__(self, command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        """执行命令，失败时抛出带退出码但不含 secret 的异常。"""


def _run_command(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@dataclass(frozen=True, slots=True)
class LifecyclePaths:
    """生命周期工具可访问的受控路径。"""

    workspace_root: Path
    agents_root: Path
    builds_root: Path
    artifacts_root: Path
    compose_path: Path

    @classmethod
    def for_workspace(cls, workspace_root: Path) -> "LifecyclePaths":
        root = workspace_root.resolve(strict=True)
        return cls(
            workspace_root=root,
            agents_root=root / "agents",
            builds_root=root / "config" / "generated" / "builds",
            artifacts_root=root / "config" / "generated" / "build-artifacts",
            compose_path=root / "compose.agents.generated.yaml",
        )


class ControlDeploymentClient:
    """使用独立 operator credential 调用固定 Control 部署 API。"""

    def __init__(
        self,
        *,
        base_url: str,
        operator_token: str,
        timeout_seconds: float = 10.0,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MUYE_CONTROL_BASE_URL 必须是不含凭据和路径的 HTTP(S) 根地址")
        if not operator_token.strip():
            raise ValueError("MUYE_CONTROL_OPERATOR_TOKEN 未配置")
        self._base_url = base_url.rstrip("/")
        self._operator_token = operator_token.strip()
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def active(self) -> AgentCatalogSnapshotV1:
        with self._client_factory(timeout=self._timeout_seconds) as client:
            response = client.get(
                f"{self._base_url}/internal/v1/deployment/catalog/active",
                headers=self._headers(),
            )
            response.raise_for_status()
        snapshot = AgentCatalogSnapshotV1.model_validate(response.json())
        validate_catalog_snapshot_checksum(snapshot)
        if response.headers.get("etag", "").strip('"') != snapshot.catalog_checksum:
            raise ValueError("Control active Catalog ETag 不匹配")
        return snapshot

    def submit(
        self,
        snapshot: AgentCatalogSnapshotV1,
        *,
        expected_active_checksum: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        body = {
            "idempotency_key": idempotency_key,
            "expected_active_checksum": expected_active_checksum,
            "snapshot": snapshot.model_dump(mode="json"),
        }
        with self._client_factory(timeout=self._timeout_seconds) as client:
            response = client.post(
                f"{self._base_url}/internal/v1/catalog/candidates",
                headers=self._headers(),
                json=body,
            )
            response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or any(
            not isinstance(value.get(field), str)
            for field in ("catalog_revision", "catalog_checksum", "status")
        ):
            raise ValueError("Control candidate 响应无效")
        return value

    def is_main_acked(self, *, revision: str, checksum: str) -> bool:
        with self._client_factory(timeout=self._timeout_seconds) as client:
            response = client.get(
                f"{self._base_url}/internal/v1/catalog/{revision}/acks/main",
                headers=self._headers(),
                params={"checksum": checksum},
            )
            response.raise_for_status()
        value = response.json()
        return isinstance(value, dict) and value.get("accepted") is True

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._operator_token}"}


class MainSmokeClient:
    """使用 Gateway 到 Main 的独立 caller credential 执行授权 smoke。"""

    def __init__(
        self,
        *,
        base_url: str,
        caller_token: str,
        user_id: str,
        timeout_seconds: float = 60.0,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MUYE_MAIN_BASE_URL 必须是不含凭据和路径的 HTTP(S) 根地址")
        if not caller_token.strip() or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", user_id) is None:
            raise ValueError("Main smoke caller token 或 user ID 无效")
        self._base_url = base_url.rstrip("/")
        self._caller_token = caller_token.strip()
        self._user_id = user_id
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def smoke(self, agent_id: str) -> None:
        with self._client_factory(timeout=self._timeout_seconds) as client:
            response = client.post(
                f"{self._base_url}/internal/v1/agents/{agent_id}/smoke",
                headers={
                    "Authorization": f"Bearer {self._caller_token}",
                    "X-Muye-User-Id": self._user_id,
                },
            )
            response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("status") != "passed" or value.get("agent_id") != agent_id:
            raise ValueError("MainAgent smoke 响应无效")


class AgentLifecycle:
    """可重放的构建、部署、下线和回滚编排，不承载 Control 业务状态。"""

    def __init__(
        self,
        paths: LifecyclePaths,
        *,
        command_runner: CommandRunner = _run_command,
        control_client: ControlDeploymentClient | None = None,
        main_smoke_client: MainSmokeClient | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._paths = paths
        self._command_runner = command_runner
        self._catalog = AgentCatalogGenerator(CatalogPaths.for_workspace(paths.workspace_root))
        self._generator = AgentGenerator(GeneratorPaths.for_workspace(paths.workspace_root))
        self._control_client = control_client
        self._main_smoke_client = main_smoke_client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._sleeper = sleeper

    @classmethod
    def for_workspace(cls, workspace_root: Path) -> "AgentLifecycle":
        root = workspace_root.resolve(strict=True)
        control_url = os.environ.get("MUYE_CONTROL_BASE_URL", "").strip()
        client = None
        if control_url:
            client = ControlDeploymentClient(
                base_url=control_url,
                operator_token=os.environ.get("MUYE_CONTROL_OPERATOR_TOKEN", ""),
            )
        main_url = os.environ.get("MUYE_MAIN_BASE_URL", "").strip()
        smoke_client = None
        if main_url:
            smoke_client = MainSmokeClient(
                base_url=main_url,
                caller_token=os.environ.get("MUYE_MAIN_CALLER_TOKEN", ""),
                user_id=os.environ.get("MUYE_AGENT_SMOKE_USER_ID", ""),
            )
        return cls(
            LifecyclePaths.for_workspace(root),
            control_client=client,
            main_smoke_client=smoke_client,
        )

    def list_agents(self) -> list[dict[str, object]]:
        """列出所有合法 descriptor；缺少 BuildRecord 作为状态返回而不是隐藏 Agent。"""
        result: list[dict[str, object]] = []
        if self._paths.agents_root.is_symlink() or not self._paths.agents_root.is_dir():
            raise ValueError("agents 根目录不存在、不是目录或是符号链接")
        for directory in sorted(self._paths.agents_root.glob("agent-*")):
            descriptor_path = directory / "agent.yaml"
            if not descriptor_path.exists():
                continue
            descriptor = self._load_descriptor(directory.name.removeprefix("agent-"))
            pointer = self._build_pointer(descriptor)
            record_id = None
            if pointer.is_file() and not pointer.is_symlink():
                record_id = load_json_model(pointer, AgentBuildRecordV1).build_record_id
            result.append(
                {
                    "agent_id": descriptor.agent_id,
                    "build_record_id": record_id,
                    "deployment_enabled": descriptor.deployment.enabled,
                    "slug": descriptor.slug,
                    "version": descriptor.version,
                }
            )
        return result

    def build(self, slug: str, *, base_image: str | None = None) -> AgentBuildRecordV1:
        """运行离线测试后构建镜像，并写入历史记录与当前版本指针。"""
        descriptor = self._load_descriptor(slug)
        validation = self._generator.validate(slug=slug)
        if not validation.is_valid:
            raise ValueError("Agent 生成基线无效，拒绝构建")
        image = (base_image or os.environ.get("MUYE_AGENT_BASE_IMAGE", "")).strip()
        match = _BASE_IMAGE_PATTERN.fullmatch(image)
        if match is None:
            raise ValueError("基础镜像必须通过 --base-image 或 MUYE_AGENT_BASE_IMAGE 提供且固定 @sha256 digest")
        directory = self._agent_directory(slug)
        source_checksum = source_tree_checksum(directory)
        descriptor_hash = descriptor_checksum(descriptor.model_dump(mode="json"))
        python = self._paths.workspace_root / ".venv" / "bin" / "python"
        if not python.is_file():
            raise ValueError("Scaffold .venv Python 不存在")
        self._command_runner([str(python), "-m", "compileall", "-q", "."], cwd=directory)
        self._command_runner([str(python), "-m", "pytest", "-q", "tests"], cwd=directory)

        image_tag = f"muye/agent-{slug}:{descriptor.version}"
        self._command_runner(
            [
                "docker",
                "build",
                "--build-arg",
                f"MUYE_AGENT_BASE_IMAGE={image}",
                "--tag",
                image_tag,
                ".",
            ],
            cwd=directory,
        )
        inspected = self._command_runner(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image_tag],
            cwd=self._paths.workspace_root,
        ).stdout.strip()
        if _IMAGE_DIGEST_PATTERN.fullmatch(inspected) is None:
            raise ValueError("docker image inspect 未返回 sha256 digest")

        built_at = self._clock().astimezone(timezone.utc)
        timestamp = built_at.strftime("%Y%m%dt%H%M%Sz").lower()
        build_record_id = f"build_{timestamp}_{inspected[7:19]}"
        artifact_directory = self._paths.artifacts_root / build_record_id
        sbom_path = artifact_directory / "sbom.json"
        report_path = artifact_directory / "test-report.json"
        relative_sbom = sbom_path.relative_to(self._paths.workspace_root).as_posix()
        relative_report = report_path.relative_to(self._paths.workspace_root).as_posix()
        self._write_json_atomic(
            sbom_path,
            {
                "schema_version": "muye.ai/agent-build-sbom/v1",
                "agent_id": descriptor.agent_id,
                "image_digest": inspected,
                "requirements": self._requirements(directory),
            },
        )
        self._write_json_atomic(
            report_path,
            {
                "schema_version": "muye.ai/agent-build-test-report/v1",
                "agent_id": descriptor.agent_id,
                "descriptor_checksum": descriptor_hash,
                "source_tree_checksum": source_checksum,
                "checks": ["compileall", "pytest"],
                "status": "passed",
            },
        )
        record = AgentBuildRecordV1(
            schema_version="muye.ai/agent-build-record/v1",
            build_record_id=build_record_id,
            agent_id=descriptor.agent_id,
            agent_version=descriptor.version,
            descriptor_checksum=descriptor_hash,
            source_tree_checksum=source_checksum,
            sdk_version=validation.provenance.sdk_version,
            base_image_digest=f"sha256:{match.group(1)}",
            image_digest=inspected,
            sbom_ref=relative_sbom,
            test_report_ref=relative_report,
            built_at=built_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            builder_version=BUILDER_VERSION,
        )
        history_path = self._history_record_path(record)
        if history_path.exists():
            existing = load_json_model(history_path, AgentBuildRecordV1)
            if existing != record:
                raise ValueError("BuildRecord ID 冲突")
        else:
            self._write_json_atomic(history_path, record.model_dump(mode="json"))
        self._write_json_atomic(self._build_pointer(descriptor), record.model_dump(mode="json"))
        return record

    def deploy(self, slug: str, *, timeout_seconds: float) -> dict[str, str]:
        """同步全部 candidate、启动目标服务、由 Control 校验并等待 Main ACK。"""
        descriptor = self._load_descriptor(slug)
        if not descriptor.deployment.enabled:
            raise ValueError("Agent deployment.enabled 为 false")
        self._require_control()
        result = self._catalog.sync()
        self._assert_aggregate_consistency(result.input_checksum)
        if not any(entry.agent_id == descriptor.agent_id for entry in result.snapshot.agents):
            raise ValueError("目标 Agent 未进入 Catalog candidate")
        record = load_json_model(self._build_pointer(descriptor), AgentBuildRecordV1)
        self._assert_local_image(record.image_digest)
        # Candidate validation probes every enabled Agent. Start the complete candidate
        # before submitting it so a fresh multi-Agent environment can converge in one deploy.
        services = [entry.service_name for entry in result.snapshot.agents]
        self._compose("up", "-d", *services)
        active = self._control_client.active()  # type: ignore[union-attr]
        expected_active = build_catalog_snapshot(
            [entry.model_copy(update={"status": "ACTIVE"}) for entry in result.snapshot.agents]
        )
        if active == expected_active:
            response = {
                "status": "ACTIVE",
                "catalog_revision": active.catalog_revision,
                "catalog_checksum": active.catalog_checksum,
            }
        else:
            response = self._control_client.submit(  # type: ignore[union-attr]
                result.snapshot,
                expected_active_checksum=active.catalog_checksum,
                idempotency_key=f"deploy:{active.catalog_checksum}:{result.snapshot.catalog_checksum}",
            )
        self._wait_for_ack(response, timeout_seconds=timeout_seconds)
        if self._main_smoke_client is None:
            raise ValueError(
                "部署必须配置 MUYE_MAIN_BASE_URL、MUYE_MAIN_CALLER_TOKEN 和 MUYE_AGENT_SMOKE_USER_ID"
            )
        self._main_smoke_client.smoke(descriptor.agent_id)
        return {
            "agent_id": descriptor.agent_id,
            "catalog_revision": response["catalog_revision"],
            "catalog_checksum": response["catalog_checksum"],
        }

    def stop(self, slug: str, *, timeout_seconds: float) -> dict[str, str]:
        """先发布移除目标的 Catalog 并等待 ACK，再停止目标容器。"""
        descriptor = self._load_descriptor(slug)
        self._require_control()
        active = self._control_client.active()  # type: ignore[union-attr]
        if not any(entry.agent_id == descriptor.agent_id for entry in active.agents):
            raise ValueError("目标 Agent 不在 active Catalog 中")
        candidate = build_catalog_snapshot(
            [entry for entry in active.agents if entry.agent_id != descriptor.agent_id]
        )
        response = self._control_client.submit(  # type: ignore[union-attr]
            candidate,
            expected_active_checksum=active.catalog_checksum,
            idempotency_key=f"stop:{active.catalog_checksum}:{descriptor.agent_id}",
        )
        self._wait_for_ack(response, timeout_seconds=timeout_seconds)
        self._compose("stop", f"agent-{slug}")
        return {
            "agent_id": descriptor.agent_id,
            "catalog_revision": response["catalog_revision"],
            "catalog_checksum": response["catalog_checksum"],
        }

    def rollback(
        self,
        slug: str,
        *,
        build_record_id: str,
        timeout_seconds: float,
    ) -> dict[str, str]:
        """只接受与当前 descriptor/source 精确匹配的历史 BuildRecord。"""
        if re.fullmatch(r"build_[a-z0-9][a-z0-9_-]{2,63}", build_record_id) is None:
            raise ValueError("build_record_id 格式无效")
        descriptor = self._load_descriptor(slug)
        path = self._paths.builds_root / descriptor.agent_id / "records" / f"{build_record_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("历史 BuildRecord 不存在")
        record = load_json_model(path, AgentBuildRecordV1)
        self._validate_record_against_source(record, descriptor, slug)
        pointer = self._build_pointer(descriptor)
        previous = load_json_model(pointer, AgentBuildRecordV1) if pointer.is_file() and not pointer.is_symlink() else None
        self._write_json_atomic(pointer, record.model_dump(mode="json"))
        try:
            response = self.deploy(slug, timeout_seconds=timeout_seconds)
        except Exception:
            if previous is not None:
                self._write_json_atomic(pointer, previous.model_dump(mode="json"))
            else:
                pointer.unlink(missing_ok=True)
            raise
        return {**response, "build_record_id": record.build_record_id, "image_digest": record.image_digest}

    def _validate_record_against_source(
        self,
        record: AgentBuildRecordV1,
        descriptor: AgentDescriptorV1,
        slug: str,
    ) -> None:
        expected = {
            "agent_id": descriptor.agent_id,
            "agent_version": descriptor.version,
            "descriptor_checksum": descriptor_checksum(descriptor.model_dump(mode="json")),
            "source_tree_checksum": source_tree_checksum(self._agent_directory(slug)),
        }
        for field, value in expected.items():
            if getattr(record, field) != value:
                raise ValueError(f"历史 BuildRecord 与当前源码不匹配：{field}")

    def _wait_for_ack(self, response: dict[str, str], *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("ACK timeout 必须为 0 至 600 秒")
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            if self._control_client.is_main_acked(  # type: ignore[union-attr]
                revision=response["catalog_revision"],
                checksum=response["catalog_checksum"],
            ):
                return
            self._sleeper(min(0.5, max(0.0, deadline - self._monotonic())))
        raise TimeoutError("等待 MainAgent Catalog ACK 超时；目标容器保持运行以便诊断")

    def _compose(self, *arguments: str) -> None:
        project = os.environ.get("MUYE_COMPOSE_PROJECT_NAME", "muye").strip()
        if _PROJECT_PATTERN.fullmatch(project) is None:
            raise ValueError("MUYE_COMPOSE_PROJECT_NAME 格式无效")
        command = ["docker", "compose", "-p", project]
        extra_files = [item for item in os.environ.get("MUYE_COMPOSE_BASE_FILES", "").split(os.pathsep) if item]
        for item in extra_files:
            path = (self._paths.workspace_root / item).resolve(strict=True)
            path.relative_to(self._paths.workspace_root)
            if path.is_symlink() or not path.is_file():
                raise ValueError("Compose base file 必须是工作区内普通文件")
            command.extend(["-f", str(path)])
        command.extend(["-f", str(self._paths.compose_path), *arguments])
        self._command_runner(command, cwd=self._paths.workspace_root)

    def _assert_aggregate_consistency(self, input_checksum: str) -> None:
        import yaml

        if self._paths.compose_path.is_symlink() or not self._paths.compose_path.is_file():
            raise ValueError("Compose aggregate 不存在或不是普通文件")
        compose = yaml.safe_load(self._paths.compose_path.read_text(encoding="utf-8"))
        if not isinstance(compose, dict) or compose.get("x-muye-catalog-input-checksum") != input_checksum:
            raise ValueError("Compose aggregate 与 Catalog 输入 checksum 不一致")

    def _assert_local_image(self, image_digest: str) -> None:
        """确认 Compose 将引用的本机内容摘要存在且没有被 tag 解析替换。"""
        inspected = self._command_runner(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image_digest],
            cwd=self._paths.workspace_root,
        ).stdout.strip()
        if inspected != image_digest:
            raise ValueError("BuildRecord image_digest 未加载到本机 Docker daemon")

    def _load_descriptor(self, slug: str) -> AgentDescriptorV1:
        if _SLUG_PATTERN.fullmatch(slug) is None or slug == "main":
            raise ValueError("Agent slug 格式无效")
        directory = self._agent_directory(slug)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Agent 目录不存在、不是目录或是符号链接")
        path = directory / "agent.yaml"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Agent descriptor 不存在、不是普通文件或是符号链接")
        descriptor = load_yaml_model(path, AgentDescriptorV1)
        if descriptor.slug != slug:
            raise ValueError("Agent descriptor slug 与目录不一致")
        return descriptor

    def _agent_directory(self, slug: str) -> Path:
        return self._paths.agents_root / f"agent-{slug}"

    def _build_pointer(self, descriptor: AgentDescriptorV1) -> Path:
        return self._paths.builds_root / descriptor.agent_id / f"{descriptor.version}.json"

    def _history_record_path(self, record: AgentBuildRecordV1) -> Path:
        return self._paths.builds_root / record.agent_id / "records" / f"{record.build_record_id}.json"

    def _require_control(self) -> None:
        if self._control_client is None:
            raise ValueError("部署命令必须配置 MUYE_CONTROL_BASE_URL 和 MUYE_CONTROL_OPERATOR_TOKEN")

    @staticmethod
    def _requirements(directory: Path) -> list[str]:
        path = directory / "requirements.txt"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Agent requirements.txt 不存在或不是普通文件")
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        if path.is_symlink():
            raise ValueError(f"生命周期产物不能是符号链接：{path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
