"""两步式 Agent 创建编排的离线回归测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os

import pytest
import yaml
from dotenv import dotenv_values

import tools.agent_creation.service as creation_service_module
from tools.agent_creation.candidate import CandidateDataService
from tools.agent_creation.service import AgentCreationService


class _ProposalClient:
    """固定 LLM 响应，确保单元测试不访问模型服务。"""

    def propose(self, *, project: object, chunks: list[dict[str, str]]) -> dict[str, object]:
        assert chunks
        return {
            "profile": {
                "schema_version": "muye.ai/agent-profile-proposal/v1",
                "display_name": "员工手册助手",
                "description": "依据员工手册回答制度问题。",
                "supported_intents": ["制度查询"],
                "instructions": "只依据检索结果回答，并提供来源引用。",
                "do_not_use_when": ["用户要求执行人事写操作"],
                "examples": ["请假需要提前多久申请？"],
            },
            "cases": [
                {
                    "case_id": f"leave_policy_{index}",
                    "query": f"请假需要提前多久申请？问题 {index}",
                    "relevant_chunk_ids": [chunks[0]["chunk_id"]],
                }
                for index in range(12)
            ],
        }


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "agent-projects" / "hotel-employee"
    sources = project / "sources"
    sources.mkdir(parents=True)
    (project / "project.yaml").write_text(
        "\n".join(
            [
                "schema_version: muye.ai/agent-project/v1",
                "slug: hotel-employee",
                "agent_id: agent_hotel_employee",
                "display_name: 员工手册助手",
                "objective: 根据员工手册回答制度问题。",
                "prohibited_actions:",
                "  - 不得执行请假审批",
                "examples:",
                "  - 请假需要提前多久申请？",
            ]
        ),
        encoding="utf-8",
    )
    (sources / "manual.md").write_text("# 请假\n\n员工请事假应提前三天提交申请。\n", encoding="utf-8")
    return project


def test_prepare_from_one_markdown_creates_reviewable_plan(tmp_path: Path) -> None:
    """用户只提供 project.yaml 与一个 MD 时，prepare 应产出可审核计划而不覆盖配置。"""

    project = _project(tmp_path)
    service = AgentCreationService(tmp_path, proposal_client=_ProposalClient(), embedding_dimensions=4)

    plan = service.prepare(project)

    assert plan.project_slug == "hotel-employee"
    assert plan.summary["chunk_count"] >= 1
    assert plan.source_config["embedding_batch_size"] == 16
    assert plan.knowledge_input["allowed_return_fields"][-1] == "knowledge_version_id"
    assert plan.evaluation_set["cases"][0]["required_citation_ids"]
    evidence = plan.summary["evaluation_evidence"]
    assert evidence[0]["source_locators"]
    assert len(evidence[0]["excerpt"]) <= 600
    assert (tmp_path / "config" / "generated" / "agent-creation-plans" / "hotel-employee" / "current.json").is_file()
    assert not (tmp_path / "config" / "knowledge-sources" / "hotel-employee.yaml").exists()


def test_prepare_uses_project_embedding_batch_size(tmp_path: Path) -> None:
    """项目可收紧单次 Embedding 请求，适配上游批量限制。"""

    project = _project(tmp_path)
    project_file = project / "project.yaml"
    project_file.write_text(
        project_file.read_text(encoding="utf-8") + "\nembedding_batch_size: 8\n",
        encoding="utf-8",
    )

    plan = AgentCreationService(
        tmp_path,
        proposal_client=_ProposalClient(),
        embedding_dimensions=4,
    ).prepare(project)

    assert plan.source_config["embedding_batch_size"] == 8


def test_prepare_rejects_llm_chunk_outside_current_documents(tmp_path: Path) -> None:
    """模型不能通过伪造 chunk ID 绕过评测来源边界。"""

    class _UnsafeProposalClient(_ProposalClient):
        def propose(self, *, project: object, chunks: list[dict[str, str]]) -> dict[str, object]:
            proposal = super().propose(project=project, chunks=chunks)
            proposal["cases"][0]["relevant_chunk_ids"] = ["chunk_not_in_source"]  # type: ignore[index]
            return proposal

    with pytest.raises(ValueError, match="不属于当前资料"):
        AgentCreationService(tmp_path, proposal_client=_UnsafeProposalClient(), embedding_dimensions=4).prepare(_project(tmp_path))


def test_create_rejects_project_drift_before_any_external_write(tmp_path: Path) -> None:
    """prepare 后修改用户声明必须使一次确认失效。"""

    project = _project(tmp_path)
    service = AgentCreationService(tmp_path, proposal_client=_ProposalClient(), embedding_dimensions=4)
    plan = service.prepare(project)
    project_file = project / "project.yaml"
    project_file.write_text(
        project_file.read_text(encoding="utf-8").replace("根据员工手册回答制度问题。", "改为另一项制度问答目标。"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project.yaml 已变化"):
        service.create(project, plan_checksum=plan.plan_checksum, approved_by="reviewer", run_tests=False)


def test_create_rejects_source_drift_before_materializing_configuration(tmp_path: Path) -> None:
    """资料变化必须在兼容配置、审批和外部写入之前使计划失效。"""

    project = _project(tmp_path)
    service = AgentCreationService(
        tmp_path,
        proposal_client=_ProposalClient(),
        embedding_dimensions=4,
    )
    plan = service.prepare(project)
    (project / "sources" / "manual.md").write_text(
        "# 请假\n\n员工请事假应提前五天提交申请。\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="资料或创建配置已变化"):
        service.create(
            project,
            plan_checksum=plan.plan_checksum,
            approved_by="reviewer",
            run_tests=False,
        )

    assert not (tmp_path / "config" / "knowledge-sources" / "hotel-employee.yaml").exists()
    assert not (tmp_path / "config" / "approvals" / "creation" / "hotel-employee.json").exists()


def test_source_drift_cannot_reuse_existing_agent(tmp_path: Path) -> None:
    """已有 Agent 目录不能绕过当前资料集合与计划 checksum 的复核。"""

    project = _project(tmp_path)
    service = AgentCreationService(
        tmp_path,
        proposal_client=_ProposalClient(),
        embedding_dimensions=4,
    )
    plan = service.prepare(project)
    (tmp_path / "agents" / "agent-hotel-employee").mkdir(parents=True)
    (project / "sources" / "manual.md").write_text(
        "# 请假\n\n制度内容已经变化。\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="资料或创建配置已变化"):
        service.create(
            project,
            plan_checksum=plan.plan_checksum,
            approved_by="reviewer",
            run_tests=False,
        )


def test_prepare_requires_the_requested_number_of_evaluation_cases(tmp_path: Path) -> None:
    """LLM 不能以少量用例静默降低评测门禁。"""

    class _InsufficientProposalClient(_ProposalClient):
        def propose(self, *, project: object, chunks: list[dict[str, str]]) -> dict[str, object]:
            proposal = super().propose(project=project, chunks=chunks)
            proposal["cases"] = proposal["cases"][:1]
            return proposal

    with pytest.raises(ValueError, match="少于要求"):
        AgentCreationService(tmp_path, proposal_client=_InsufficientProposalClient(), embedding_dimensions=4).prepare(_project(tmp_path))


def test_materialize_refuses_to_overwrite_different_existing_configuration(tmp_path: Path) -> None:
    """创建流程不得覆盖同 slug 的手工高级配置。"""

    project = _project(tmp_path)
    service = AgentCreationService(tmp_path, proposal_client=_ProposalClient(), embedding_dimensions=4)
    plan = service.prepare(project)
    source_path = tmp_path / "config" / "knowledge-sources" / "hotel-employee.yaml"
    source_path.parent.mkdir(parents=True)
    existing = {**plan.source_config, "max_chunks": 9}
    source_path.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="拒绝覆盖"):
        service._materialize(plan)
    assert yaml.safe_load(source_path.read_text(encoding="utf-8"))["max_chunks"] == 9


def test_create_stops_when_evaluation_job_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """评测返回 manifest 也不能绕过 FAILED Job 的发布门禁。"""

    project = _project(tmp_path)
    service = AgentCreationService(tmp_path, proposal_client=_ProposalClient(), embedding_dimensions=4)
    plan = service.prepare(project)

    class _Worker:
        def __init__(self, _: Path) -> None:
            pass

        def propose_schema(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                proposal=SimpleNamespace(
                    document_set_checksum=plan.source_set_checksum,
                    proposal_checksum=plan.proposal_checksum,
                )
            )

        def approve_schema(self, **_: object) -> None:
            return None

        def build(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(job_id="build-job", manifest=object())

        def evaluate(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(job_id="evaluation-job", manifest=object())

        def status(self, job_id: str) -> dict[str, str]:
            return {"status": "SUCCEEDED" if job_id == "build-job" else "FAILED"}

    class _Generator:
        def __init__(self, _: object) -> None:
            pass

        def generate(self, **_: object) -> object:
            raise AssertionError("failed evaluation must not generate an Agent")

    monkeypatch.setattr(creation_service_module, "KnowledgeWorker", _Worker)
    monkeypatch.setattr(creation_service_module, "AgentGenerator", _Generator)

    with pytest.raises(RuntimeError, match="检索评测失败（Job evaluation-job，错误码：UNKNOWN"):
        service.create(project, plan_checksum=plan.plan_checksum, approved_by="reviewer", runner=object(), run_tests=False)
    assert not (tmp_path / "agents" / "agent-hotel-employee").exists()
    assert not (tmp_path / "config" / "knowledge-sources" / "hotel-employee.yaml").exists()
    assert not (tmp_path / "config" / "approvals" / "creation" / "hotel-employee.json").exists()


def test_create_reuses_existing_agent_from_the_same_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首次生成后的测试失败或重复确认均可只校验并重跑测试。"""

    project = _project(tmp_path)
    service = AgentCreationService(tmp_path, proposal_client=_ProposalClient(), embedding_dimensions=4)
    plan = service.prepare(project)
    directory = tmp_path / "agents" / "agent-hotel-employee"
    directory.mkdir(parents=True)
    (directory / "agent.yaml").write_text("placeholder: true\n", encoding="utf-8")

    class _Generator:
        def __init__(self, _: object) -> None:
            pass

        def validate(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(is_valid=True)

    original_load_json_model = creation_service_module.load_json_model

    def _load_json_model(path: Path, model_type: object) -> object:
        if path.name == ".muye-generation-input.json":
            return SimpleNamespace(
                profile_input=SimpleNamespace(model_dump=lambda **_: plan.profile_input),
                knowledge=SimpleNamespace(model_dump=lambda **_: plan.knowledge_input),
                evaluation_set=SimpleNamespace(model_dump=lambda **_: plan.evaluation_set),
            )
        return original_load_json_model(path, model_type)

    monkeypatch.setattr(creation_service_module, "AgentGenerator", _Generator)
    monkeypatch.setattr(creation_service_module, "load_json_model", _load_json_model)
    monkeypatch.setattr(service, "_run_generated_tests", lambda _: None)

    result = service.create(project, plan_checksum=plan.plan_checksum, approved_by="reviewer")

    assert result["status"] == "reused"
    assert result["directory"] == str(directory)


def test_create_reports_incomplete_existing_agent_without_overwriting_it(tmp_path: Path) -> None:
    """不完整目录可能含有用户 `.env`，创建流程必须保留它并提供可恢复错误。"""

    project = _project(tmp_path)
    service = AgentCreationService(tmp_path, proposal_client=_ProposalClient(), embedding_dimensions=4)
    plan = service.prepare(project)
    directory = tmp_path / "agents" / "agent-hotel-employee"
    directory.mkdir(parents=True)
    environment_file = directory / ".env"
    environment_file.write_text("MUYE_AGENT_TOKEN=example\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="目标 Agent 目录不完整"):
        service.create(
            project,
            plan_checksum=plan.plan_checksum,
            approved_by="reviewer",
            run_tests=False,
        )

    assert environment_file.read_text(encoding="utf-8") == "MUYE_AGENT_TOKEN=example\n"


def test_candidate_start_failure_terminates_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """候选服务未就绪时，已启动的子进程必须立即被清理。"""

    class _Process:
        def __init__(self) -> None:
            self.terminated = False
            self.waited = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> None:
            self.waited = True

    process = _Process()
    captured: dict[str, object] = {}

    def _popen(*_args: object, **kwargs: object) -> _Process:
        captured.update(kwargs)
        return process

    candidate = CandidateDataService(workspace_root=tmp_path, slug="hotel-employee", connection="milvus_default")
    monkeypatch.setattr("tools.agent_creation.candidate._available_loopback_port", lambda: 19000)
    monkeypatch.setattr("tools.agent_creation.candidate.subprocess.Popen", _popen)
    monkeypatch.setattr(candidate, "_wait_ready", lambda: (_ for _ in ()).throw(RuntimeError("not ready")))

    with pytest.raises(RuntimeError, match="not ready"):
        candidate.__enter__()
    assert process.terminated
    assert process.waited
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["PYTHONPATH"].split(os.pathsep)[:2] == [str(tmp_path), str(tmp_path / "muye-data")]


def test_creation_environment_does_not_mutate_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent Creation 配置只属于服务实例，不能影响其他统一 CLI 子命令。"""

    environment_file = tmp_path / "tools" / "agent_creation" / ".env"
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text(
        "MUYE_KNOWLEDGE_LLM_BASE_URL=http://llm.example.test:9850\n"
        "MUYE_KNOWLEDGE_MILVUS_URI=http://milvus.example.test:19530\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MUYE_KNOWLEDGE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("MUYE_KNOWLEDGE_MILVUS_URI", raising=False)

    service = AgentCreationService(
        tmp_path,
        proposal_client=_ProposalClient(),
        embedding_dimensions=4,
    )

    assert service._llm_base_url == "http://llm.example.test:9850"
    assert service._milvus_uri == "http://milvus.example.test:19530"
    assert "MUYE_KNOWLEDGE_LLM_BASE_URL" not in os.environ
    assert "MUYE_KNOWLEDGE_MILVUS_URI" not in os.environ


def test_creation_environment_example_does_not_contain_token() -> None:
    """Agent Creation 功能提交必须携带无凭据的模块配置模板。"""

    example = Path(__file__).resolve().parents[2] / "tools" / "agent_creation" / ".env.example"
    values = dotenv_values(example)

    assert values["MUYE_KNOWLEDGE_MILVUS_TOKEN"] == ""
