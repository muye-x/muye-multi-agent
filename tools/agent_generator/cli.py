"""`muye.sh agent` 子命令的参数解析与稳定 JSON 输出。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Sequence

from .approvals import write_approval
from .checksums import canonical_checksum
from .generator import AgentGenerator, GeneratorPaths
from .io import assert_path_within, load_yaml_model
from .models import AgentProfileInputV1, GenerationApprovalV1
from tools.agent_catalog.cli import LIFECYCLE_COMMANDS, add_agent_lifecycle_parsers, run_agent_lifecycle_command


def add_agent_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册 `agent generate`、`validate` 与只读 `diff` 命令。"""
    parser = subparsers.add_parser("agent", help="生成、校验或比较模板 Agent")
    commands = parser.add_subparsers(dest="agent_command", required=True)

    generate = commands.add_parser("generate", help="从确认的知识和 Profile 配置一次性生成 Agent")
    generate.add_argument("slug", help="目标 Agent slug")
    generate.add_argument("--knowledge", required=True, dest="knowledge_slug", help="知识配置 slug")

    approve_profile = commands.add_parser("approve-profile", help="确认当前 Agent Profile checksum")
    approve_profile.add_argument("slug", help="目标 Agent slug")
    approve_profile.add_argument("--checksum", required=True, help="审阅后确认的 Profile checksum")
    approve_profile.add_argument("--approved-by", required=True, help="确认人的稳定逻辑标识")

    validate = commands.add_parser("validate", help="校验已生成 Agent 的描述符和来源记录")
    validate.add_argument("slug", help="目标 Agent slug")

    diff = commands.add_parser("diff", help="只读比较当前 Agent 与模板重渲染结果")
    diff.add_argument("slug", help="目标 Agent slug")
    diff.add_argument("--template", choices=("latest", "source"), default="latest", help="比较的模板版本")

    add_agent_lifecycle_parsers(commands)

    parser.set_defaults(handler=run_agent_command)


def run_agent_command(arguments: argparse.Namespace, workspace_root: Path) -> int:
    """执行 Agent 子命令；输出不包含配置正文、环境变量或其他敏感值。"""
    generator = AgentGenerator(GeneratorPaths.for_workspace(workspace_root))
    if arguments.agent_command == "generate":
        result = generator.generate(slug=arguments.slug, knowledge_slug=arguments.knowledge_slug)
        _print_json(
            {
                "directory": result.directory.relative_to(workspace_root).as_posix(),
                "generated_source_tree_checksum": result.provenance.generated_source_tree_checksum,
                "status": "generated",
            }
        )
        return 0
    if arguments.agent_command == "approve-profile":
        return _approve_profile(
            workspace_root,
            slug=arguments.slug,
            submitted_checksum=arguments.checksum,
            approved_by=arguments.approved_by,
        )
    if arguments.agent_command == "validate":
        report = generator.validate(slug=arguments.slug)
        _print_json(
            {
                "directory": report.directory.relative_to(workspace_root).as_posix(),
                "generated_source_tree_checksum": report.provenance.generated_source_tree_checksum,
                "missing_generated_files": list(report.missing_generated_files),
                "source_drift": report.source_drift,
                "status": "valid" if report.is_valid else "invalid",
            }
        )
        return 0 if report.is_valid else 1
    if arguments.agent_command == "diff":
        result = generator.diff(slug=arguments.slug, template=arguments.template)
        if result.text:
            print(result.text, end="" if result.text.endswith("\n") else "\n")
        return 1 if result.has_changes else 0
    if arguments.agent_command in LIFECYCLE_COMMANDS:
        return run_agent_lifecycle_command(arguments, workspace_root)
    raise ValueError(f"不支持的 agent 子命令：{arguments.agent_command}")


def _print_json(value: object) -> None:
    """输出稳定的机器可读结果，便于 CI 和开发者脚本解析。"""
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _approve_profile(
    workspace_root: Path,
    *,
    slug: str,
    submitted_checksum: str,
    approved_by: str,
) -> int:
    """确认受限 Profile，令 Generator 能将该确认与其 revision/checksum 精确绑定。"""
    _validate_slug(slug)
    config_root = workspace_root / "config"
    profile_path = config_root / "agents" / f"{slug}.yaml"
    if profile_path.is_symlink() or not profile_path.is_file():
        raise ValueError(f"Agent Profile 配置不存在、不是普通文件或是符号链接：{profile_path}")
    assert_path_within(profile_path, config_root, description="Agent Profile 配置")
    profile_input = load_yaml_model(profile_path, AgentProfileInputV1)
    if profile_input.slug != slug:
        raise ValueError("Agent Profile 配置中的 slug 必须与命令参数一致")
    checksum = canonical_checksum(profile_input.profile.model_dump(mode="json"))
    if submitted_checksum != checksum:
        raise ValueError("Profile checksum 不匹配，拒绝确认当前 Agent 输入")
    approval = GenerationApprovalV1(
        schema_version="muye.ai/generation-approval/v1",
        subject_type="profile",
        subject_slug=slug,
        revision=profile_input.profile_revision,
        checksum=checksum,
        approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        approved_by=approved_by,
    )
    path = write_approval(config_root, approval)
    _print_json(
        {
            "approval": path.relative_to(workspace_root).as_posix(),
            "agent_slug": slug,
            "status": "checksum-confirmed",
            "type": "profile",
        }
    )
    return 0


def _validate_slug(slug: str) -> None:
    """在通过 CLI 构造 Profile 配置路径前复用 Generator 的 slug 约束。"""
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None or slug == "main":
        raise ValueError("Agent slug 必须是非 main 的小写字母、数字和单连字符组成的 slug")


def main(argv: Sequence[str] | None = None) -> int:
    """提供独立模块入口，便于在本地直接调试 Agent CLI。"""
    parser = argparse.ArgumentParser(prog="agent-generator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_agent_parser(subparsers)
    arguments = parser.parse_args(argv)
    return arguments.handler(arguments, Path.cwd())


if __name__ == "__main__":  # pragma: no cover - 由统一 tools.cli 覆盖
    raise SystemExit(main())
