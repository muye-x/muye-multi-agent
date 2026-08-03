"""阶段 4 Milvus Publisher 的真实 Standalone 回归，默认不进入 CI。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from contracts.models import ChunkingPolicyV1, SchemaProposalV1
from tools.knowledge_pipeline.milvus_publisher import MilvusPublisher
from tools.knowledge_pipeline.models import KnowledgeChunkV1
from tools.knowledge_pipeline.planning import build_collection_index_plan


pytestmark = pytest.mark.skipif(
    os.environ.get("MUYE_RUN_MILVUS_INTEGRATION") != "1",
    reason="set MUYE_RUN_MILVUS_INTEGRATION=1 to run against local Milvus Standalone",
)


def test_publisher_creates_and_verifies_dense_bm25_collection() -> None:
    """创建临时 Collection，验证 Plan checksum、Dense、BM25/Sparse 和清理范围。"""
    from pymilvus import MilvusClient

    uri = os.environ.get("MUYE_MILVUS_INTEGRATION_URI", "http://127.0.0.1:19530")
    proposal = SchemaProposalV1(
        schema_version="muye.ai/schema-proposal/v1",
        knowledge_id="kb.phase4verify",
        knowledge_version_id="kv_20260730verify",
        proposal_revision="proposal/kv_20260730verify",
        proposal_checksum="a" * 64,
        parser_profile="docling-default-v1",
        embedding_alias="embed-test",
        embedding_dimensions=4,
        chunking=ChunkingPolicyV1(max_characters=200, overlap_characters=20, min_characters=20),
        document_set_checksum="b" * 64,
    )
    plan = build_collection_index_plan(proposal).model_copy(
        update={"collection_name": f"phase4_verify_{uuid4().hex[:12]}"}
    )
    chunk = KnowledgeChunkV1(
        chunk_id="chunk_0123456789abcdef",
        knowledge_version_id=plan.knowledge_version_id,
        document_id="doc_0123456789abcdef",
        source_file_id="file_0123456789abcdef",
        content="员工请假需要提前填写请假申请单并逐级审批。",
        title="Phase 4 verification",
        citation_id="citation_0123456789abcdef",
        source_locators=[{"source_path": "verification.md", "kind": "line", "start": 1, "end": 1}],
        block_ids=["block_0123456789abcdef"],
        chunk_index=0,
        content_hash="c" * 64,
    )
    client = MilvusClient(uri=uri, timeout=30)
    try:
        publisher = MilvusPublisher(uri=uri)
        publisher.publish(plan=plan, chunks=[chunk], embeddings=[[1.0, 0.0, 0.0, 0.0]])
        publisher.publish(plan=plan, chunks=[chunk], embeddings=[[1.0, 0.0, 0.0, 0.0]])
        description = client.describe_collection(collection_name=plan.collection_name)
        assert description["description"] == f"muye-collection-plan:{plan.plan_checksum}"
        dense = client.search(
            collection_name=plan.collection_name,
            data=[[1.0, 0.0, 0.0, 0.0]],
            anns_field="embedding",
            search_params={"metric_type": "COSINE", "params": {}},
            limit=1,
            output_fields=["chunk_id"],
        )
        sparse = client.search(
            collection_name=plan.collection_name,
            data=["请假申请单"],
            anns_field="sparse_embedding",
            search_params={"metric_type": "BM25", "params": {}},
            limit=1,
            output_fields=["chunk_id"],
        )
        assert dense[0][0]["chunk_id"] == chunk.chunk_id
        assert sparse[0][0]["chunk_id"] == chunk.chunk_id
    finally:
        if client.has_collection(collection_name=plan.collection_name):
            client.drop_collection(collection_name=plan.collection_name)
        client.close()
