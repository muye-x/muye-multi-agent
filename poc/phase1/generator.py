"""阶段 1 的一次性 Agent 目录渲染器。

它验证目录所有权和 provenance 行为，为阶段 3 的正式生成器提供可抛弃的 PoC 依据。
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile

import yaml

from contracts.models import AgentDescriptorV1, AgentGenerationSpecV1, SourceProvenanceV1

from .contracts import AgentProfileProposalV1, ParsedDocumentV1, Phase1PocConfigV1
from .profile import canonical_checksum


def generate_agent_directory(
    *,
    target_parent: Path,
    generation_spec: AgentGenerationSpecV1,
    profile: AgentProfileProposalV1,
    document: ParsedDocumentV1,
    config: Phase1PocConfigV1,
) -> Path:
    """在目标父目录下原子创建一次性 ReAct 知识 Agent 目录。

    已存在的同名目录一律失败，避免 PoC 或后续工具覆盖开发者接管后的文件。
    """
    _validate_inputs(generation_spec, profile, config)
    parent = target_parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"agent-{generation_spec.slug}"
    if target.exists():
        raise FileExistsError(f"目标 Agent 目录已存在：{target}")

    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=parent))
    try:
        _write_agent_files(staging, generation_spec, profile, config)
        provenance = SourceProvenanceV1(
            schema_version="muye.ai/source-provenance/v1",
            generator_version="0.1.0",
            template_id=generation_spec.template_id,
            template_version=generation_spec.template_version,
            sdk_version=generation_spec.sdk_version,
            generation_spec_checksum=generation_spec.input_checksum,
            knowledge_resource_checksum=document.content_checksum,
            skill_checksum=generation_spec.skill_checksum,
            profile_checksum=generation_spec.agent_profile_checksum,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            generated_files=sorted(
                [
                    ".muye-generation.json",
                    "Dockerfile",
                    "README.md",
                    "agent.py",
                    "agent.yaml",
                    "main.py",
                    "prompts/system.md",
                    "requirements.txt",
                    "tests/test_generated_agent.py",
                ]
            ),
            generated_source_tree_checksum=_source_tree_checksum(staging),
        )
        _write_json(staging / ".muye-generation.json", provenance.model_dump(mode="json"))
        os.replace(staging, target)
        return target
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_inputs(
    generation_spec: AgentGenerationSpecV1,
    profile: AgentProfileProposalV1,
    config: Phase1PocConfigV1,
) -> None:
    """拒绝 Profile、资源或 slug 与可信 GenerationSpec 不一致的 PoC 输入。"""
    if generation_spec.slug != config.agent_slug:
        raise ValueError("generation_spec.slug 与 PoC 配置不一致")
    if generation_spec.resource_id != config.resource_id:
        raise ValueError("generation_spec.resource_id 与 PoC 配置不一致")
    if generation_spec.agent_profile_checksum != canonical_checksum(profile.model_dump(mode="json")):
        raise ValueError("Agent Profile checksum 与 generation_spec 不一致")


def _write_agent_files(
    directory: Path,
    generation_spec: AgentGenerationSpecV1,
    profile: AgentProfileProposalV1,
    config: Phase1PocConfigV1,
) -> None:
    """渲染无需网络的源码、配置、测试和容器骨架。"""
    (directory / "prompts").mkdir()
    (directory / "tests").mkdir()
    _write_text(directory / "prompts" / "system.md", profile.instructions + "\n")
    _write_text(directory / "agent.py", _agent_source(generation_spec, profile, config))
    _write_text(directory / "main.py", _main_source(generation_spec.slug))
    _write_text(directory / "tests" / "test_generated_agent.py", _test_source(generation_spec.slug))
    _write_text(directory / "requirements.txt", "muye-multi-agent-sdk==1.1.0\n")
    _write_text(directory / "Dockerfile", _dockerfile_source())
    _write_text(directory / "README.md", _readme_source(generation_spec, profile, config))
    descriptor = AgentDescriptorV1(
        schema_version="muye.ai/agent-descriptor/v1",
        agent_id=generation_spec.agent_id,
        slug=generation_spec.slug,
        tool_name=f"{generation_spec.slug.replace('-', '_')}_agent",
        display_name=profile.display_name,
        version="0.1.0",
        description=profile.description,
        supported_intents=profile.supported_intents,
        entrypoint="main:app",
        api_profile="internal",
        protocol_version="muye-agent-internal/3.0",
        model_alias=generation_spec.model_alias,
        resources=[{"resource_id": config.resource_id, "skill_ref": generation_spec.skill_revision}],
        runtime={"internal_port": 8000, "timeout_seconds": 30, "max_concurrency": 2, "memory_limit": "512m"},
        deployment={"enabled": False},
        source={
            "template_id": generation_spec.template_id,
            "template_version": generation_spec.template_version,
            "provenance_file": ".muye-generation.json",
        },
    )
    _write_text(
        directory / "agent.yaml",
        yaml.safe_dump(descriptor.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
    )


def _agent_source(
    generation_spec: AgentGenerationSpecV1,
    profile: AgentProfileProposalV1,
    config: Phase1PocConfigV1,
) -> str:
    """生成仅绑定一个逻辑资源和固定 scope 的当前 SDK ReAct Agent。"""
    class_name = "Generated" + "".join(part.capitalize() for part in generation_spec.slug.split("-")) + "Agent"
    metadata = {
        "name": generation_spec.slug,
        "version": "0.1.0",
        "description": profile.description,
        "supported_intents": profile.supported_intents,
    }
    return f'''"""阶段 1 PoC 生成的只读知识 Agent。"""
from pathlib import Path

from muye_multi_agent_sdk import AgentMetadata, ReActAgent
from muye_multi_agent_sdk.tools import create_data_retrieval_tool


class {class_name}(ReActAgent):
    """只调用绑定知识资源的 ReAct Agent；不包含跨 Agent 客户端。"""

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(**{json.dumps(metadata, ensure_ascii=False)})

    @property
    def instructions(self) -> str:
        return (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

    @property
    def langchain_tools(self) -> list:
        return [
            create_data_retrieval_tool(
                self.data_client,
                name={json.dumps(generation_spec.slug.replace('-', '_') + '_retrieve')},
                resource={json.dumps(config.resource_id)},
                pipeline={json.dumps(config.retrieval_pipeline)},
                fixed_filter={{"op": "eq", "field": {json.dumps(config.scope_field)}, "value": {json.dumps(config.scope_value)}}},
                return_fields=["title", "source", "citation_id"],
            )
        ]
'''


def _main_source(slug: str) -> str:
    """生成 SDK create_app 入口，保持 SubAgent 只暴露 internal profile 的约束。"""
    class_name = "Generated" + "".join(part.capitalize() for part in slug.split("-")) + "Agent"
    return f'''"""阶段 1 PoC Agent 的 HTTP 入口。"""
from muye_multi_agent_sdk import create_app

from agent import {class_name}


app = create_app({class_name}())
'''


def _test_source(slug: str) -> str:
    """生成无网络的最小契约测试，供开发者接管后扩展。"""
    class_name = "Generated" + "".join(part.capitalize() for part in slug.split("-")) + "Agent"
    return f'''"""生成 Agent 的最小离线契约测试。"""
from agent import {class_name}


def test_metadata_is_stable() -> None:
    assert {class_name}().metadata.name == {json.dumps(slug)}
'''


def _dockerfile_source() -> str:
    """提供 PoC 构建骨架；最终 base image 策略在阶段 2 固化。"""
    return '''FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
USER 10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
'''


def _readme_source(
    generation_spec: AgentGenerationSpecV1,
    profile: AgentProfileProposalV1,
    config: Phase1PocConfigV1,
) -> str:
    """说明 PoC 目录的限制，防止它被误当作最终生产模板。"""
    return f'''# {profile.display_name}

这是阶段 1 垂直 PoC 生成的只读知识 Agent。它绑定逻辑资源 `{config.resource_id}`，使用 `{config.retrieval_pipeline}`
检索 pipeline，并在调用时固定 `{config.scope_field}={config.scope_value}`。

该目录用于验证一次性生成和开发者接管：生成器不会覆盖已有目录。SDK v2、正式模板、镜像策略和生产部署将在后续阶段替换此 PoC 骨架。

生成输入 checksum：`{generation_spec.input_checksum}`
'''


def _source_tree_checksum(directory: Path) -> str:
    """计算排除 provenance 的稳定源文件树 checksum。"""
    digest = sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative_path = path.relative_to(directory).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_text(path: Path, content: str) -> None:
    """以 UTF-8 写入生成文件。"""
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    """使用稳定 JSON 格式写入 provenance。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
