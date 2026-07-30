"""`muye.sh agent` 子命令的参数解析与稳定 JSON 输出。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .generator import AgentGenerator, GeneratorPaths


def add_agent_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册 `agent generate`、`validate` 与只读 `diff` 命令。"""
    parser = subparsers.add_parser("agent", help="生成、校验或比较模板 Agent")
    commands = parser.add_subparsers(dest="agent_command", required=True)

    generate = commands.add_parser("generate", help="从确认的知识和 Profile 配置一次性生成 Agent")
    generate.add_argument("slug", help="目标 Agent slug")
    generate.add_argument("--knowledge", required=True, dest="knowledge_slug", help="知识配置 slug")

    validate = commands.add_parser("validate", help="校验已生成 Agent 的描述符和来源记录")
    validate.add_argument("slug", help="目标 Agent slug")

    diff = commands.add_parser("diff", help="只读比较当前 Agent 与模板重渲染结果")
    diff.add_argument("slug", help="目标 Agent slug")
    diff.add_argument("--template", choices=("latest", "source"), default="latest", help="比较的模板版本")

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
    raise ValueError(f"不支持的 agent 子命令：{arguments.agent_command}")


def _print_json(value: object) -> None:
    """输出稳定的机器可读结果，便于 CI 和开发者脚本解析。"""
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    """提供独立模块入口，便于在本地直接调试 Agent CLI。"""
    parser = argparse.ArgumentParser(prog="agent-generator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_agent_parser(subparsers)
    arguments = parser.parse_args(argv)
    return arguments.handler(arguments, Path.cwd())


if __name__ == "__main__":  # pragma: no cover - 由统一 tools.cli 覆盖
    raise SystemExit(main())
