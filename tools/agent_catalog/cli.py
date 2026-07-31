"""Catalog 与部署生命周期的 `muye.sh agent` CLI 接线。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .generator import AgentCatalogGenerator, CatalogPaths
from .lifecycle import AgentLifecycle


LIFECYCLE_COMMANDS = frozenset({"list", "sync", "build", "deploy", "stop", "rollback"})


def add_agent_lifecycle_parsers(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """注册阶段 5 的只读发现、构建、同步、部署、下线和回滚命令。"""
    commands.add_parser("list", help="列出 Descriptor、部署开关和当前 BuildRecord")

    sync = commands.add_parser("sync", help="确定性生成 Catalog、Compose aggregate 和报告")
    sync.add_argument("--check", action="store_true", help="只读检查生成物漂移")

    build = commands.add_parser("build", help="测试并构建 digest 固定的 Agent 镜像")
    build.add_argument("slug", help="目标 Agent slug")
    build.add_argument("--base-image", help="覆盖 MUYE_AGENT_BASE_IMAGE，必须包含 @sha256 digest")

    deploy = commands.add_parser("deploy", help="启动 Agent，激活 Catalog 并等待 Main ACK")
    deploy.add_argument("slug", help="目标 Agent slug")
    deploy.add_argument("--timeout", type=float, default=60.0, help="等待 Main ACK 的秒数")

    stop = commands.add_parser("stop", help="先移除 Catalog 并等待 Main ACK，再停止容器")
    stop.add_argument("slug", help="目标 Agent slug")
    stop.add_argument("--timeout", type=float, default=60.0, help="等待 Main ACK 的秒数")

    rollback = commands.add_parser("rollback", help="切换到已验证 BuildRecord 并执行完整部署")
    rollback.add_argument("slug", help="目标 Agent slug")
    rollback.add_argument("--build-record", required=True, dest="build_record_id", help="历史 BuildRecord ID")
    rollback.add_argument("--timeout", type=float, default=60.0, help="等待 Main ACK 的秒数")


def run_agent_lifecycle_command(arguments: argparse.Namespace, workspace_root: Path) -> int:
    """执行阶段 5 生命周期命令并输出稳定、无 secret 的 JSON。"""
    if arguments.agent_command == "sync":
        result = AgentCatalogGenerator(CatalogPaths.for_workspace(workspace_root)).sync(check=arguments.check)
        _print_json(
            {
                "catalog_checksum": result.snapshot.catalog_checksum,
                "catalog_revision": result.snapshot.catalog_revision,
                "changed": result.changed,
                "input_checksum": result.input_checksum,
                "status": "current" if arguments.check else "synchronized",
            }
        )
        return 0

    lifecycle = AgentLifecycle.for_workspace(workspace_root)
    if arguments.agent_command == "list":
        _print_json({"agents": lifecycle.list_agents(), "status": "ok"})
        return 0
    if arguments.agent_command == "build":
        record = lifecycle.build(arguments.slug, base_image=arguments.base_image)
        _print_json(
            {
                "agent_id": record.agent_id,
                "build_record_id": record.build_record_id,
                "image_digest": record.image_digest,
                "status": "built",
            }
        )
        return 0
    if arguments.agent_command == "deploy":
        response = lifecycle.deploy(arguments.slug, timeout_seconds=arguments.timeout)
        _print_json({**response, "status": "deployed"})
        return 0
    if arguments.agent_command == "stop":
        response = lifecycle.stop(arguments.slug, timeout_seconds=arguments.timeout)
        _print_json({**response, "status": "stopped"})
        return 0
    if arguments.agent_command == "rollback":
        response = lifecycle.rollback(
            arguments.slug,
            build_record_id=arguments.build_record_id,
            timeout_seconds=arguments.timeout,
        )
        _print_json({**response, "status": "rolled-back"})
        return 0
    raise ValueError(f"不支持的 Agent 生命周期命令：{arguments.agent_command}")


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
