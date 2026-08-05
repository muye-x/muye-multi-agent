"""阶段 1 Markdown 解析与一次性 Agent 目录 PoC 测试。"""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from contracts.models import AgentDescriptorV1, SourceProvenanceV1
from poc.phase1.contracts import Phase1PocConfigV1
from poc.phase1.generator import generate_agent_directory
from poc.phase1.markdown import parse_markdown_file
from poc.phase1.milvus.verify_hybrid import (
    DEFAULT_MILVUS_URI,
    EMBEDDING_DIMENSIONS,
    KNOWLEDGE_ID,
    _DOCUMENTS,
    assert_results_scoped,
    validate_local_milvus_uri,
)
from poc.phase1.profile import build_generation_spec, build_profile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = PROJECT_ROOT / "poc" / "phase1" / "samples"


def _poc_inputs() -> tuple[object, object, Phase1PocConfigV1]:
    document = parse_markdown_file(SAMPLE_ROOT / "product-handbook.md", source_root=SAMPLE_ROOT)
    profile = build_profile(document)
    config = Phase1PocConfigV1(
        schema_version="muye.ai/phase1-poc-config/v1",
        agent_id="agent_product_handbook",
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
    assert descriptor.runtime.token_budget == spec.token_budget
    assert descriptor.runtime.tool_budget == spec.tool_budget
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


@pytest.mark.parametrize(
    ("field_name", "replacement", "expected_spec_field"),
    [
        ("agent_id", "agent_alternate_handbook", "agent_id"),
        ("resource_revision", "resource/phase1-v2", "resource_revision"),
        ("model_alias", "chat-alternate", "model_alias"),
        ("scope_value", "kb.attacker", "scope_filter_ref"),
    ],
)
def test_poc_generator_rejects_config_that_disagrees_with_generation_spec(
    tmp_path: Path,
    field_name: str,
    replacement: str,
    expected_spec_field: str,
) -> None:
    """源码依赖的每项 config 都必须与 provenance 所用 spec 完整一致。"""
    document, profile, config = _poc_inputs()
    spec = build_generation_spec(document=document, profile=profile, config=config)  # type: ignore[arg-type]
    inconsistent_config = config.model_copy(update={field_name: replacement})

    with pytest.raises(ValueError, match=expected_spec_field):
        generate_agent_directory(
            target_parent=tmp_path,
            generation_spec=spec,
            profile=profile,  # type: ignore[arg-type]
            document=document,  # type: ignore[arg-type]
            config=inconsistent_config,
        )

    assert list(tmp_path.iterdir()) == []


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
    assert resource["embedding"]["dimensions"] == EMBEDDING_DIMENSIONS
    assert resource["pipelines"]["hybrid"]["type"] == "hybrid"
    assert resource["fields"]["filterable_fields"] == {"knowledge_id": "knowledge_id"}
    assert all(len(document["embedding"]) == EMBEDDING_DIMENSIONS for document in _DOCUMENTS)


def test_local_milvus_start_script_generates_ignored_credentials_and_uses_compose_env_file() -> None:
    """本地启动器不得在 Compose 或脚本中硬编码 MinIO 凭据。"""
    script = PROJECT_ROOT / "poc" / "phase1" / "milvus" / "start-local.sh"

    assert script.is_file()
    assert script.stat().st_mode & 0o111
    syntax_check = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)
    assert syntax_check.returncode == 0, syntax_check.stderr

    source = script.read_text(encoding="utf-8")
    assert "secrets.token_urlsafe" in source
    assert "--env-file" in source
    assert "umask 077" in source
    assert "chmod 600" in source


def test_hybrid_verifier_declares_bm25_sparse_index_and_scope_filter() -> None:
    verifier = (PROJECT_ROOT / "poc" / "phase1" / "milvus" / "verify_hybrid.py").read_text(encoding="utf-8")

    assert "FunctionType.BM25" in verifier
    assert 'index_type="SPARSE_INVERTED_INDEX"' in verifier
    assert 'metric_type="BM25"' in verifier
    assert 'SCOPE_FILTER = f\'knowledge_id == "{KNOWLEDGE_ID}"\'' in verifier


@pytest.mark.parametrize(
    "uri",
    [
        "https://127.0.0.1:19530",
        "http://milvus.example.test:19530",
        "http://127.0.0.1:19531",
        "http://127.0.0.1:19530/other",
    ],
)
def test_hybrid_verifier_rejects_non_local_or_non_default_milvus_uri(uri: str) -> None:
    with pytest.raises(ValueError, match="仅允许本机"):
        validate_local_milvus_uri(uri)


def test_hybrid_verifier_scope_assertion_checks_every_hit() -> None:
    assert validate_local_milvus_uri(DEFAULT_MILVUS_URI) == DEFAULT_MILVUS_URI
    assert_results_scoped(
        [[{"chunk_id": "chunk_refund_policy", "knowledge_id": KNOWLEDGE_ID}]],
        query_name="Dense",
    )

    with pytest.raises(RuntimeError, match="scope 外"):
        assert_results_scoped(
            [[{"chunk_id": "chunk_outside_scope", "knowledge_id": "kb.unrelated"}]],
            query_name="Dense",
        )


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
