"""将资料目录编排为已评测的模板 Agent。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from typing import Any
import uuid

import httpx
import yaml
from dotenv import dotenv_values

from contracts.models import EvaluationCaseV1, EvaluationSetV1
from tools.agent_generator.approvals import write_approval
from tools.agent_generator.checksums import canonical_checksum
from tools.agent_generator.generator import AgentGenerator, GeneratorPaths
from tools.agent_generator.io import load_yaml_model
from tools.agent_generator.models import (
    AgentProfileInputV1,
    AgentProfileProposalV1,
    GenerationApprovalV1,
    GenerationRecipeV1,
    KnowledgeGenerationInputV1,
)
from tools.knowledge_pipeline.checksums import file_checksum
from tools.knowledge_pipeline.chunking import chunk_documents
from tools.knowledge_pipeline.evaluation import RetrievalRunner
from tools.knowledge_pipeline.io import load_json_model, write_json_atomic
from tools.knowledge_pipeline.milvus_publisher import MilvusPublisher
from tools.knowledge_pipeline.models import KnowledgeSourceConfigV1
from tools.knowledge_pipeline.parsers import discover_source_files, parse_documents
from tools.knowledge_pipeline.planning import (
    build_collection_index_plan,
    build_resource_manifest,
)
from tools.knowledge_pipeline.worker import KnowledgeWorker

from .candidate import CandidateDataService
from .models import AgentCreationApprovalV1, AgentCreationPlanV1, AgentCreationRunV1, AgentProjectSpecV1
from .proposals import MuyeLLMProposalClient, ProposalClient, sampled_chunk_context


_DEFAULT_LLM_BASE_URL = "http://127.0.0.1:9850"
_DEFAULT_MILVUS_URI = "http://127.0.0.1:19530"


def _creation_environment(workspace_root: Path) -> dict[str, str]:
    """读取 Agent Creation 专属配置，不污染其他 CLI 子命令的进程环境。"""

    path = workspace_root / "tools" / "agent_creation" / ".env"
    values = (
        {name: value for name, value in dotenv_values(path).items() if value is not None}
        if path.is_file()
        else {}
    )
    return {**values, **os.environ}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_yaml_atomic(path: Path, value: dict[str, Any]) -> None:
    """以原子替换写入编排器拥有的 YAML 派生产物。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temporary.replace(path)


class AgentCreationService:
    """两步式 Agent 创建门面。

    ``prepare`` 只解析资料并生成可审阅计划；``create`` 必须携带该计划 checksum，
    才会调用 Embedding、Milvus 和模板 Generator。所有底层配置均为计划派生物。
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        proposal_client: ProposalClient | None = None,
        embedding_dimensions: int | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve(strict=True)
        self._environment = _creation_environment(self._workspace_root)
        self._llm_base_url = self._environment.get(
            "MUYE_KNOWLEDGE_LLM_BASE_URL", _DEFAULT_LLM_BASE_URL
        ).rstrip("/")
        self._milvus_uri = self._environment.get(
            "MUYE_KNOWLEDGE_MILVUS_URI", _DEFAULT_MILVUS_URI
        ).rstrip("/")
        self._milvus_token = self._environment.get("MUYE_KNOWLEDGE_MILVUS_TOKEN", "").strip() or None
        self._proposal_client = proposal_client or MuyeLLMProposalClient(
            base_url=self._llm_base_url
        )
        self._embedding_dimensions = embedding_dimensions

    def prepare(self, project_directory: Path) -> AgentCreationPlanV1:
        """解析项目资料并生成统一计划，不访问 Milvus 或调用 Embedding。"""

        project_path, import_root, project = self._load_project(project_directory)
        self._resolve_model_capabilities(project)
        source_config = self._source_config(project)
        worker = KnowledgeWorker(self._workspace_root)
        proposal_result = worker.propose_schema(
            slug=project.slug,
            import_root=import_root,
            ocr_available=project.ocr_available,
            source_config=source_config,
        )
        paths = discover_source_files(source_config, import_root=import_root)
        documents = parse_documents(
            paths,
            import_root=import_root,
            config=source_config,
            knowledge_version_id=proposal_result.proposal.knowledge_version_id,
            ocr_available=project.ocr_available,
        )
        chunks = chunk_documents(documents, policy=source_config.chunking)
        proposal = self._proposal_client.propose(project=project, chunks=sampled_chunk_context(chunks))
        profile_input = self._profile_input(project, proposal)
        evaluation_set = self._evaluation_set(project, proposal, chunks)
        worker_plan = build_collection_index_plan(proposal_result.proposal)
        manifest = build_resource_manifest(source_config, proposal_result.proposal, worker_plan)
        knowledge_input = self._knowledge_input(project, manifest, evaluation_set)
        project_checksum = file_checksum(project_path)
        payload = {
            "schema_version": "muye.ai/agent-creation-plan/v1",
            "project_slug": project.slug,
            "project_checksum": project_checksum,
            "source_set_checksum": proposal_result.proposal.document_set_checksum,
            "proposal_checksum": proposal_result.proposal.proposal_checksum,
            "created_at": _timestamp(),
            "source_config": source_config.model_dump(mode="json"),
            "knowledge_input": knowledge_input.model_dump(mode="json"),
            "profile_input": profile_input.model_dump(mode="json"),
            "evaluation_set": evaluation_set.model_dump(mode="json"),
            "summary": {
                "source_files": [path.relative_to(import_root).as_posix() for path in paths],
                "chunk_count": len(chunks),
                "embedding_dimensions": self._embedding_dimensions,
                "profile": profile_input.profile.model_dump(mode="json"),
                "evaluation_cases": [case.model_dump(mode="json") for case in evaluation_set.cases],
                "evaluation_evidence": self._evaluation_evidence(evaluation_set, chunks),
                "collection_plan_checksum": worker_plan.plan_checksum,
            },
        }
        plan = AgentCreationPlanV1.model_validate({**payload, "plan_checksum": canonical_checksum(payload)})
        self._write_plan(plan)
        return plan

    def create(
        self,
        project_directory: Path,
        *,
        plan_checksum: str,
        approved_by: str,
        runner: RetrievalRunner | None = None,
        embedder: Any | None = None,
        publisher: Any | None = None,
        run_tests: bool = True,
    ) -> dict[str, Any]:
        """复核计划后构建、评测并生成源码；失败时绝不生成未评测 Agent。"""

        project_path, import_root, project = self._load_project(project_directory)
        plan = self._load_plan(project.slug)
        if plan.plan_checksum != plan_checksum:
            raise ValueError("提交的 plan_checksum 与当前计划不一致")
        if file_checksum(project_path) != plan.project_checksum:
            raise ValueError("project.yaml 已变化，必须重新执行 agent prepare")
        self._assert_sources_unchanged(
            plan,
            import_root=import_root,
            ocr_available=project.ocr_available,
        )
        run = self._create_run(project.slug, plan.plan_checksum)
        created_artifacts: list[Path] = []
        try:
            existing_directory = self._reuse_existing_agent(plan, run_tests=run_tests)
            if existing_directory is not None:
                run = self._update_run(run, status="SUCCEEDED", stage="complete")
                return {"run_id": run.run_id, "directory": str(existing_directory), "status": "reused"}
            self._materialize(plan, created_artifacts=created_artifacts)
            worker = KnowledgeWorker(self._workspace_root)
            proposal = worker.propose_schema(slug=project.slug, import_root=import_root, ocr_available=project.ocr_available)
            if proposal.proposal.proposal_checksum != plan.proposal_checksum:
                raise ValueError("资料或创建配置已变化，必须重新执行 agent prepare")
            self._write_approvals(
                plan,
                approved_by,
                created_artifacts=created_artifacts,
            )
            run = self._update_run(run, stage="build")
            selected_publisher = publisher or MilvusPublisher(
                uri=self._milvus_uri,
                token=self._milvus_token,
            )
            build = worker.build(
                slug=project.slug,
                import_root=import_root,
                embedder=embedder,
                publisher=selected_publisher,
                ocr_available=project.ocr_available,
                llm_base_url=self._llm_base_url,
            )
            build_status = worker.status(build.job_id)
            if build.manifest is None or build_status["status"] != "SUCCEEDED":
                raise RuntimeError(
                    self._job_failure_message(
                        "知识构建",
                        job_id=build.job_id,
                        status=build_status,
                        report_path=getattr(build, "report_path", None),
                    )
                )
            run = self._update_run(run, stage="evaluate")
            if runner is None:
                with CandidateDataService(
                    workspace_root=self._workspace_root,
                    slug=project.slug,
                    connection=project.connection,
                    llm_base_url=self._llm_base_url,
                    milvus_uri=self._milvus_uri,
                    milvus_token=self._milvus_token,
                ) as candidate:
                    evaluation = worker.evaluate(slug=project.slug, data_base_url=candidate.base_url)
            else:
                evaluation = worker.evaluate(slug=project.slug, runner=runner)
            evaluation_status = worker.status(evaluation.job_id)
            if evaluation.manifest is None or evaluation_status["status"] != "SUCCEEDED":
                raise RuntimeError(
                    self._job_failure_message(
                        "检索评测",
                        job_id=evaluation.job_id,
                        status=evaluation_status,
                        report_path=getattr(evaluation, "report_path", None),
                    )
                )
            run = self._update_run(run, stage="generate")
            generator = AgentGenerator(GeneratorPaths.for_workspace(self._workspace_root))
            generated = generator.generate(
                slug=project.slug,
                knowledge_slug=project.slug,
                evaluation_set=EvaluationSetV1.model_validate(plan.evaluation_set),
            )
            report = generator.validate(slug=project.slug)
            if not report.is_valid:
                raise RuntimeError("生成 Agent 的 provenance 校验失败")
            if run_tests:
                self._run_generated_tests(generated.directory)
            run = self._update_run(run, status="SUCCEEDED", stage="complete")
            return {"run_id": run.run_id, "directory": str(generated.directory), "status": "generated"}
        except Exception as exc:
            self._update_run(run, status="FAILED", stage=run.stage, error=str(exc))
            agent_directory = self._workspace_root / "agents" / f"agent-{project.slug}"
            if not agent_directory.exists() and not agent_directory.is_symlink():
                self._rollback_created_artifacts(created_artifacts)
            raise

    def _load_project(self, project_directory: Path) -> tuple[Path, Path, AgentProjectSpecV1]:
        directory = project_directory.resolve(strict=True)
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("Agent 项目目录必须是普通目录")
        project_path = directory / "project.yaml"
        project = load_yaml_model(project_path, AgentProjectSpecV1)
        source_directory = directory / "sources"
        if not source_directory.is_dir() or source_directory.is_symlink():
            raise ValueError("Agent 项目必须包含普通 sources 目录")
        return project_path, directory, project

    def _source_config(self, project: AgentProjectSpecV1) -> KnowledgeSourceConfigV1:
        if self._embedding_dimensions is None:
            raise RuntimeError("Embedding 模型维度尚未解析")
        resource_id = f"kb.{project.slug.replace('-', '_')}"
        return KnowledgeSourceConfigV1.model_validate(
            {
                "schema_version": "muye.ai/knowledge-source-config/v1",
                "knowledge_id": resource_id,
                "resource_id": resource_id,
                "slug": project.slug,
                "display_name": project.display_name,
                "sources": [{"path": "sources", "include": ["**/*.pdf", "**/*.docx", "**/*.md", "**/*.txt"]}],
                "parser_profile": "docling-default-v1",
                "embedding_alias": project.embedding_model_alias,
                "embedding_revision": "r1",
                "embedding_dimensions": self._embedding_dimensions,
                "connection": project.connection,
                "chunking": {
                    "max_characters": project.max_characters,
                    "overlap_characters": project.overlap_characters,
                    "min_characters": project.min_characters,
                },
                "embedding_batch_size": project.embedding_batch_size,
                "keyword_analyzer": "jieba",
                "default_pipeline": "hybrid",
                "rerank_alias": None,
                "rerank_required": False,
                "evaluation_set_ref": f"knowledge-evaluations/{project.slug}.yaml",
            }
        )

    def _resolve_model_capabilities(self, project: AgentProjectSpecV1) -> None:
        """在 prepare 前确认 Chat/Embedding alias，并读取真实向量维度。"""

        if self._embedding_dimensions is not None:
            return
        try:
            response = httpx.get(f"{self._llm_base_url}/api/v2/models", timeout=10.0, trust_env=False)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError("无法读取 muye-llm 模型能力；请确认服务与配置") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        chat_models = data.get("models") if isinstance(data, dict) else None
        embedding_models = data.get("embedding_models") if isinstance(data, dict) else None
        if not isinstance(chat_models, list) or not any(item.get("id") == project.chat_model_alias for item in chat_models if isinstance(item, dict)):
            raise ValueError("project.yaml 引用了未注册的 Chat model alias")
        dimensions = next(
            (
                item.get("dimensions")
                for item in embedding_models or []
                if isinstance(item, dict) and item.get("id") == project.embedding_model_alias
            ),
            None,
        )
        if not isinstance(dimensions, int) or dimensions < 1:
            raise ValueError("project.yaml 引用了未注册或无效的 Embedding model alias")
        self._embedding_dimensions = dimensions

    @staticmethod
    def _profile_input(project: AgentProjectSpecV1, proposal: dict[str, Any]) -> AgentProfileInputV1:
        profile = AgentProfileProposalV1.model_validate(proposal.get("profile"))
        if profile.display_name != project.display_name:
            profile = profile.model_copy(update={"display_name": project.display_name})
        return AgentProfileInputV1(
            schema_version="muye.ai/agent-profile-input/v1",
            agent_id=project.agent_id,
            slug=project.slug,
            agent_version=project.agent_version,
            profile_revision=f"profile/{project.slug}@1",
            profile=profile,
        )

    @staticmethod
    def _evaluation_set(project: AgentProjectSpecV1, proposal: dict[str, Any], chunks: list[Any]) -> EvaluationSetV1:
        chunk_citations = {chunk.chunk_id: chunk.citation_id for chunk in chunks}
        cases: list[EvaluationCaseV1] = []
        for raw in proposal.get("cases", []):
            case = EvaluationCaseV1.model_validate(raw)
            if not set(case.relevant_chunk_ids).issubset(chunk_citations):
                raise ValueError("LLM 评测提案引用了不属于当前资料的 chunk")
            cases.append(case.model_copy(update={"required_citation_ids": [chunk_citations[item] for item in case.relevant_chunk_ids]}))
        if not cases:
            raise ValueError("LLM 未生成可用的评测用例")
        if len(cases) < project.evaluation_case_count:
            raise ValueError(
                f"LLM 仅生成 {len(cases)} 条评测用例，少于要求的 {project.evaluation_case_count} 条"
            )
        payload = {
            "schema_version": "muye.ai/evaluation-set/v1",
            "evaluation_set_id": f"{project.slug.replace('-', '_')}_eval",
            "revision": f"evaluation/{project.slug}@1",
            "recall_at_k": 5,
            "min_recall": project.min_recall,
            "min_mrr": project.min_mrr,
            "min_citation_coverage": 1.0,
            "cases": [case.model_dump(mode="json") for case in cases[: project.evaluation_case_count]],
        }
        return EvaluationSetV1.model_validate({**payload, "checksum": canonical_checksum(payload)})

    @staticmethod
    def _evaluation_evidence(evaluation_set: EvaluationSetV1, chunks: list[Any]) -> list[dict[str, Any]]:
        """将评测引用投影为有限摘录，供人工在计划中核对相关性。"""

        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        evidence: list[dict[str, Any]] = []
        for case in evaluation_set.cases:
            for chunk_id in case.relevant_chunk_ids:
                chunk = chunk_by_id[chunk_id]
                evidence.append(
                    {
                        "case_id": case.case_id,
                        "chunk_id": chunk.chunk_id,
                        "citation_id": chunk.citation_id,
                        "source_locators": [locator.model_dump(mode="json") for locator in chunk.source_locators],
                        "excerpt": chunk.content[:600],
                    }
                )
        return evidence

    @staticmethod
    def _knowledge_input(project: AgentProjectSpecV1, manifest: Any, evaluation: EvaluationSetV1) -> KnowledgeGenerationInputV1:
        skill_payload = {"resource_id": manifest.resource_id, "resource_revision": manifest.resource_revision, "pipeline": "hybrid"}
        return KnowledgeGenerationInputV1(
            schema_version="muye.ai/knowledge-generation-input/v1",
            knowledge_slug=project.slug,
            resource_id=manifest.resource_id,
            resource_revision=manifest.resource_revision,
            resource_checksum=manifest.resource_checksum,
            skill_revision=f"retrieval/{project.slug}@1",
            skill_checksum=canonical_checksum(skill_payload),
            model_alias=project.chat_model_alias,
            retrieval_pipeline="hybrid",
            scope_filter_ref=f"scope/{project.slug}@1",
            fixed_scope_filter={"op": "eq", "field": "knowledge_version_id", "value": manifest.knowledge_version_id},
            allowed_filter_fields=["knowledge_version_id"],
            allowed_return_fields=["title", "source", "citation_id", "source_locator", "knowledge_version_id"],
            tool_budget=4,
            token_budget=8192,
            timeout_budget_seconds=30,
            evaluation_set_ref=f"knowledge-evaluations/{project.slug}.yaml",
        )

    def _assert_sources_unchanged(
        self,
        plan: AgentCreationPlanV1,
        *,
        import_root: Path,
        ocr_available: bool,
    ) -> None:
        """在任何可发布写入前复核计划绑定的完整资料集合。"""

        source_config = KnowledgeSourceConfigV1.model_validate(plan.source_config)
        current = KnowledgeWorker(self._workspace_root).propose_schema(
            slug=plan.project_slug,
            import_root=import_root,
            ocr_available=ocr_available,
            source_config=source_config,
        ).proposal
        if (
            current.document_set_checksum != plan.source_set_checksum
            or current.proposal_checksum != plan.proposal_checksum
        ):
            raise ValueError("资料或创建配置已变化，必须重新执行 agent prepare")

    def _materialize(
        self,
        plan: AgentCreationPlanV1,
        *,
        created_artifacts: list[Path] | None = None,
    ) -> None:
        """仅在同名配置不存在或与计划完全一致时物化兼容配置。"""

        config = self._workspace_root / "config"
        artifacts: tuple[tuple[Path, dict[str, Any], type[Any]], ...] = (
            (config / "knowledge-sources" / f"{plan.project_slug}.yaml", plan.source_config, KnowledgeSourceConfigV1),
            (config / "knowledge" / f"{plan.project_slug}.yaml", plan.knowledge_input, KnowledgeGenerationInputV1),
            (config / "agents" / f"{plan.project_slug}.yaml", plan.profile_input, AgentProfileInputV1),
            (config / "knowledge-evaluations" / f"{plan.project_slug}.yaml", plan.evaluation_set, EvaluationSetV1),
        )
        for path, expected, model_type in artifacts:
            if path.exists() or path.is_symlink():
                actual = load_yaml_model(path, model_type).model_dump(mode="json")
                if actual != expected:
                    raise ValueError(f"拒绝覆盖已有同名高级配置：{path.relative_to(self._workspace_root)}")
        for path, expected, _ in artifacts:
            existed = path.exists() or path.is_symlink()
            _write_yaml_atomic(path, expected)
            if not existed and created_artifacts is not None:
                created_artifacts.append(path)

    def _reuse_existing_agent(self, plan: AgentCreationPlanV1, *, run_tests: bool) -> Path | None:
        """复用同一计划已发布的目录，避免测试失败后无法恢复。"""

        directory = self._workspace_root / "agents" / f"agent-{plan.project_slug}"
        if not directory.exists() and not directory.is_symlink():
            return None
        descriptor_path = directory / "agent.yaml"
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or descriptor_path.is_symlink()
            or not descriptor_path.is_file()
        ):
            raise FileExistsError(
                "目标 Agent 目录不完整，拒绝覆盖以保护现有文件："
                f"{directory}。请先将该目录移动到备份位置后重试，例如："
                f"mv {directory} {directory}.incomplete"
            )
        generator = AgentGenerator(GeneratorPaths.for_workspace(self._workspace_root))
        report = generator.validate(slug=plan.project_slug)
        if not report.is_valid:
            raise RuntimeError("已有 Agent 的 provenance 校验失败")
        recipe = load_json_model(directory / ".muye-generation-input.json", GenerationRecipeV1)
        recipe_evaluation = (
            recipe.evaluation_set.model_dump(mode="json") if recipe.evaluation_set is not None else None
        )
        if (
            recipe.profile_input.model_dump(mode="json") != plan.profile_input
            or recipe.knowledge.model_dump(mode="json") != plan.knowledge_input
            or recipe_evaluation != plan.evaluation_set
        ):
            raise FileExistsError("目标 Agent 目录属于不同的创建计划，拒绝覆盖")
        if run_tests:
            self._run_generated_tests(directory)
        return directory

    @staticmethod
    def _job_failure_message(
        operation: str,
        *,
        job_id: str,
        status: dict[str, Any],
        report_path: Path | None,
    ) -> str:
        """将 Worker 的持久化失败状态投影为可直接定位报告的 CLI 错误。"""

        error_code = status.get("error_code") or "UNKNOWN"
        report_reference = report_path or status.get("report_ref") or "未生成报告"
        return f"{operation}失败（Job {job_id}，错误码：{error_code}；报告：{report_reference}）"

    def _write_plan(self, plan: AgentCreationPlanV1) -> None:
        root = self._workspace_root / "config" / "generated" / "agent-creation-plans" / plan.project_slug
        write_json_atomic(root / "current.json", plan.model_dump(mode="json"))

    def _load_plan(self, slug: str) -> AgentCreationPlanV1:
        path = self._workspace_root / "config" / "generated" / "agent-creation-plans" / slug / "current.json"
        plan = load_json_model(path, AgentCreationPlanV1)
        payload = plan.model_dump(mode="json")
        checksum = payload.pop("plan_checksum")
        if checksum != canonical_checksum(payload):
            raise ValueError("Agent Creation Plan checksum 无效")
        return plan

    def _write_approvals(
        self,
        plan: AgentCreationPlanV1,
        approved_by: str,
        *,
        created_artifacts: list[Path] | None = None,
    ) -> None:
        now = _timestamp()
        approval = AgentCreationApprovalV1(
            schema_version="muye.ai/agent-creation-approval/v1",
            project_slug=plan.project_slug,
            plan_checksum=plan.plan_checksum,
            approved_by=approved_by,
            approved_at=now,
        )
        creation_path = (
            self._workspace_root
            / "config"
            / "approvals"
            / "creation"
            / f"{plan.project_slug}.json"
        )
        creation_existed = creation_path.exists() or creation_path.is_symlink()
        write_json_atomic(
            creation_path,
            approval.model_dump(mode="json"),
        )
        if not creation_existed and created_artifacts is not None:
            created_artifacts.append(creation_path)
        worker = KnowledgeWorker(self._workspace_root)
        schema_path = (
            self._workspace_root
            / "config"
            / "approvals"
            / "schema"
            / f"{plan.project_slug}.json"
        )
        schema_existed = schema_path.exists() or schema_path.is_symlink()
        worker.approve_schema(
            slug=plan.project_slug,
            checksum=plan.proposal_checksum,
            approved_by=approved_by,
        )
        if not schema_existed and created_artifacts is not None:
            created_artifacts.append(schema_path)
        for subject, checksum, revision in (
            ("resource", plan.knowledge_input["resource_checksum"], plan.knowledge_input["resource_revision"]),
            ("skill", plan.knowledge_input["skill_checksum"], plan.knowledge_input["skill_revision"]),
            ("profile", canonical_checksum(plan.profile_input["profile"]), plan.profile_input["profile_revision"]),
        ):
            approval_path = (
                self._workspace_root
                / "config"
                / "approvals"
                / subject
                / f"{plan.project_slug}.json"
            )
            approval_existed = approval_path.exists() or approval_path.is_symlink()
            written_path = write_approval(
                self._workspace_root / "config",
                GenerationApprovalV1(
                    schema_version="muye.ai/generation-approval/v1",
                    subject_type=subject,
                    subject_slug=plan.project_slug,
                    revision=revision,
                    checksum=checksum,
                    approved_at=now,
                    approved_by=approved_by,
                ),
            )
            if not approval_existed and created_artifacts is not None:
                created_artifacts.append(written_path)

    @staticmethod
    def _rollback_created_artifacts(paths: list[Path]) -> None:
        """回滚本次新建且尚未被 Agent 生成结果引用的本地配置。"""

        for path in reversed(paths):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                # 保留原始创建失败异常；残留路径仍可从失败 run 中定位。
                continue

    def _create_run(self, slug: str, plan_checksum: str) -> AgentCreationRunV1:
        now = _timestamp()
        run = AgentCreationRunV1(
            schema_version="muye.ai/agent-creation-run/v1",
            run_id=f"creation_{uuid.uuid4().hex}",
            project_slug=slug,
            plan_checksum=plan_checksum,
            status="RUNNING",
            stage="prepare",
            created_at=now,
            updated_at=now,
        )
        self._save_run(run)
        return run

    def _update_run(
        self,
        run: AgentCreationRunV1,
        *,
        status: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> AgentCreationRunV1:
        updated = run.model_copy(
            update={
                "status": status or run.status,
                "stage": stage or run.stage,
                "error": error,
                "updated_at": _timestamp(),
            }
        )
        self._save_run(updated)
        return updated

    def _save_run(self, run: AgentCreationRunV1) -> None:
        write_json_atomic(self._workspace_root / "config" / "generated" / "agent-creation-runs" / f"{run.run_id}.json", run.model_dump(mode="json"))

    def _run_generated_tests(self, directory: Path) -> None:
        result = subprocess.run(
            [str(self._workspace_root / ".venv" / "bin" / "python"), "-m", "pytest", "-q", "tests"],
            cwd=directory,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("生成 Agent 契约测试失败")
