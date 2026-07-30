"""在本地 Milvus Standalone 中重放阶段 1 的 Hybrid 检索验证。

该工具仅用于阶段 1 的开发环境。它创建固定的 PoC Collection、写入确定性测试数据，
并验证 Dense、BM25/Sparse 与 RRF Hybrid 检索都遵守 ``knowledge_id`` 过滤条件。生产
应用仍只能通过 ``muye-data`` 的只读接口访问已发布资源。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from typing import NoReturn

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)


COLLECTION_NAME = "phase1_product_handbook_v1"
KNOWLEDGE_ID = "kb.product_handbook"
SCOPE_FILTER = f'knowledge_id == "{KNOWLEDGE_ID}"'
EXPECTED_CHUNK_ID = "chunk_refund_policy"

_DOCUMENTS = [
    {
        "chunk_id": EXPECTED_CHUNK_ID,
        "knowledge_id": KNOWLEDGE_ID,
        "content": "OrbitDesk refund policy allows returns within fourteen days of purchase.",
        "embedding": [1.0, 0.0, 0.0, 0.0],
        "title": "退款政策",
        "source": "product-handbook.md#refunds",
        "citation_id": "citation-refund-policy",
    },
    {
        "chunk_id": "chunk_security",
        "knowledge_id": KNOWLEDGE_ID,
        "content": "OrbitDesk supports single sign-on and audit logs for administrators.",
        "embedding": [0.0, 1.0, 0.0, 0.0],
        "title": "安全管理",
        "source": "product-handbook.md#security",
        "citation_id": "citation-security",
    },
    {
        "chunk_id": "chunk_outside_scope",
        "knowledge_id": "kb.unrelated",
        "content": "Refund policy allows returns within fourteen days of purchase.",
        "embedding": [1.0, 0.0, 0.0, 0.0],
        "title": "不相关知识库",
        "source": "unrelated.md#refunds",
        "citation_id": "citation-outside-scope",
    },
]


def parse_args() -> argparse.Namespace:
    """解析本地 Milvus 地址及显式重建开关。"""
    parser = argparse.ArgumentParser(description="验证阶段 1 Milvus Dense + BM25/Sparse Hybrid 检索")
    parser.add_argument("--uri", default="http://127.0.0.1:19530", help="Milvus Standalone URI")
    parser.add_argument(
        "--reset",
        action="store_true",
        help=f"删除并重新创建临时 Collection {COLLECTION_NAME!r}",
    )
    return parser.parse_args()


def create_collection(client: MilvusClient) -> None:
    """创建带 BM25 Function、Dense 与 Sparse 索引的固定 PoC Collection。"""
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("knowledge_id", DataType.VARCHAR, max_length=128)
    schema.add_field("content", DataType.VARCHAR, max_length=4096, enable_analyzer=True)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=4)
    schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field("title", DataType.VARCHAR, max_length=256)
    schema.add_field("source", DataType.VARCHAR, max_length=512)
    schema.add_field("citation_id", DataType.VARCHAR, max_length=128)
    schema.add_function(
        Function(
            name="phase1_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["content"],
            output_field_names=["sparse_embedding"],
        )
    )

    indexes = client.prepare_index_params()
    indexes.add_index(field_name="embedding", index_type="FLAT", metric_type="COSINE")
    indexes.add_index(
        field_name="sparse_embedding",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    client.create_collection(collection_name=COLLECTION_NAME, schema=schema, index_params=indexes)
    client.insert(collection_name=COLLECTION_NAME, data=_DOCUMENTS)
    client.flush(collection_name=COLLECTION_NAME)
    client.load_collection(collection_name=COLLECTION_NAME)


def first_hit_id(results: object, *, query_name: str) -> str:
    """返回单查询首个命中的 chunk ID，并在空结果时提供可诊断错误。"""
    try:
        normalized_results = list(results)  # type: ignore[arg-type]
    except TypeError as exc:
        raise RuntimeError(f"{query_name} 返回了不支持的结果类型") from exc
    if not normalized_results:
        raise RuntimeError(f"{query_name} 未返回可用命中")
    try:
        first_group = list(normalized_results[0])
    except TypeError as exc:
        raise RuntimeError(f"{query_name} 返回了不支持的命中组类型") from exc
    if not first_group:
        raise RuntimeError(f"{query_name} 未返回可用命中")
    first_hit = first_group[0]
    if not isinstance(first_hit, Mapping) or not isinstance(first_hit.get("chunk_id"), str):
        raise RuntimeError(f"{query_name} 未返回可用命中")
    return first_hit["chunk_id"]


def verify_hybrid(client: MilvusClient) -> dict[str, str]:
    """执行三类真实检索并断言固定 scope 内的预期文档均排名第一。"""
    output_fields = ["chunk_id", "knowledge_id", "title", "citation_id"]
    dense_request = AnnSearchRequest(
        data=[[1.0, 0.0, 0.0, 0.0]],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {}},
        limit=3,
        filter=SCOPE_FILTER,
    )
    sparse_request = AnnSearchRequest(
        data=["refund policy fourteen days"],
        anns_field="sparse_embedding",
        param={"metric_type": "BM25", "params": {}},
        limit=3,
        filter=SCOPE_FILTER,
    )
    dense = client.search(
        collection_name=COLLECTION_NAME,
        data=[[1.0, 0.0, 0.0, 0.0]],
        anns_field="embedding",
        search_params={"metric_type": "COSINE", "params": {}},
        filter=SCOPE_FILTER,
        limit=3,
        output_fields=output_fields,
    )
    sparse = client.search(
        collection_name=COLLECTION_NAME,
        data=["refund policy fourteen days"],
        anns_field="sparse_embedding",
        search_params={"metric_type": "BM25", "params": {}},
        filter=SCOPE_FILTER,
        limit=3,
        output_fields=output_fields,
    )
    hybrid = client.hybrid_search(
        collection_name=COLLECTION_NAME,
        reqs=[dense_request, sparse_request],
        ranker=RRFRanker(k=60),
        limit=3,
        output_fields=output_fields,
    )
    results = {
        "dense": first_hit_id(dense, query_name="Dense"),
        "sparse": first_hit_id(sparse, query_name="BM25/Sparse"),
        "hybrid": first_hit_id(hybrid, query_name="RRF Hybrid"),
    }
    unexpected = {name: chunk_id for name, chunk_id in results.items() if chunk_id != EXPECTED_CHUNK_ID}
    if unexpected:
        raise RuntimeError(f"阶段 1 Hybrid 验证失败：{unexpected!r}")
    return results


def main() -> int:
    """重建可选的 PoC Collection，并输出可审计的三路首位命中结果。"""
    args = parse_args()
    client = MilvusClient(uri=args.uri, timeout=30)
    exists = client.has_collection(collection_name=COLLECTION_NAME)
    if exists and not args.reset:
        raise RuntimeError(f"{COLLECTION_NAME} 已存在；使用 --reset 显式重建临时 PoC 数据")
    if exists:
        client.drop_collection(collection_name=COLLECTION_NAME)
    create_collection(client)
    results = verify_hybrid(client)
    print(f"phase1 hybrid verification passed: {results}")
    return 0


def fail(message: str) -> NoReturn:
    """以稳定的 CLI 错误形式退出，避免显示无上下文 traceback。"""
    print(f"phase1 hybrid verification failed: {message}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        fail(str(exc))
