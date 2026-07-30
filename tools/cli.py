"""Scaffold 统一开发工具入口；Shell wrapper 不承载业务状态机。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .agent_generator.cli import add_agent_parser
from .knowledge_cli import add_knowledge_parser


def build_parser() -> argparse.ArgumentParser:
    """构建统一 CLI，并使各子命令保持独立实现和可测试边界。"""
    parser = argparse.ArgumentParser(prog="muye.sh")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_knowledge_parser(subparsers)
    add_agent_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None, *, workspace_root: Path | None = None) -> int:
    """解析命令并把受控 Scaffold 根目录传入具体业务工具。"""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    root = (workspace_root or Path.cwd()).resolve(strict=True)
    try:
        return arguments.handler(arguments, root)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
