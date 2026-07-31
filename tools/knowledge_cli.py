"""`muye.sh knowledge` 的阶段 3 逻辑输入与阶段 4 构建 Job 命令。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Literal, Sequence

from .agent_generator.approvals import ApprovalSubjectType, write_approval
from .agent_generator.io import assert_path_within, load_yaml_model
from .agent_generator.models import GenerationApprovalV1, KnowledgeGenerationInputV1
from .knowledge_pipeline.worker import KnowledgeWorker


def add_knowledge_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册阶段 3 可安全执行的知识输入检查和 checksum 确认命令。"""
    parser = subparsers.add_parser("knowledge", help="检查已确认的知识生成输入")
    commands = parser.add_subparsers(dest="knowledge_command", required=True)

    analyze = commands.add_parser("analyze", help="校验知识逻辑输入并显示其已确认 checksum")
    analyze.add_argument("slug", help="知识配置 slug")

    approve_schema = commands.add_parser("approve-schema", help="确认当前 Resource checksum")
    approve_schema.add_argument("slug", help="知识配置 slug")
    approve_schema.add_argument("--checksum", required=True, help="审阅后确认的 Resource checksum")
    approve_schema.add_argument("--approved-by", required=True, help="确认人的稳定逻辑标识")

    approve_skill = commands.add_parser("approve-skill", help="确认当前 Retrieval Skill checksum")
    approve_skill.add_argument("slug", help="知识配置 slug")
    approve_skill.add_argument("--checksum", required=True, help="审阅后确认的 Skill checksum")
    approve_skill.add_argument("--approved-by", required=True, help="确认人的稳定逻辑标识")

    propose = commands.add_parser("propose-schema", help="解析阶段 4 源文件并生成 Schema Proposal")
    propose.add_argument("slug", help="知识源配置 slug")
    propose.add_argument("--import-root", required=True, type=Path, help="受控本地知识导入根")
    propose.add_argument("--ocr-available", action="store_true", help="声明已启用 OCR Worker capability")

    approve_proposal = commands.add_parser("approve-proposal", help="确认当前阶段 4 Schema Proposal checksum")
    approve_proposal.add_argument("slug", help="知识源配置 slug")
    approve_proposal.add_argument("--checksum", required=True, help="审阅后确认的 Proposal checksum")
    approve_proposal.add_argument("--approved-by", required=True, help="确认人的稳定逻辑标识")

    build = commands.add_parser("build", help="构建不可变 Milvus Collection 与待评测 Manifest")
    build.add_argument("slug", help="知识源配置 slug")
    build.add_argument("--import-root", type=Path, help="受控本地知识导入根")
    build.add_argument("--ocr-available", action="store_true", help="声明已启用 OCR Worker capability")
    build.add_argument("--llm-base-url", help="受信任 muye-llm 内网地址；默认读取环境")
    build.add_argument("--milvus-uri", help="受信任 Milvus 地址；默认读取环境")

    evaluate = commands.add_parser("evaluate", help="运行固定 RAG 评测，并在门禁通过后激活快照")
    evaluate.add_argument("slug", help="知识源配置 slug")
    evaluate.add_argument("--data-url", help="候选 muye-data 只读地址；默认读取环境")

    status = commands.add_parser("status", help="查看本地 Knowledge Job 状态")
    status.add_argument("job_id", help="知识 Job ID")

    cancel = commands.add_parser("cancel", help="请求协作式取消本地 Knowledge Job")
    cancel.add_argument("job_id", help="知识 Job ID")

    retry = commands.add_parser("retry", help="同步重放失败或取消 Job，生成并执行新的 attempt")
    retry.add_argument("job_id", help="知识 Job ID")
    retry.add_argument("--import-root", type=Path, help="重试 build Job 所需的受控本地知识导入根")
    retry.add_argument("--ocr-available", action="store_true", help="声明重试 build Job 使用 OCR Worker capability")
    retry.add_argument("--llm-base-url", help="重试 build Job 的受信任 muye-llm 内网地址")
    retry.add_argument("--milvus-uri", help="重试 build Job 的受信任 Milvus 地址")
    retry.add_argument("--data-url", help="重试 evaluate Job 的候选 muye-data 只读地址")

    parser.set_defaults(handler=run_knowledge_command)


def run_knowledge_command(arguments: argparse.Namespace, workspace_root: Path) -> int:
    """分派阶段 3 的离线确认与阶段 4 的受控 Knowledge Worker Job。"""
    if arguments.knowledge_command in {
        "propose-schema",
        "approve-proposal",
        "build",
        "evaluate",
        "status",
        "cancel",
        "retry",
    }:
        return _run_phase4_command(arguments, workspace_root)
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
        return _confirm_knowledge_input(
            workspace_root,
            arguments.checksum,
            approved_by=arguments.approved_by,
            subject_type="resource",
            expected_checksum=knowledge.resource_checksum,
            revision=knowledge.resource_revision,
            knowledge_slug=knowledge.knowledge_slug,
        )
    if arguments.knowledge_command == "approve-skill":
        return _confirm_knowledge_input(
            workspace_root,
            arguments.checksum,
            approved_by=arguments.approved_by,
            subject_type="skill",
            expected_checksum=knowledge.skill_checksum,
            revision=knowledge.skill_revision,
            knowledge_slug=knowledge.knowledge_slug,
        )
    raise ValueError(f"不支持的 knowledge 子命令：{arguments.knowledge_command}")


def _run_phase4_command(arguments: argparse.Namespace, workspace_root: Path) -> int:
    """执行阶段 4 写侧命令；仍不向 `muye-data` 暴露任何写入 HTTP API。"""
    worker = KnowledgeWorker(workspace_root)
    command = arguments.knowledge_command
    if command == "propose-schema":
        result = worker.propose_schema(
            slug=arguments.slug,
            import_root=arguments.import_root,
            ocr_available=arguments.ocr_available,
        )
        _print_json(
            {
                "proposal": result.proposal_path.relative_to(workspace_root).as_posix(),
                "proposal_checksum": result.proposal.proposal_checksum,
                "proposal_revision": result.proposal.proposal_revision,
                "status": "proposal-created",
            }
        )
        return 0
    if command == "approve-proposal":
        path = worker.approve_schema(
            slug=arguments.slug,
            checksum=arguments.checksum,
            approved_by=arguments.approved_by,
        )
        _print_json(
            {
                "approval": path.relative_to(workspace_root).as_posix(),
                "status": "proposal-confirmed",
            }
        )
        return 0
    if command == "build":
        if arguments.import_root is None:
            raise ValueError("knowledge build 必须显式提供 --import-root")
        result = worker.build(
            slug=arguments.slug,
            import_root=arguments.import_root,
            ocr_available=arguments.ocr_available,
            llm_base_url=arguments.llm_base_url,
            milvus_uri=arguments.milvus_uri,
        )
        _print_json(
            {
                "job_id": result.job_id,
                "manifest": (
                    result.manifest.resource_checksum if result.manifest is not None else None
                ),
                "report": (
                    result.report_path.relative_to(workspace_root).as_posix()
                    if result.report_path is not None
                    else None
                ),
                "status": worker.status(result.job_id)["status"],
            }
        )
        return 0 if worker.status(result.job_id)["status"] == "SUCCEEDED" else 2
    if command == "evaluate":
        result = worker.evaluate(slug=arguments.slug, data_base_url=arguments.data_url)
        _print_json(
            {
                "job_id": result.job_id,
                "report": (
                    result.report_path.relative_to(workspace_root).as_posix()
                    if result.report_path is not None
                    else None
                ),
                "status": worker.status(result.job_id)["status"],
            }
        )
        return 0 if worker.status(result.job_id)["status"] == "SUCCEEDED" else 2
    if command == "status":
        _print_json(worker.status(arguments.job_id))
        return 0
    if command == "cancel":
        _print_json(worker.cancel(arguments.job_id))
        return 0
    if command == "retry":
        result = worker.retry_job(
            arguments.job_id,
            import_root=arguments.import_root,
            ocr_available=arguments.ocr_available,
            llm_base_url=arguments.llm_base_url,
            milvus_uri=arguments.milvus_uri,
            data_base_url=arguments.data_url,
        )
        _print_json(
            {
                "job_id": result.job_id,
                "report": (
                    result.report_path.relative_to(workspace_root).as_posix()
                    if result.report_path is not None
                    else None
                ),
                "status": worker.status(result.job_id)["status"],
            }
        )
        return 0 if worker.status(result.job_id)["status"] == "SUCCEEDED" else 2
    raise ValueError(f"不支持的阶段 4 knowledge 子命令：{command}")


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


def _confirm_knowledge_input(
    workspace_root: Path,
    submitted_checksum: str,
    *,
    approved_by: str,
    subject_type: ApprovalSubjectType,
    expected_checksum: str,
    revision: str,
    knowledge_slug: str,
) -> int:
    """写入精确绑定 revision/checksum 的确认记录，供 Generator 在生成前复核。"""
    if submitted_checksum != expected_checksum:
        raise ValueError(f"{subject_type} checksum 不匹配，拒绝确认当前知识输入")
    approval = GenerationApprovalV1(
        schema_version="muye.ai/generation-approval/v1",
        subject_type=subject_type,
        subject_slug=knowledge_slug,
        revision=revision,
        checksum=expected_checksum,
        approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        approved_by=approved_by,
    )
    path = write_approval(workspace_root / "config", approval)
    _print_json(
        {
            "approval": path.relative_to(workspace_root).as_posix(),
            "knowledge_slug": knowledge_slug,
            "status": "checksum-confirmed",
            "type": subject_type,
        }
    )
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
