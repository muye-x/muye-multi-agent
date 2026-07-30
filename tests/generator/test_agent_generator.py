"""阶段 3 生成器的确定性、安全边界和开发者接管回归测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tools.agent_generator import AgentGenerator, GeneratorPaths
from tools.agent_generator.checksums import read_source_tree
from tools.cli import main as cli_main


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "generator" / "config"
TEMPLATES_ROOT = PROJECT_ROOT / "templates" / "agents"
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
    assert "scaffold" not in (first.directory / "agent.py").read_text(encoding="utf-8").lower()


def test_generated_agent_compiles_imports_and_runs_its_template_contract(tmp_path: Path) -> None:
    """生成的独立目录不得依赖 Scaffold 源码，并能通过 SDK capabilities smoke。"""
    target = _generator(tmp_path).generate(slug="product-handbook", knowledge_slug="product-handbook").directory

    result = subprocess.run(
        [str(PROJECT_ROOT / ".venv" / "bin" / "python"), "-m", "pytest", "-q", "tests"],
        cwd=target,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    _load_generated_module(target / "agent.py")
    assert _generator(tmp_path).validate(slug="product-handbook").source_drift is False


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


def test_cli_dispatches_knowledge_confirmation_and_preserves_future_phase_boundary(tmp_path: Path, capsys) -> None:
    """统一 CLI 的知识检查可确认当前 checksum，构建/评测在阶段 4 前明确失败。"""
    workspace_root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_CONFIG_ROOT, workspace_root / "config")
    checksum = "a" * 64

    assert cli_main(["knowledge", "analyze", "product-handbook"], workspace_root=workspace_root) == 0
    assert '"status": "validated-input"' in capsys.readouterr().out
    assert (
        cli_main(
            ["knowledge", "approve-schema", "product-handbook", "--checksum", checksum],
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
