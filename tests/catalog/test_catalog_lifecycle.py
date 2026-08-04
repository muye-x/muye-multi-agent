"""阶段 5 Catalog/Compose 生成与 CLI 生命周期回归测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

import yaml

from contracts.models import AgentBuildRecordV1, AgentDescriptorV1, ResourceSnapshotV1
from contracts.catalog import build_catalog_snapshot
from tools.agent_catalog.generator import AgentCatalogGenerator, CatalogPaths, CatalogSyncResult
from tools.agent_catalog.lifecycle import AgentLifecycle, LifecyclePaths
from tools.agent_generator.checksums import canonical_checksum, source_tree_checksum
from tools.agent_generator.generator import AgentGenerator, GeneratorPaths
from tools.agent_generator.io import load_yaml_model
from tools.cli import build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXED_TIME = datetime(2026, 7, 31, 1, 2, 3, tzinfo=timezone.utc)


def _workspace(tmp_path: Path, *, enabled: bool) -> tuple[Path, AgentDescriptorV1]:
    root = tmp_path / "workspace"
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "tests" / "fixtures" / "generator" / "config", root / "config")
    shutil.copytree(PROJECT_ROOT / "templates", root / "templates")
    generator = AgentGenerator(GeneratorPaths.for_workspace(root), clock=lambda: FIXED_TIME)
    generated = generator.generate(slug="product-handbook", knowledge_slug="product-handbook")
    descriptor_path = generated.directory / "agent.yaml"
    payload = yaml.safe_load(descriptor_path.read_text(encoding="utf-8"))
    payload["deployment"]["enabled"] = enabled
    descriptor_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    descriptor = load_yaml_model(descriptor_path, AgentDescriptorV1)
    if enabled:
        resource_snapshot = json.loads(
            (PROJECT_ROOT / "contracts" / "fixtures" / "resource-snapshot-v1.valid.json").read_text(
                encoding="utf-8"
            )
        )
        normalized_snapshot = ResourceSnapshotV1.model_validate(resource_snapshot).model_dump(mode="json")
        manifest = normalized_snapshot["resources"]["kb.product_handbook"]
        manifest_without_checksum = dict(manifest)
        manifest_without_checksum.pop("resource_checksum")
        manifest["resource_checksum"] = canonical_checksum(manifest_without_checksum)
        snapshot_without_checksum = dict(normalized_snapshot)
        snapshot_without_checksum.pop("snapshot_checksum")
        normalized_snapshot["snapshot_checksum"] = canonical_checksum(snapshot_without_checksum)
        snapshot_path = root / "config" / "generated" / "resource-snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(normalized_snapshot), encoding="utf-8")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    return root, descriptor


def _write_build_record(root: Path, descriptor: AgentDescriptorV1) -> AgentBuildRecordV1:
    directory = root / "agents" / f"agent-{descriptor.slug}"
    record = AgentBuildRecordV1(
        schema_version="muye.ai/agent-build-record/v1",
        build_record_id="build_catalog_fixture",
        agent_id=descriptor.agent_id,
        agent_version=descriptor.version,
        descriptor_checksum=canonical_checksum(descriptor.model_dump(mode="json")),
        source_tree_checksum=source_tree_checksum(directory),
        sdk_version="2.0.0",
        base_image_digest=f"sha256:{'a' * 64}",
        image_digest=f"sha256:{'b' * 64}",
        sbom_ref="artifacts/sbom.json",
        test_report_ref="artifacts/tests.json",
        built_at="2026-07-31T01:02:03Z",
        builder_version="2.0.0",
    )
    path = root / "config" / "generated" / "builds" / descriptor.agent_id / f"{descriptor.version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.model_dump(mode="json")), encoding="utf-8")
    return record


def test_sync_supports_empty_catalog_and_check_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    (root / "agents").mkdir(parents=True)
    (root / "templates" / "agents").mkdir(parents=True)
    generator = AgentCatalogGenerator(CatalogPaths.for_workspace(root))

    result = generator.sync()
    (root / "compose.agents.generated.yaml").unlink()
    (root / "config" / "generated" / "catalog-report.json").unlink()
    checked = generator.sync(check=True)

    assert result.snapshot.agents == []
    assert checked.changed is False
    assert checked.compose["services"] == {}
    assert checked.compose["x-muye-catalog-input-checksum"] == result.input_checksum
    assert not (root / "compose.agents.generated.yaml").exists()
    assert not (root / "config" / "generated" / "catalog-report.json").exists()


def test_sync_generates_digest_only_internal_service_deterministically(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    record = _write_build_record(root, descriptor)
    generator = AgentCatalogGenerator(CatalogPaths.for_workspace(root))

    first = generator.sync()
    second = generator.sync()

    assert first.snapshot == second.snapshot
    assert first.snapshot.agents[0].status == "STARTING"
    assert first.snapshot.agents[0].base_url == "http://agent-product-handbook:8000"
    service = first.compose["services"]["agent-product-handbook"]
    assert service["image"] == record.image_digest
    assert service["environment"]["MUYE_AGENT_DATA_TOKEN"].startswith("${MUYE_AGENT_")
    assert "ports" not in service
    assert service["read_only"] is True
    assert first.compose["x-muye-catalog-input-checksum"] == first.report["input_checksum"]


def test_sync_failure_preserves_previous_outputs(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    _write_build_record(root, descriptor)
    generator = AgentCatalogGenerator(CatalogPaths.for_workspace(root))
    generator.sync()
    catalog_before = (root / "config" / "generated" / "agent-catalog.json").read_bytes()
    compose_before = (root / "compose.agents.generated.yaml").read_bytes()
    (root / "agents" / "agent-product-handbook" / "prompts" / "system.md").write_text(
        "source drift", encoding="utf-8"
    )

    try:
        generator.sync()
    except ValueError as exc:
        assert "BuildRecord" in str(exc)
    else:
        raise AssertionError("源码漂移必须阻断整个 Catalog candidate")

    assert (root / "config" / "generated" / "agent-catalog.json").read_bytes() == catalog_before
    assert (root / "compose.agents.generated.yaml").read_bytes() == compose_before


def test_enabled_agent_requires_declared_resource_in_active_snapshot(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    _write_build_record(root, descriptor)
    snapshot_path = root / "config" / "generated" / "resource-snapshot.json"
    snapshot_path.unlink()

    try:
        AgentCatalogGenerator(CatalogPaths.for_workspace(root)).sync()
    except ValueError as exc:
        assert "Resource Snapshot" in str(exc)
    else:
        raise AssertionError("未发布 Resource 不能进入可部署 Catalog")


def test_build_writes_historical_record_and_version_pointer(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=False)
    commands: list[list[str]] = []

    def runner(command, *, cwd):
        commands.append(list(command))
        output = f"sha256:{'c' * 64}\n" if command[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    lifecycle = AgentLifecycle(
        LifecyclePaths.for_workspace(root),
        command_runner=runner,
        clock=lambda: FIXED_TIME,
    )
    record = lifecycle.build(
        "product-handbook",
        base_image=f"python:3.11-slim@sha256:{'a' * 64}",
    )

    pointer = root / "config" / "generated" / "builds" / descriptor.agent_id / "1.0.0.json"
    history = pointer.parent / "records" / f"{record.build_record_id}.json"
    assert load_yaml_model(pointer, AgentBuildRecordV1) == record
    assert load_yaml_model(history, AgentBuildRecordV1) == record
    assert [command[:2] for command in commands] == [
        [str(root / ".venv" / "bin" / "python"), "-m"],
        [str(root / ".venv" / "bin" / "python"), "-m"],
        ["docker", "build"],
        ["docker", "image"],
    ]
    assert record.base_image_digest == f"sha256:{'a' * 64}"
    assert record.image_digest == f"sha256:{'c' * 64}"


class _Control:
    def __init__(self, active, events: list[str]) -> None:
        self._active = active
        self.events = events

    def active(self):
        self.events.append("control-active")
        return self._active

    def submit(self, snapshot, *, expected_active_checksum, idempotency_key):
        self.events.append("control-submit")
        assert expected_active_checksum == self._active.catalog_checksum
        self._active = build_catalog_snapshot(
            [entry.model_copy(update={"status": "ACTIVE"}) for entry in snapshot.agents]
        )
        return {
            "status": "PENDING_MAIN_ACK",
            "catalog_revision": self._active.catalog_revision,
            "catalog_checksum": self._active.catalog_checksum,
        }

    def is_main_acked(self, *, revision, checksum):
        self.events.append("main-ack")
        return revision == self._active.catalog_revision and checksum == self._active.catalog_checksum


class _Smoke:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def smoke(self, agent_id: str) -> None:
        self.events.append("main-smoke")


def test_deploy_starts_service_before_candidate_and_waits_for_ack(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    _write_build_record(root, descriptor)
    events: list[str] = []

    def runner(command, *, cwd):
        if command[:3] == ["docker", "image", "inspect"]:
            events.append("image-inspect")
            return subprocess.CompletedProcess(command, 0, stdout=f"sha256:{'b' * 64}\n", stderr="")
        events.append("compose" if command[:2] == ["docker", "compose"] else "command")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    control = _Control(build_catalog_snapshot([]), events)
    lifecycle = AgentLifecycle(
        LifecyclePaths.for_workspace(root),
        command_runner=runner,
        control_client=control,
        main_smoke_client=_Smoke(events),
    )

    result = lifecycle.deploy("product-handbook", timeout_seconds=1)

    assert result["agent_id"] == descriptor.agent_id
    assert events == [
        "image-inspect",
        "compose",
        "control-active",
        "control-submit",
        "main-ack",
        "main-smoke",
    ]


def test_deploy_starts_every_service_in_the_catalog_candidate(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    _write_build_record(root, descriptor)
    generated = AgentCatalogGenerator(CatalogPaths.for_workspace(root)).sync()
    second = generated.snapshot.agents[0].model_copy(
        update={
            "agent_id": "agent_second_handbook",
            "service_name": "agent-second-handbook",
            "base_url": "http://agent-second-handbook:8000",
            "tool_name": "second_product_help",
        }
    )
    candidate = build_catalog_snapshot([generated.snapshot.agents[0], second])
    commands: list[list[str]] = []

    def runner(command, *, cwd):
        commands.append(list(command))
        output = f"sha256:{'b' * 64}\n" if command[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    events: list[str] = []
    lifecycle = AgentLifecycle(
        LifecyclePaths.for_workspace(root),
        command_runner=runner,
        control_client=_Control(build_catalog_snapshot([]), events),
        main_smoke_client=_Smoke(events),
    )
    lifecycle._catalog.sync = lambda: CatalogSyncResult(candidate, "input", {}, {}, False)  # type: ignore[method-assign]
    lifecycle._assert_aggregate_consistency = lambda _checksum: None  # type: ignore[method-assign]

    lifecycle.deploy("product-handbook", timeout_seconds=1)

    compose_command = next(command for command in commands if command[:2] == ["docker", "compose"])
    assert compose_command[-2:] == ["agent-product-handbook", "agent-second-handbook"]


def test_failed_rollback_restores_previous_build_pointer(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    current = _write_build_record(root, descriptor)
    historical = current.model_copy(
        update={"build_record_id": "build_rollback_failure", "image_digest": f"sha256:{'d' * 64}"}
    )
    history_path = root / "config" / "generated" / "builds" / descriptor.agent_id / "records" / f"{historical.build_record_id}.json"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(json.dumps(historical.model_dump(mode="json")), encoding="utf-8")

    def runner(command, *, cwd):
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{historical.image_digest}\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    class _FailingSmoke:
        def smoke(self, agent_id: str) -> None:
            raise RuntimeError("smoke failed")

    lifecycle = AgentLifecycle(
        LifecyclePaths.for_workspace(root),
        command_runner=runner,
        control_client=_Control(build_catalog_snapshot([]), []),
        main_smoke_client=_FailingSmoke(),
    )
    try:
        lifecycle.rollback("product-handbook", build_record_id=historical.build_record_id, timeout_seconds=1)
    except RuntimeError as exc:
        assert str(exc) == "smoke failed"
    else:
        raise AssertionError("失败 rollback 必须向调用方报告")

    pointer = root / "config" / "generated" / "builds" / descriptor.agent_id / "1.0.0.json"
    assert load_yaml_model(pointer, AgentBuildRecordV1) == current


def test_stop_removes_catalog_before_stopping_container(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    _write_build_record(root, descriptor)
    generated = AgentCatalogGenerator(CatalogPaths.for_workspace(root)).sync()
    active = build_catalog_snapshot(
        [entry.model_copy(update={"status": "ACTIVE"}) for entry in generated.snapshot.agents]
    )
    events: list[str] = []

    def runner(command, *, cwd):
        events.append("compose-stop")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    lifecycle = AgentLifecycle(
        LifecyclePaths.for_workspace(root),
        command_runner=runner,
        control_client=_Control(active, events),
    )

    lifecycle.stop("product-handbook", timeout_seconds=1)

    assert events == ["control-active", "control-submit", "main-ack", "compose-stop"]


def test_rollback_uses_matching_historical_record_and_replays_deploy(tmp_path: Path) -> None:
    root, descriptor = _workspace(tmp_path, enabled=True)
    current = _write_build_record(root, descriptor)
    historical = current.model_copy(
        update={
            "build_record_id": "build_rollback_fixture",
            "image_digest": f"sha256:{'d' * 64}",
            "built_at": "2026-07-30T01:02:03Z",
        }
    )
    history_path = (
        root
        / "config"
        / "generated"
        / "builds"
        / descriptor.agent_id
        / "records"
        / f"{historical.build_record_id}.json"
    )
    history_path.parent.mkdir(parents=True)
    history_path.write_text(json.dumps(historical.model_dump(mode="json")), encoding="utf-8")
    events: list[str] = []

    def runner(command, *, cwd):
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{historical.image_digest}\n", stderr="")
        events.append("compose")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    lifecycle = AgentLifecycle(
        LifecyclePaths.for_workspace(root),
        command_runner=runner,
        control_client=_Control(build_catalog_snapshot([]), events),
        main_smoke_client=_Smoke(events),
    )

    result = lifecycle.rollback(
        "product-handbook",
        build_record_id=historical.build_record_id,
        timeout_seconds=1,
    )

    pointer = root / "config" / "generated" / "builds" / descriptor.agent_id / "1.0.0.json"
    assert load_yaml_model(pointer, AgentBuildRecordV1) == historical
    assert result["build_record_id"] == historical.build_record_id
    assert result["image_digest"] == historical.image_digest
    assert events == ["compose", "control-active", "control-submit", "main-ack", "main-smoke"]


def test_agent_lifecycle_cli_parses_all_stage_five_commands() -> None:
    parser = build_parser()
    cases = (
        (["agent", "list"], "list"),
        (["agent", "sync", "--check"], "sync"),
        (["agent", "build", "product-handbook"], "build"),
        (["agent", "deploy", "product-handbook"], "deploy"),
        (["agent", "stop", "product-handbook"], "stop"),
        (
            ["agent", "rollback", "product-handbook", "--build-record", "build_rollback_fixture"],
            "rollback",
        ),
    )

    for arguments, expected in cases:
        assert parser.parse_args(arguments).agent_command == expected


def test_lifecycle_loads_its_dedicated_module_environment(tmp_path: Path, monkeypatch) -> None:
    """部署 CLI 不依赖根目录或其他服务目录的环境变量。"""

    root = tmp_path / "workspace"
    environment_file = root / "tools" / "agent_catalog" / ".env"
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text(
        "\n".join(
            (
                "MUYE_CONTROL_BASE_URL=http://control.example.test:9880",
                "MUYE_CONTROL_OPERATOR_TOKEN=operator-token",
                "MUYE_MAIN_BASE_URL=http://main.example.test:9860",
                "MUYE_MAIN_CALLER_TOKEN=caller-token",
                "MUYE_AGENT_SMOKE_USER_ID=smoke-user",
                "MUYE_COMPOSE_PROJECT_NAME=muye-test",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    for name in (
        "MUYE_CONTROL_BASE_URL",
        "MUYE_CONTROL_OPERATOR_TOKEN",
        "MUYE_MAIN_BASE_URL",
        "MUYE_MAIN_CALLER_TOKEN",
        "MUYE_AGENT_SMOKE_USER_ID",
        "MUYE_COMPOSE_PROJECT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    lifecycle = AgentLifecycle.for_workspace(root)

    assert lifecycle._control_client is not None
    assert lifecycle._main_smoke_client is not None
    assert lifecycle._runtime_environment["MUYE_COMPOSE_PROJECT_NAME"] == "muye-test"
