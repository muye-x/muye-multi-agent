"""从 Agent Descriptor 与 BuildRecord 确定性生成 Catalog/Compose aggregate。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from contracts.models import (
    AgentBuildRecordV1,
    AgentCatalogEntryV1,
    AgentCatalogSnapshotV1,
    AgentDescriptorV1,
    ResourceSnapshotV1,
)
import yaml

from tools.agent_generator.checksums import canonical_checksum, source_tree_checksum
from tools.agent_generator.generator import AgentGenerator, GeneratorPaths
from tools.agent_generator.io import load_json_model, load_yaml_model
from tools.knowledge_pipeline.checksums import verify_declared_checksum

from .checksums import build_catalog_snapshot, capabilities_identity_checksum


CATALOG_FILE = Path("config/generated/agent-catalog.json")
COMPOSE_FILE = Path("compose.agents.generated.yaml")
REPORT_FILE = Path("config/generated/catalog-report.json")
BUILD_ROOT = Path("config/generated/builds")
_DIGEST_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class CatalogPaths:
    """Catalog 生成器的全部受控工作区路径。"""

    workspace_root: Path
    agents_root: Path
    builds_root: Path
    catalog_path: Path
    compose_path: Path
    report_path: Path
    resource_snapshot_path: Path

    @classmethod
    def for_workspace(cls, workspace_root: Path) -> "CatalogPaths":
        root = workspace_root.resolve(strict=True)
        return cls(
            workspace_root=root,
            agents_root=root / "agents",
            builds_root=root / BUILD_ROOT,
            catalog_path=root / CATALOG_FILE,
            compose_path=root / COMPOSE_FILE,
            report_path=root / REPORT_FILE,
            resource_snapshot_path=root / "config" / "generated" / "resource-snapshot.json",
        )


@dataclass(frozen=True, slots=True)
class CatalogSyncResult:
    """一次 sync 的确定性内存结果与是否写入状态。"""

    snapshot: AgentCatalogSnapshotV1
    input_checksum: str
    report: dict[str, object]
    compose: dict[str, object]
    changed: bool


@dataclass(frozen=True, slots=True)
class _ScannedAgent:
    directory: Path
    descriptor: AgentDescriptorV1
    descriptor_checksum: str
    source_checksum: str
    build_record: AgentBuildRecordV1 | None


class AgentCatalogGenerator:
    """全量扫描 Agent 输入；任一无效项都会使整个 candidate 失败。"""

    def __init__(self, paths: CatalogPaths) -> None:
        self._paths = paths
        self._agent_generator = AgentGenerator(GeneratorPaths.for_workspace(paths.workspace_root))

    def sync(self, *, check: bool = False) -> CatalogSyncResult:
        """生成全部聚合输出，或只读比较受版本控制的 Catalog。

        `check=True` 仍会在内存中完整生成并校验 Compose 与报告，但只要求提交管理的
        Catalog 存在且一致。正常模式在完成全部输入校验和内容构造后才逐个原子替换，
        且将可提交的 Catalog 最后切换为新的事实源。
        """
        scanned = self.scan()
        snapshot, input_checksum, report, compose = self._build_outputs(scanned)
        rendered = self._render_outputs(snapshot=snapshot, report=report, compose=compose)
        changed = any(self._read_existing(path) != content for path, content in rendered.items())
        if check:
            if self._read_existing(self._paths.catalog_path) != rendered[self._paths.catalog_path]:
                raise ValueError("Agent Catalog 与当前 Descriptor/BuildRecord 不一致")
            return CatalogSyncResult(snapshot, input_checksum, report, compose, changed=False)
        for path in (self._paths.compose_path, self._paths.report_path, self._paths.catalog_path):
            self._write_atomic(path, rendered[path])
        return CatalogSyncResult(snapshot, input_checksum, report, compose, changed=changed)

    def scan(self) -> tuple[_ScannedAgent, ...]:
        """扫描 `agents/agent-*/agent.yaml`，忽略没有 descriptor 的历史示例目录。"""
        if self._paths.agents_root.is_symlink() or not self._paths.agents_root.is_dir():
            raise ValueError(f"Agent 根目录不存在、不是目录或是符号链接：{self._paths.agents_root}")
        scanned: list[_ScannedAgent] = []
        active_resource_ids: frozenset[str] | None = None
        for directory in sorted(self._paths.agents_root.glob("agent-*")):
            descriptor_path = directory / "agent.yaml"
            if not descriptor_path.exists():
                continue
            if directory.is_symlink() or descriptor_path.is_symlink() or not descriptor_path.is_file():
                raise ValueError(f"Agent descriptor 必须是受控普通文件：{descriptor_path}")
            descriptor = load_yaml_model(descriptor_path, AgentDescriptorV1)
            if directory.name != f"agent-{descriptor.slug}":
                raise ValueError(f"Agent 目录与 descriptor slug 不一致：{directory}")
            validation = self._agent_generator.validate(slug=descriptor.slug)
            if not validation.is_valid:
                raise ValueError(f"Agent 生成基线无效：{descriptor.slug}")
            descriptor_checksum = canonical_checksum(descriptor.model_dump(mode="json"))
            source_checksum = source_tree_checksum(directory)
            build_record = self._load_build_record(descriptor) if descriptor.deployment.enabled else None
            if build_record is not None:
                if active_resource_ids is None:
                    active_resource_ids = self._load_active_resource_ids()
                unknown_resources = {
                    binding.resource_id for binding in descriptor.resources
                } - active_resource_ids
                if unknown_resources:
                    raise ValueError(
                        "启用部署的 Agent 引用了未发布 Resource：" + ", ".join(sorted(unknown_resources))
                    )
                self._validate_build_record(
                    descriptor=descriptor,
                    descriptor_checksum=descriptor_checksum,
                    source_checksum=source_checksum,
                    build_record=build_record,
                    sdk_version=validation.provenance.sdk_version,
                )
            scanned.append(
                _ScannedAgent(
                    directory=directory,
                    descriptor=descriptor,
                    descriptor_checksum=descriptor_checksum,
                    source_checksum=source_checksum,
                    build_record=build_record,
                )
            )
        self._validate_global_uniqueness(scanned)
        return tuple(scanned)

    def _load_active_resource_ids(self) -> frozenset[str]:
        """读取阶段 4 已发布快照，并复核 Snapshot 与每个 Manifest checksum。"""
        path = self._paths.resource_snapshot_path
        if path.is_symlink() or not path.is_file():
            raise ValueError("启用部署的 Agent 要求已发布 Resource Snapshot")
        snapshot = load_json_model(path, ResourceSnapshotV1)
        verify_declared_checksum(snapshot, checksum_field="snapshot_checksum", label="Resource Snapshot")
        for manifest in snapshot.resources.values():
            verify_declared_checksum(
                manifest,
                checksum_field="resource_checksum",
                label=f"Resource {manifest.resource_id}",
            )
        return frozenset(snapshot.resources)

    def _load_build_record(self, descriptor: AgentDescriptorV1) -> AgentBuildRecordV1:
        path = self._paths.builds_root / descriptor.agent_id / f"{descriptor.version}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"启用部署的 Agent 缺少 BuildRecord：{path}")
        return load_json_model(path, AgentBuildRecordV1)

    @staticmethod
    def _validate_build_record(
        *,
        descriptor: AgentDescriptorV1,
        descriptor_checksum: str,
        source_checksum: str,
        build_record: AgentBuildRecordV1,
        sdk_version: str,
    ) -> None:
        mismatches: list[str] = []
        for field_name, actual, expected in (
            ("agent_id", build_record.agent_id, descriptor.agent_id),
            ("agent_version", build_record.agent_version, descriptor.version),
            ("descriptor_checksum", build_record.descriptor_checksum, descriptor_checksum),
            ("source_tree_checksum", build_record.source_tree_checksum, source_checksum),
            ("sdk_version", build_record.sdk_version, sdk_version),
        ):
            if actual != expected:
                mismatches.append(field_name)
        if mismatches:
            raise ValueError("BuildRecord 与当前 Agent 不一致：" + ", ".join(mismatches))
        if _DIGEST_PATTERN.fullmatch(build_record.image_digest) is None:
            raise ValueError("BuildRecord image_digest 必须固定为 sha256 digest")

    @staticmethod
    def _validate_global_uniqueness(scanned: list[_ScannedAgent]) -> None:
        for label, values in (
            ("agent_id", [item.descriptor.agent_id for item in scanned]),
            ("slug", [item.descriptor.slug for item in scanned]),
            ("tool_name", [item.descriptor.tool_name for item in scanned]),
            ("service_name", [f"agent-{item.descriptor.slug}" for item in scanned]),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"Agent descriptor 的 {label} 必须全局唯一")

    def _build_outputs(
        self,
        scanned: tuple[_ScannedAgent, ...],
    ) -> tuple[AgentCatalogSnapshotV1, str, dict[str, object], dict[str, object]]:
        entries: list[AgentCatalogEntryV1] = []
        report_agents: list[dict[str, object]] = []
        input_items: list[dict[str, object]] = []
        services: dict[str, object] = {}
        for item in scanned:
            descriptor = item.descriptor
            service_name = f"agent-{descriptor.slug}"
            record = item.build_record
            status = "STARTING" if record is not None else "DISCOVERED"
            input_items.append(
                {
                    "build_record": record.model_dump(mode="json") if record else None,
                    "descriptor": descriptor.model_dump(mode="json"),
                    "descriptor_checksum": item.descriptor_checksum,
                    "source_tree_checksum": item.source_checksum,
                }
            )
            report_agents.append(
                {
                    "agent_id": descriptor.agent_id,
                    "agent_version": descriptor.version,
                    "build_record_id": record.build_record_id if record else None,
                    "descriptor_checksum": item.descriptor_checksum,
                    "image_reference": record.image_digest if record else None,
                    "image_tag": f"muye/{service_name}:{descriptor.version}" if record else None,
                    "service_name": service_name,
                    "slug": descriptor.slug,
                    "source_tree_checksum": item.source_checksum,
                    "status": status,
                    "tool_name": descriptor.tool_name,
                }
            )
            if record is None:
                continue
            capabilities_checksum = capabilities_identity_checksum(
                agent_id=descriptor.agent_id,
                agent_version=descriptor.version,
                descriptor_checksum=item.descriptor_checksum,
                source_tree_checksum=item.source_checksum,
                internal_protocol_version=descriptor.protocol_version,
            )
            entry = AgentCatalogEntryV1(
                caller="agent-main",
                target_type="sub_agent",
                agent_id=descriptor.agent_id,
                agent_version=descriptor.version,
                tool_name=descriptor.tool_name,
                display_name=descriptor.display_name,
                description=descriptor.description,
                supported_intents=descriptor.supported_intents,
                service_name=service_name,
                base_url=f"http://{service_name}:{descriptor.runtime.internal_port}",
                timeout_seconds=descriptor.runtime.timeout_seconds,
                internal_protocol_version=descriptor.protocol_version,
                api_profile="internal",
                descriptor_checksum=item.descriptor_checksum,
                source_tree_checksum=item.source_checksum,
                image_digest=record.image_digest,
                resource_bindings=descriptor.resources,
                capabilities_checksum=capabilities_checksum,
                max_concurrency=descriptor.runtime.max_concurrency,
                status="STARTING",
            )
            entries.append(entry)
            services[service_name] = self._compose_service(item, entry)
        input_checksum = canonical_checksum(input_items)
        snapshot = build_catalog_snapshot(entries)
        report: dict[str, object] = {
            "schema_version": "muye.ai/catalog-report/v1",
            "catalog_checksum": snapshot.catalog_checksum,
            "catalog_revision": snapshot.catalog_revision,
            "input_checksum": input_checksum,
            "agents": report_agents,
        }
        compose: dict[str, object] = {
            "x-muye-catalog-input-checksum": input_checksum,
            "services": services,
        }
        return snapshot, input_checksum, report, compose

    @staticmethod
    def _compose_service(item: _ScannedAgent, entry: AgentCatalogEntryV1) -> dict[str, object]:
        descriptor = item.descriptor
        record = item.build_record
        assert record is not None
        token_prefix = "MUYE_AGENT_" + re.sub(r"[^A-Z0-9]", "_", descriptor.agent_id.upper())
        return {
            # 阶段 5 使用本机 Docker daemon 中已加载的内容寻址 image ID。直接引用
            # sha256 ID 可避免把 config digest 错当作 registry manifest digest 拼到 tag 后。
            "image": record.image_digest,
            "restart": "unless-stopped",
            "read_only": True,
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=64m"],
            "pids_limit": 256,
            "mem_limit": descriptor.runtime.memory_limit.lower(),
            "cpus": "1.0",
            "networks": ["internal"],
            "stop_grace_period": "20s",
            "environment": {
                "MUYE_AGENT_DEPLOYMENT_ID": f"{descriptor.agent_id}:{descriptor.version}:{record.image_digest[7:19]}",
                "MUYE_AGENT_DESCRIPTOR_CHECKSUM": item.descriptor_checksum,
                "MUYE_AGENT_MAIN_TOKEN": f"${{{token_prefix}_MAIN_TOKEN:?set {token_prefix}_MAIN_TOKEN}}",
                "MUYE_AGENT_CONTROL_TOKEN": (
                    f"${{{token_prefix}_CONTROL_TOKEN:?set {token_prefix}_CONTROL_TOKEN}}"
                ),
                "MUYE_AGENT_DATA_TOKEN": f"${{{token_prefix}_DATA_TOKEN:?set {token_prefix}_DATA_TOKEN}}",
                "MUYE_AGENT_SERVICE_ID": entry.service_name,
                "MUYE_AGENT_SOURCE_TREE_CHECKSUM": item.source_checksum,
                "MUYE_LLM_BASE_URL": "http://muye-llm:9850",
                "MUYE_SDK_DATA_BASE_URL": "http://muye-data:9840",
                "MUYE_SDK_API_PROFILES": "internal",
            },
            "labels": {
                "muye.agent.id": descriptor.agent_id,
                "muye.agent.version": descriptor.version,
                "muye.catalog.capabilities-checksum": entry.capabilities_checksum,
                "muye.catalog.descriptor-checksum": item.descriptor_checksum,
                "muye.catalog.source-tree-checksum": item.source_checksum,
            },
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    (
                        "import urllib.request; "
                        f"urllib.request.urlopen('http://127.0.0.1:{descriptor.runtime.internal_port}/ready', timeout=3).read()"
                    ),
                ],
                "interval": "5s",
                "timeout": "3s",
                "retries": 12,
                "start_period": "10s",
            },
        }

    def _render_outputs(
        self,
        *,
        snapshot: AgentCatalogSnapshotV1,
        report: dict[str, object],
        compose: dict[str, object],
    ) -> dict[Path, str]:
        return {
            self._paths.catalog_path: json.dumps(
                snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            self._paths.report_path: json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            self._paths.compose_path: (
                "# Generated by `scripts/muye.sh agent sync`; DO NOT EDIT.\n"
                + yaml.safe_dump(compose, allow_unicode=True, sort_keys=True)
            ),
        }

    @staticmethod
    def _read_existing(path: Path) -> str | None:
        if path.is_symlink():
            raise ValueError(f"Catalog 生成物不能是符号链接：{path}")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError(f"Catalog 生成物必须是普通文件：{path}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        if path.is_symlink():
            raise ValueError(f"Catalog 生成物不能是符号链接：{path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
