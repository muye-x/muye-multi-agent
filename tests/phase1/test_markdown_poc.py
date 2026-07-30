"""阶段 1 Markdown 解析与一次性 Agent 目录 PoC 测试。"""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import sys

import pytest
import yaml

from contracts.models import AgentDescriptorV1, SourceProvenanceV1
from poc.phase1.contracts import Phase1PocConfigV1
from poc.phase1.generator import generate_agent_directory
from poc.phase1.markdown import parse_markdown_file
from poc.phase1.profile import build_generation_spec, build_profile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = PROJECT_ROOT / "poc" / "phase1" / "samples"


def _poc_inputs() -> tuple[object, object, Phase1PocConfigV1]:
    document = parse_markdown_file(SAMPLE_ROOT / "product-handbook.md", source_root=SAMPLE_ROOT)
    profile = build_profile(document)
    config = Phase1PocConfigV1(
        schema_version="muye.ai/phase1-poc-config/v1",
        agent_slug="product-handbook",
        resource_id="kb.product_handbook",
        resource_revision="resource/phase1-v1",
        model_alias="chat-default",
        scope_field="knowledge_id",
        scope_value="kb.product_handbook",
    )
    return document, profile, config


def test_markdown_parser_is_deterministic_and_preserves_source_locations() -> None:
    first = parse_markdown_file(SAMPLE_ROOT / "product-handbook.md", source_root=SAMPLE_ROOT)
    second = parse_markdown_file(SAMPLE_ROOT / "product-handbook.md", source_root=SAMPLE_ROOT)

    assert first == second
    assert first.title == "OrbitDesk 产品手册"
    assert [block.ordinal for block in first.blocks] == [0, 1, 2, 3]
    assert first.blocks[1].source_locator.path == "product-handbook.md"
    assert "14 天" in first.blocks[1].content


def test_markdown_parser_rejects_source_outside_configured_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source_root"):
        parse_markdown_file(outside, source_root=SAMPLE_ROOT)


def test_poc_generator_creates_valid_agent_and_never_overwrites_developer_change(tmp_path: Path) -> None:
    document, profile, config = _poc_inputs()
    spec = build_generation_spec(document=document, profile=profile, config=config)  # type: ignore[arg-type]

    target = generate_agent_directory(
        target_parent=tmp_path,
        generation_spec=spec,
        profile=profile,  # type: ignore[arg-type]
        document=document,  # type: ignore[arg-type]
        config=config,
    )

    descriptor = AgentDescriptorV1.model_validate(yaml.safe_load((target / "agent.yaml").read_text(encoding="utf-8")))
    provenance = SourceProvenanceV1.model_validate(
        json.loads((target / ".muye-generation.json").read_text(encoding="utf-8"))
    )
    compile((target / "agent.py").read_text(encoding="utf-8"), str(target / "agent.py"), "exec")
    compile((target / "main.py").read_text(encoding="utf-8"), str(target / "main.py"), "exec")
    generated_module = _load_generated_module(target / "agent.py")
    generated_agent = generated_module.GeneratedProductHandbookAgent()

    assert descriptor.deployment.enabled is False
    assert descriptor.resources[0].resource_id == "kb.product_handbook"
    assert provenance.generation_spec_checksum == spec.input_checksum
    assert generated_agent.metadata.name == "product-handbook"

    prompt = target / "prompts" / "system.md"
    prompt.write_text("开发者自定义 Prompt。\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        generate_agent_directory(
            target_parent=tmp_path,
            generation_spec=spec,
            profile=profile,  # type: ignore[arg-type]
            document=document,  # type: ignore[arg-type]
            config=config,
        )
    assert prompt.read_text(encoding="utf-8") == "开发者自定义 Prompt。\n"


def test_milvus_poc_files_keep_hybrid_resource_and_local_only_port() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "poc" / "phase1" / "milvus" / "compose.yaml").read_text(encoding="utf-8"))
    data_config = yaml.safe_load(
        (PROJECT_ROOT / "poc" / "phase1" / "milvus" / "muye-data.config.yaml").read_text(encoding="utf-8")
    )

    assert compose["services"]["milvus"]["ports"] == ["127.0.0.1:19530:19530"]
    milvus_environment = compose["services"]["milvus"]["environment"]
    assert milvus_environment["MINIO_ACCESS_KEY_ID"] == "${PHASE1_MINIO_ROOT_USER:?set PHASE1_MINIO_ROOT_USER in the environment}"
    assert milvus_environment["MINIO_SECRET_ACCESS_KEY"] == "${PHASE1_MINIO_ROOT_PASSWORD:?set PHASE1_MINIO_ROOT_PASSWORD in the environment}"
    resource = data_config["resources"]["product_handbook"]
    assert resource["pipelines"]["hybrid"]["type"] == "hybrid"
    assert resource["fields"]["filterable_fields"] == {"knowledge_id": "knowledge_id"}


def test_hybrid_verifier_declares_bm25_sparse_index_and_scope_filter() -> None:
    verifier = (PROJECT_ROOT / "poc" / "phase1" / "milvus" / "verify_hybrid.py").read_text(encoding="utf-8")

    assert "FunctionType.BM25" in verifier
    assert 'index_type="SPARSE_INVERTED_INDEX"' in verifier
    assert 'metric_type="BM25"' in verifier
    assert 'SCOPE_FILTER = f\'knowledge_id == "{KNOWLEDGE_ID}"\'' in verifier


def _load_generated_module(path: Path) -> object:
    """从临时目录导入生成 Agent，确认它不依赖 Scaffold 源码目录。"""
    module_name = "phase1_generated_product_handbook_agent"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module
