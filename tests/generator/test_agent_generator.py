"""阶段 3 生成器的确定性、安全边界和开发者接管回归测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from tools.agent_generator import AgentGenerator, GeneratorPaths
from tools.agent_generator.approvals import write_approval
from tools.agent_generator.checksums import canonical_checksum, read_source_tree
from tools.agent_generator.generator import _semver_sort_key
from tools.agent_generator.io import load_yaml_model
from tools.agent_generator.models import (
    AgentProfileInputV1,
    AgentProfileProposalV1,
    GenerationApprovalV1,
    KnowledgeGenerationInputV1,
)
from tools.cli import main as cli_main


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "generator" / "config"
TEMPLATES_ROOT = PROJECT_ROOT / "templates" / "agents"
TEMPLATE_DIRECTORY = TEMPLATES_ROOT / "react-knowledge" / "v1"
FIXED_TIME = datetime(2026, 7, 30, 1, 2, 3, tzinfo=timezone.utc)
LATER_TIME = datetime(2026, 7, 30, 1, 2, 4, tzinfo=timezone.utc)


def _generator(workspace_root: Path, *, timestamp: datetime = FIXED_TIME) -> AgentGenerator:
    """为测试创建隔离输出路径，但复用已提交的模板与输入 fixture。"""
    workspace_root.mkdir(parents=True, exist_ok=True)
    return AgentGenerator(
        GeneratorPaths(
            workspace_root=workspace_root,
            agents_root=workspace_root / "agents",
            config_root=FIXTURE_CONFIG_ROOT,
            templates_root=TEMPLATES_ROOT,
        ),
        clock=lambda: timestamp,
    )


def _generator_with_config(workspace_root: Path, config_root: Path) -> AgentGenerator:
    """为审批边界测试注入临时 config，但保留受控模板目录。"""
    return AgentGenerator(
        GeneratorPaths(
            workspace_root=workspace_root,
            agents_root=workspace_root / "agents",
            config_root=config_root,
            templates_root=TEMPLATES_ROOT,
        ),
        clock=lambda: FIXED_TIME,
    )


def test_generate_is_deterministic_and_matches_the_golden_source_checksum(tmp_path: Path) -> None:
    """同一输入在不同时钟下只能改变 provenance 时间，不能改变源码树。"""
    first = _generator(tmp_path / "first", timestamp=FIXED_TIME).generate(
        slug="product-handbook",
        knowledge_slug="product-handbook",
    )
    second = _generator(tmp_path / "second", timestamp=LATER_TIME).generate(
        slug="product-handbook",
        knowledge_slug="product-handbook",
    )

    expected_checksum = (
        PROJECT_ROOT / "tests" / "fixtures" / "generator" / "react-knowledge-v1.source-tree-checksum.txt"
    ).read_text(encoding="utf-8").strip()
    assert first.provenance.generated_source_tree_checksum == expected_checksum
    assert second.provenance.generated_source_tree_checksum == expected_checksum
    assert first.provenance.generated_at != second.provenance.generated_at
    assert read_source_tree(first.directory) == read_source_tree(second.directory)

    descriptor = first.descriptor
    assert descriptor.agent_id == "agent_product_handbook"
    assert descriptor.resources[0].resource_id == "kb.product_handbook"
    assert descriptor.source.provenance_file == ".muye-generation.json"
    assert ".dockerignore" in first.provenance.generated_files
    assert (first.directory / ".dockerignore").read_text(encoding="utf-8") == (
        TEMPLATE_DIRECTORY / ".dockerignore"
    ).read_text(encoding="utf-8")
    assert "scaffold" not in (first.directory / "agent.py").read_text(encoding="utf-8").lower()


def test_generated_agent_compiles_imports_and_runs_its_template_contract(tmp_path: Path) -> None:
    """生成的独立目录不得依赖 Scaffold 源码，并能通过 SDK capabilities smoke。"""
    target = _generator(tmp_path).generate(slug="product-handbook", knowledge_slug="product-handbook").directory

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=target,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    _load_generated_module(target / "agent.py")
    assert _generator(tmp_path).validate(slug="product-handbook").source_drift is False


@pytest.mark.parametrize(
    ("slug", "resource_id", "pipeline", "scope_field", "return_fields", "tool_budget"),
    [
        ("policy-library", "kb.policy_library", "hybrid", "tenant_id", ["title", "section", "citation_id"], 4),
        ("support-playbook", "kb.support_playbook", "dense", "product_line", ["title", "severity", "source_locator"], 7),
        ("api-reference", "kb.api_reference", "sparse", "api_version", ["endpoint", "method", "citation_id"], 5),
    ],
)
def test_template_generates_for_distinct_approved_knowledge_structures(
    tmp_path: Path,
    slug: str,
    resource_id: str,
    pipeline: str,
    scope_field: str,
    return_fields: list[str],
    tool_budget: int,
) -> None:
    """发布门禁覆盖不同 scope、字段和检索策略，而非只验证单一手册 fixture。"""
    config_root = tmp_path / "config"
    shutil.copytree(FIXTURE_CONFIG_ROOT, config_root)
    base_knowledge = yaml.safe_load((config_root / "knowledge" / "product-handbook.yaml").read_text(encoding="utf-8"))
    base_profile = yaml.safe_load((config_root / "agents" / "product-handbook.yaml").read_text(encoding="utf-8"))
    resource_checksum = canonical_checksum({"resource": resource_id})
    skill_checksum = canonical_checksum({"skill": slug, "pipeline": pipeline})
    base_knowledge.update(
        {
            "knowledge_slug": slug,
            "resource_id": resource_id,
            "resource_revision": f"resource/{slug}@1",
            "resource_checksum": resource_checksum,
            "skill_revision": f"skill_{slug.replace('-', '_')}@1",
            "skill_checksum": skill_checksum,
            "retrieval_pipeline": pipeline,
            "scope_filter_ref": f"scope/{slug}@1",
            "fixed_scope_filter": {"op": "eq", "field": scope_field, "value": resource_id},
            "allowed_filter_fields": [scope_field],
            "allowed_return_fields": return_fields,
            "tool_budget": tool_budget,
            "token_budget": 4096 + tool_budget * 256,
            "timeout_budget_seconds": 20 + tool_budget,
            "evaluation_set_ref": f"evaluation/{slug}@1",
        }
    )
    base_profile.update(
        {
            "agent_id": f"agent_{slug.replace('-', '_')}",
            "slug": slug,
            "profile_revision": f"profile/{slug}@1",
        }
    )
    base_profile["profile"].update(
        {
            "display_name": f"{slug} knowledge assistant",
            "description": f"Answers approved questions from {resource_id}.",
            "supported_intents": [f"{slug} lookup"],
            "instructions": f"Answer only from the approved {slug} knowledge resource.",
            "do_not_use_when": ["The requested information is outside the approved resource."],
        }
    )
    (config_root / "knowledge" / f"{slug}.yaml").write_text(
        yaml.safe_dump(base_knowledge, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (config_root / "agents" / f"{slug}.yaml").write_text(
        yaml.safe_dump(base_profile, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    knowledge = load_yaml_model(config_root / "knowledge" / f"{slug}.yaml", KnowledgeGenerationInputV1)
    profile = load_yaml_model(config_root / "agents" / f"{slug}.yaml", AgentProfileInputV1)
    for subject_type, revision, checksum in (
        ("resource", knowledge.resource_revision, knowledge.resource_checksum),
        ("skill", knowledge.skill_revision, knowledge.skill_checksum),
        ("profile", profile.profile_revision, canonical_checksum(profile.profile.model_dump(mode="json"))),
    ):
        write_approval(
            config_root,
            GenerationApprovalV1(
                schema_version="muye.ai/generation-approval/v1",
                subject_type=subject_type,
                subject_slug=slug,
                revision=revision,
                checksum=checksum,
                approved_at="2026-07-31T00:00:00Z",
                approved_by="release_reviewer",
            ),
        )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    generator = _generator_with_config(workspace_root, config_root)
    result = generator.generate(slug=slug, knowledge_slug=slug)

    assert result.descriptor.agent_id == profile.agent_id
    assert result.descriptor.resources[0].resource_id == resource_id
    assert generator.validate(slug=slug).source_drift is False
    _load_generated_module(result.directory / "agent.py")


def test_existing_directory_is_never_overwritten_after_developer_takeover(tmp_path: Path) -> None:
    """再次生成必须失败，并保留开发者改动的 Prompt 字节内容。"""
    generator = _generator(tmp_path)
    target = generator.generate(slug="product-handbook", knowledge_slug="product-handbook").directory
    prompt = target / "prompts" / "system.md"
    prompt.write_text("开发者自定义 Prompt。\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generator.generate(slug="product-handbook", knowledge_slug="product-handbook")

    assert prompt.read_text(encoding="utf-8") == "开发者自定义 Prompt。\n"


@pytest.mark.parametrize("slug", ["../outside", "/tmp/outside", "Product-Handbook", "main", "bad\x00slug"])
def test_invalid_or_reserved_slug_does_not_write_workspace(tmp_path: Path, slug: str) -> None:
    """路径类输入在创建 agents 或临时目录前被拒绝。"""
    with pytest.raises(ValueError):
        _generator(tmp_path).generate(slug=slug, knowledge_slug="product-handbook")

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_invalid_profile_proposal_does_not_write_workspace(tmp_path: Path) -> None:
    """代码围栏和模板语法等未经确认的 Proposal 内容不能成为生成 Prompt。"""
    config_root = tmp_path / "config"
    shutil.copytree(FIXTURE_CONFIG_ROOT, config_root)
    profile_path = config_root / "agents" / "product-handbook.yaml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "你是产品手册知识助手。仅依据检索工具返回的引用资料回答，并使用简体中文。",
            "```python\\nimport os",
        ),
        encoding="utf-8",
    )
    workspace_root = tmp_path / "workspace"
    generator = AgentGenerator(
        GeneratorPaths(
            workspace_root=workspace_root,
            agents_root=workspace_root / "agents",
            config_root=config_root,
            templates_root=TEMPLATES_ROOT,
        )
    )

    with pytest.raises(ValueError, match="代码围栏"):
        generator.generate(slug="product-handbook", knowledge_slug="product-handbook")

    assert not workspace_root.exists()


@pytest.mark.parametrize("subject_type", ("resource", "skill", "profile"))
def test_generate_requires_current_resource_skill_and_profile_approvals(
    tmp_path: Path,
    subject_type: str,
) -> None:
    """缺少任一可提交审批记录时，Generator 必须在创建输出目录前 fail closed。"""
    config_root = tmp_path / "config"
    shutil.copytree(FIXTURE_CONFIG_ROOT, config_root)
    shutil.rmtree(config_root / "approvals" / subject_type)
    workspace_root = tmp_path / "workspace"

    with pytest.raises(ValueError, match=subject_type + " 审批记录"):
        _generator_with_config(workspace_root, config_root).generate(
            slug="product-handbook",
            knowledge_slug="product-handbook",
        )

    assert not workspace_root.exists()


def test_approval_commands_persist_records_and_checksum_drift_blocks_generation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """审批 CLI 写入可提交记录；输入 checksum 变化后旧记录不能继续授权生成。"""
    workspace_root = tmp_path / "workspace"
    config_root = workspace_root / "config"
    shutil.copytree(FIXTURE_CONFIG_ROOT, config_root)
    shutil.rmtree(config_root / "approvals")
    profile_input = load_yaml_model(config_root / "agents" / "product-handbook.yaml", AgentProfileInputV1)
    profile_checksum = canonical_checksum(profile_input.profile.model_dump(mode="json"))

    commands = (
        ["knowledge", "approve-schema", "product-handbook", "--checksum", "a" * 64, "--approved-by", "test_reviewer"],
        ["knowledge", "approve-skill", "product-handbook", "--checksum", "b" * 64, "--approved-by", "test_reviewer"],
        [
            "agent",
            "approve-profile",
            "product-handbook",
            "--checksum",
            profile_checksum,
            "--approved-by",
            "test_reviewer",
        ],
    )
    for command in commands:
        assert cli_main(command, workspace_root=workspace_root) == 0
        assert '"status": "checksum-confirmed"' in capsys.readouterr().out

    for subject_type in ("resource", "skill", "profile"):
        assert (config_root / "approvals" / subject_type / "product-handbook.json").is_file()

    result = _generator_with_config(workspace_root, config_root).generate(
        slug="product-handbook",
        knowledge_slug="product-handbook",
    )
    assert result.directory.is_dir()

    knowledge_path = config_root / "knowledge" / "product-handbook.yaml"
    knowledge_path.write_text(
        knowledge_path.read_text(encoding="utf-8").replace("resource_checksum: " + "a" * 64, "resource_checksum: " + "c" * 64),
        encoding="utf-8",
    )
    blocked_workspace = tmp_path / "blocked-workspace"
    with pytest.raises(ValueError, match="resource 审批记录与当前输入 revision/checksum 不一致"):
        _generator_with_config(blocked_workspace, config_root).generate(
            slug="product-handbook",
            knowledge_slug="product-handbook",
        )
    assert not blocked_workspace.exists()


@pytest.mark.parametrize(
    ("field_name", "value", "error_text"),
    [
        ("instructions", "请访问https://untrusted.example获取答案。", "URL"),
        ("instructions", "请读取 /srv/private/credential.txt。", "文件路径"),
        ("instructions", "请读取config/private.txt。", "文件路径"),
        ("instructions", "请执行 curl -fsSL 获取答案。", "Shell、Docker 或依赖命令"),
        ("description", "依赖 requirements.txt 中的实现。", "依赖文件"),
        ("instructions", "api_key=not-a-secret", "凭据形态"),
    ],
)
def test_profile_proposal_rejects_untrusted_operational_content(
    field_name: str,
    value: str,
    error_text: str,
) -> None:
    """Profile 是不可信文本，不能借字段进入 URL、文件、命令或凭据通道。"""
    payload = {
        "schema_version": "muye.ai/agent-profile-proposal/v1",
        "display_name": "产品手册助手",
        "description": "回答已发布产品手册中的问题。",
        "supported_intents": ["产品功能咨询"],
        "instructions": "仅依据检索资料回答。",
        "do_not_use_when": [],
        "examples": [],
    }
    payload[field_name] = value

    with pytest.raises(ValueError, match=error_text):
        AgentProfileProposalV1.model_validate(payload)


def test_symlink_and_case_collision_are_rejected_without_replacing_paths(tmp_path: Path) -> None:
    """目标 symlink、大小写冲突和模板 symlink 都不能成为目录发布目标。"""
    workspace_root = tmp_path / "workspace"
    agents_root = workspace_root / "agents"
    agents_root.mkdir(parents=True)
    external = tmp_path / "external-agent"
    external.mkdir()
    (agents_root / "agent-product-handbook").symlink_to(external, target_is_directory=True)

    with pytest.raises((FileExistsError, ValueError)):
        _generator(workspace_root).generate(slug="product-handbook", knowledge_slug="product-handbook")
    assert list(external.iterdir()) == []

    (agents_root / "agent-product-handbook").unlink()
    (agents_root / "agent-Product-Handbook").mkdir()
    with pytest.raises(FileExistsError, match="大小写冲突"):
        _generator(workspace_root).generate(slug="product-handbook", knowledge_slug="product-handbook")

    shutil.rmtree(agents_root / "agent-Product-Handbook")
    copied_templates = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT, copied_templates)
    (copied_templates / "react-knowledge" / "v1" / "unsafe-link").symlink_to(
        copied_templates / "react-knowledge" / "v1" / "agent.py.tmpl"
    )
    generator = AgentGenerator(
        GeneratorPaths(
            workspace_root=workspace_root,
            agents_root=agents_root,
            config_root=FIXTURE_CONFIG_ROOT,
            templates_root=copied_templates,
        )
    )
    with pytest.raises(ValueError, match="符号链接"):
        generator.generate(slug="product-handbook", knowledge_slug="product-handbook")
    assert list(agents_root.iterdir()) == []


def test_validate_reports_source_drift_and_diff_is_read_only(tmp_path: Path) -> None:
    """开发者合法修改会提示 drift；diff 只比较内存，不回写任意来源文件。"""
    generator = _generator(tmp_path)
    target = generator.generate(slug="product-handbook", knowledge_slug="product-handbook").directory
    assert generator.validate(slug="product-handbook").source_drift is False
    assert generator.diff(slug="product-handbook", template="source").has_changes is False

    prompt = target / "prompts" / "system.md"
    prompt.write_text("开发者自定义 Prompt。\n", encoding="utf-8")
    before = {path.relative_to(target).as_posix(): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    report = generator.validate(slug="product-handbook")
    result = generator.diff(slug="product-handbook", template="latest")
    after = {path.relative_to(target).as_posix(): path.read_bytes() for path in target.rglob("*") if path.is_file()}

    assert report.is_valid is True
    assert report.source_drift is True
    assert result.has_changes is True
    assert "current/prompts/system.md" in result.text
    assert before == after


def test_validate_allows_descriptor_takeover_but_freezes_identity(tmp_path: Path) -> None:
    """版本等 descriptor 接管字段可演进，稳定 Agent 身份和工具名不可被替换。"""
    generator = _generator(tmp_path)
    target = generator.generate(slug="product-handbook", knowledge_slug="product-handbook").directory
    descriptor_path = target / "agent.yaml"
    descriptor_path.write_text(
        descriptor_path.read_text(encoding="utf-8").replace(
            '\nversion: "1.0.0"\n', '\nversion: "1.0.1"\n'
        ),
        encoding="utf-8",
    )

    report = generator.validate(slug="product-handbook")

    assert report.is_valid is True
    assert report.source_drift is True

    descriptor_path.write_text(
        descriptor_path.read_text(encoding="utf-8").replace(
            'agent_id: "agent_product_handbook"', 'agent_id: "agent_replaced_identity"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不可变身份字段：agent_id"):
        generator.validate(slug="product-handbook")


@pytest.mark.parametrize(
    ("tamper_kind", "error_text"),
    [
        ("sdk_version", "sdk_version"),
        ("profile_checksum", "profile_checksum"),
        ("generated_files", "generated_files"),
    ],
)
def test_validate_rejects_tampered_provenance(tamper_kind: str, error_text: str, tmp_path: Path) -> None:
    """来源字段和完整生成清单必须与可重放 recipe 精确匹配。"""
    generator = _generator(tmp_path)
    target = generator.generate(slug="product-handbook", knowledge_slug="product-handbook").directory
    provenance_path = target / ".muye-generation.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if tamper_kind == "sdk_version":
        provenance["sdk_version"] = "2.0.1"
    elif tamper_kind == "profile_checksum":
        provenance["profile_checksum"] = "c" * 64
    else:
        provenance["generated_files"].remove("Dockerfile")
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=error_text):
        generator.validate(slug="product-handbook")


def test_semver_template_selection_handles_numeric_prerelease_identifiers() -> None:
    """SemVer 的数字 prerelease 按数值比较，并在同层级低于非数字标识。"""
    assert _semver_sort_key("1.0.0-alpha.10") > _semver_sort_key("1.0.0-alpha.2")
    assert _semver_sort_key("1.0.0-alpha.1") < _semver_sort_key("1.0.0-alpha.beta")
    assert _semver_sort_key("1.0.0") > _semver_sort_key("1.0.0-rc.1")
    assert _semver_sort_key("1.0.0+build.1") == _semver_sort_key("1.0.0+build.2")


def test_cli_dispatches_knowledge_confirmation_and_preserves_future_phase_boundary(tmp_path: Path, capsys) -> None:
    """统一 CLI 的知识检查可确认当前 checksum，构建/评测在阶段 4 前明确失败。"""
    workspace_root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_CONFIG_ROOT, workspace_root / "config")
    checksum = "a" * 64

    assert cli_main(["knowledge", "analyze", "product-handbook"], workspace_root=workspace_root) == 0
    assert '"status": "validated-input"' in capsys.readouterr().out
    assert (
        cli_main(
            [
                "knowledge",
                "approve-schema",
                "product-handbook",
                "--checksum",
                checksum,
                "--approved-by",
                "test_reviewer",
            ],
            workspace_root=workspace_root,
        )
        == 0
    )
    assert cli_main(["knowledge", "build", "product-handbook"], workspace_root=workspace_root) == 2
    assert not (workspace_root / "agents").exists()


def _load_generated_module(path: Path) -> object:
    """从临时生成目录导入 agent.py，避免测试依赖 Scaffold 项目模块路径。"""
    module_name = "generated_product_handbook_agent"
    specification = importlib.util.spec_from_file_location(module_name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module
