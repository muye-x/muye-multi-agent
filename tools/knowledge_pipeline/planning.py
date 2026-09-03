"""Schema Proposal、Milvus 计划和 Resource Manifest 的确定性 Planner。"""

from __future__ import annotations

from contracts.models import (
    CollectionFieldPlanV1,
    CollectionIndexPlanV1,
    KnowledgeResourceManifestV1,
    MilvusIndexPlanV1,
    ParsedDocumentV1,
    PublishedPipelineV1,
    ResourceFieldMappingV1,
    SchemaMetadataFieldV1,
    SchemaProposalV1,
)

from .checksums import canonical_checksum
from .models import KnowledgeSourceConfigV1


def document_set_checksum(documents: list[ParsedDocumentV1]) -> str:
    """计算解析规范化产物的稳定集合 checksum，源文件顺序固定后可重复。"""
    payload = [
        document.model_dump(mode="json")
        for document in sorted(documents, key=lambda item: (item.source_path, item.document_id))
    ]
    return canonical_checksum(payload)


def build_schema_proposal(
    config: KnowledgeSourceConfigV1,
    documents: list[ParsedDocumentV1],
) -> SchemaProposalV1:
    """从受控配置和解析结果构造不可执行的 Schema Proposal。"""
    if not documents:
        raise ValueError("无法为空知识集生成 Schema Proposal")
    version = documents[0].knowledge_version_id
    if any(document.knowledge_version_id != version for document in documents):
        raise ValueError("Schema Proposal 的文档必须属于同一 KnowledgeVersion")
    payload = {
        "schema_version": "muye.ai/schema-proposal/v1",
        "knowledge_id": config.knowledge_id,
        "knowledge_version_id": version,
        "proposal_revision": f"proposal/{version}",
        "parser_profile": config.parser_profile,
        "embedding_alias": config.embedding_alias,
        "embedding_dimensions": config.embedding_dimensions,
        "chunking": config.chunking.model_dump(mode="json"),
        "metadata_fields": [
            {"name": "title", "type": "string", "filterable": False, "returnable": True},
            {"name": "source_file_id", "type": "string", "filterable": True, "returnable": False},
        ],
        "document_set_checksum": document_set_checksum(documents),
    }
    return SchemaProposalV1.model_validate(
        {**payload, "proposal_checksum": canonical_checksum(payload)}
    )


def build_collection_index_plan(proposal: SchemaProposalV1) -> CollectionIndexPlanV1:
    """从已确认的逻辑提案派生固定字段、BM25 Function 与两类索引。"""
    collection_prefix = proposal.knowledge_id.replace(".", "_").replace("-", "_")
    collection_name = f"{collection_prefix}_{proposal.knowledge_version_id.removeprefix('kv_')}"
    fields = [
        _varchar("chunk_id", 128, primary_key=True),
        _varchar("knowledge_version_id", 128),
        _varchar("document_id", 128),
        _varchar("source_file_id", 128),
        _varchar("content", 65_535, analyzer=True),
        CollectionFieldPlanV1(name="embedding", data_type="FLOAT_VECTOR", dimension=proposal.embedding_dimensions),
        CollectionFieldPlanV1(name="sparse_embedding", data_type="SPARSE_FLOAT_VECTOR"),
        _varchar("title", 512),
        _varchar("citation_id", 128),
        CollectionFieldPlanV1(name="source_locators", data_type="JSON"),
        CollectionFieldPlanV1(name="block_ids", data_type="JSON"),
        CollectionFieldPlanV1(name="chunk_index", data_type="INT64"),
        _varchar("content_hash", 64),
        CollectionFieldPlanV1(name="metadata", data_type="JSON"),
    ]
    payload = {
        "schema_version": "muye.ai/collection-index-plan/v1",
        "knowledge_id": proposal.knowledge_id,
        "knowledge_version_id": proposal.knowledge_version_id,
        "plan_revision": f"plan/{proposal.knowledge_version_id}",
        "collection_name": collection_name,
        "fields": [field.model_dump(mode="json") for field in fields],
        "bm25_function_name": "bm25_content",
        "indexes": [
            {"field_name": "embedding", "index_type": "FLAT", "metric_type": "COSINE"},
            {
                "field_name": "sparse_embedding",
                "index_type": "SPARSE_INVERTED_INDEX",
                "metric_type": "BM25",
            },
        ],
    }
    return CollectionIndexPlanV1.model_validate({**payload, "plan_checksum": canonical_checksum(payload)})


def build_resource_manifest(
    config: KnowledgeSourceConfigV1,
    proposal: SchemaProposalV1,
    plan: CollectionIndexPlanV1,
) -> KnowledgeResourceManifestV1:
    """构造未激活 Resource Manifest；运行时只通过逻辑 ID 访问它。"""
    pipelines: dict[str, PublishedPipelineV1] = {
        "dense": PublishedPipelineV1(type="dense", candidate_k=50),
        "keyword": PublishedPipelineV1(type="keyword", candidate_k=50),
        "hybrid": PublishedPipelineV1(
            type="hybrid",
            dense_candidate_k=50,
            keyword_candidate_k=50,
            dense_weight=1.0,
            keyword_weight=1.0,
            rank_constant=60,
        ),
    }
    if config.rerank_alias is not None:
        pipelines["hybrid_rerank"] = PublishedPipelineV1(
            type="hybrid",
            dense_candidate_k=50,
            keyword_candidate_k=50,
            dense_weight=1.0,
            keyword_weight=1.0,
            rank_constant=60,
            rerank_model=config.rerank_alias,
            rerank_required=config.rerank_required,
        )
    payload = {
        "schema_version": "muye.ai/knowledge-resource-manifest/v1",
        "resource_id": config.resource_id,
        "resource_revision": f"resource/{proposal.knowledge_version_id}",
        "knowledge_id": config.knowledge_id,
        "knowledge_version_id": proposal.knowledge_version_id,
        "collection_plan_checksum": plan.plan_checksum,
        "connection": config.connection,
        "target": plan.collection_name,
        "fields": ResourceFieldMappingV1(
            id="chunk_id",
            content="content",
            vector="embedding",
            keyword="sparse_embedding",
            exposed_fields={
                "title": "title",
                "source": "source_file_id",
                "citation_id": "citation_id",
                "source_locator": "source_locators",
                "source_locators": "source_locators",
                "source_file_id": "source_file_id",
                "source_asset_id": "metadata.source_asset_id",
                "knowledge_version_id": "knowledge_version_id",
            },
            filterable_fields={"knowledge_version_id": "knowledge_version_id"},
        ).model_dump(mode="json"),
        "embedding_alias": config.embedding_alias,
        "embedding_dimensions": config.embedding_dimensions,
        "pipelines": {name: value.model_dump(mode="json") for name, value in pipelines.items()},
        "default_pipeline": config.default_pipeline,
        "default_return_fields": [
            "title",
            "source",
            "citation_id",
            "source_locator",
            "source_locators",
            "knowledge_version_id",
        ],
    }
    return KnowledgeResourceManifestV1.model_validate(
        {**payload, "resource_checksum": canonical_checksum(payload)}
    )


def _varchar(name: str, max_length: int, *, primary_key: bool = False, analyzer: bool = False) -> CollectionFieldPlanV1:
    """构造字符串字段，集中避免手写时遗漏安全约束。"""
    return CollectionFieldPlanV1(
        name=name,
        data_type="VARCHAR",
        primary_key=primary_key,
        max_length=max_length,
        enable_analyzer=analyzer,
    )
