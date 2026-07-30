"""阶段 3 的 `muye.sh knowledge` 输入检查与显式确认边界。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

from .agent_generator.io import assert_path_within, load_yaml_model
from .agent_generator.models import KnowledgeGenerationInputV1


def add_knowledge_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册阶段 3 可安全执行的知识输入检查和 checksum 确认命令。"""
    parser = subparsers.add_parser("knowledge", help="检查已确认的知识生成输入")
    commands = parser.add_subparsers(dest="knowledge_command", required=True)

    analyze = commands.add_parser("analyze", help="校验知识逻辑输入并显示其已确认 checksum")
    analyze.add_argument("slug", help="知识配置 slug")

    approve_schema = commands.add_parser("approve-schema", help="确认当前 Resource checksum")
    approve_schema.add_argument("slug", help="知识配置 slug")
    approve_schema.add_argument("--checksum", required=True, help="审阅后确认的 Resource checksum")

    approve_skill = commands.add_parser("approve-skill", help="确认当前 Retrieval Skill checksum")
    approve_skill.add_argument("slug", help="知识配置 slug")
    approve_skill.add_argument("--checksum", required=True, help="审阅后确认的 Skill checksum")

    for command_name, help_text in (
        ("build", "阶段 4 负责提交知识构建 Job"),
        ("evaluate", "阶段 4 负责运行检索评测"),
    ):
        command = commands.add_parser(command_name, help=help_text)
        command.add_argument("slug", help="知识配置 slug")

    parser.set_defaults(handler=run_knowledge_command)


def run_knowledge_command(arguments: argparse.Namespace, workspace_root: Path) -> int:
    """处理非破坏性知识命令；构建与评测在对应阶段前 fail closed。"""
    knowledge = _load_knowledge_input(workspace_root, arguments.slug)
    if arguments.knowledge_command == "analyze":
        _print_json(
            {
                "knowledge_slug": knowledge.knowledge_slug,
                "resource_checksum": knowledge.resource_checksum,
                "skill_checksum": knowledge.skill_checksum,
                "status": "validated-input",
            }
        )
        return 0
    if arguments.knowledge_command == "approve-schema":
        return _confirm_checksum(
            arguments.checksum,
            knowledge.resource_checksum,
            label="Resource",
            knowledge_slug=knowledge.knowledge_slug,
        )
    if arguments.knowledge_command == "approve-skill":
        return _confirm_checksum(
            arguments.checksum,
            knowledge.skill_checksum,
            label="Retrieval Skill",
            knowledge_slug=knowledge.knowledge_slug,
        )
    if arguments.knowledge_command in {"build", "evaluate"}:
        raise ValueError(
            f"knowledge {arguments.knowledge_command} 尚未实现：它依赖阶段 4 的 Knowledge Worker、Job 和评测契约"
        )
    raise ValueError(f"不支持的 knowledge 子命令：{arguments.knowledge_command}")


def _load_knowledge_input(workspace_root: Path, slug: str) -> KnowledgeGenerationInputV1:
    """读取路径受限的知识生成输入，命令参数不能控制任意文件系统路径。"""
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None:
        raise ValueError("知识 slug 必须是小写字母、数字和单连字符组成的 slug")
    config_root = workspace_root / "config"
    path = config_root / "knowledge" / f"{slug}.yaml"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"知识配置不存在、不是普通文件或是符号链接：{path}")
    assert_path_within(path, config_root, description="知识配置")
    knowledge = load_yaml_model(path, KnowledgeGenerationInputV1)
    if knowledge.knowledge_slug != slug:
        raise ValueError("知识配置中的 knowledge_slug 必须与命令参数一致")
    return knowledge


def _confirm_checksum(submitted: str, expected: str, *, label: str, knowledge_slug: str) -> int:
    """确认命令必须精确匹配当前版本 checksum，避免旧审阅确认新输入。"""
    if submitted != expected:
        raise ValueError(f"{label} checksum 不匹配，拒绝确认当前知识输入")
    _print_json({"knowledge_slug": knowledge_slug, "status": "checksum-confirmed", "type": label})
    return 0


def _print_json(value: object) -> None:
    """输出稳定、无配置正文的结果。"""
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    """提供独立模块入口，便于本地调试知识输入校验。"""
    parser = argparse.ArgumentParser(prog="knowledge-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_knowledge_parser(subparsers)
    arguments = parser.parse_args(argv)
    return arguments.handler(arguments, Path.cwd())


if __name__ == "__main__":  # pragma: no cover - 由统一 tools.cli 覆盖
    raise SystemExit(main())
