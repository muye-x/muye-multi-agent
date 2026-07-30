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
from urllib.parse import urlsplit

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)


COLLECTION_NAME = "phase1_product_handbook_v1"
DEFAULT_MILVUS_URI = "http://127.0.0.1:19530"
EMBEDDING_DIMENSIONS = 4
KNOWLEDGE_ID = "kb.product_handbook"
SCOPE_FILTER = f'knowledge_id == "{KNOWLEDGE_ID}"'
EXPECTED_CHUNK_ID = "chunk_refund_policy"
OUTSIDE_SCOPE_CHUNK_ID = "chunk_outside_scope"


def _dense_vector(*values: float) -> list[float]:
    """构造与固定 Collection schema 维度一致的确定性测试向量。"""
    if len(values) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"阶段 1 Dense 向量必须为 {EMBEDDING_DIMENSIONS} 维")
    return list(values)


DENSE_QUERY = _dense_vector(1.0, 0.0, 0.0, 0.0)

_DOCUMENTS = [
    {
        "chunk_id": EXPECTED_CHUNK_ID,
        "knowledge_id": KNOWLEDGE_ID,
        "content": "OrbitDesk refund policy allows returns within fourteen days of purchase.",
        "embedding": _dense_vector(0.9, 0.1, 0.0, 0.0),
        "title": "退款政策",
        "source": "product-handbook.md#refunds",
        "citation_id": "citation-refund-policy",
    },
    {
        "chunk_id": "chunk_security",
        "knowledge_id": KNOWLEDGE_ID,
        "content": "OrbitDesk supports single sign-on and audit logs for administrators.",
        "embedding": _dense_vector(0.0, 1.0, 0.0, 0.0),
        "title": "安全管理",
        "source": "product-handbook.md#security",
        "citation_id": "citation-security",
    },
    {
        "chunk_id": OUTSIDE_SCOPE_CHUNK_ID,
        "knowledge_id": "kb.unrelated",
        "content": "Refund policy allows returns within fourteen days of purchase.",
        "embedding": _dense_vector(1.0, 0.0, 0.0, 0.0),
        "title": "不相关知识库",
        "source": "unrelated.md#refunds",
        "citation_id": "citation-outside-scope",
    },
]


def parse_args() -> argparse.Namespace:
    """解析本地 Milvus 地址及显式重建开关。"""
    parser = argparse.ArgumentParser(description="验证阶段 1 Milvus Dense + BM25/Sparse Hybrid 检索")
    parser.add_argument("--uri", default=DEFAULT_MILVUS_URI, help="仅允许本机 Milvus Standalone URI")
    parser.add_argument(
        "--reset",
        action="store_true",
        help=f"删除并重新创建临时 Collection {COLLECTION_NAME!r}",
    )
    return parser.parse_args()


def validate_local_milvus_uri(uri: str) -> str:
    """仅接受本机固定端口，防止 ``--reset`` 误删共享或远端 Collection。"""
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Milvus URI 必须是本机 http://127.0.0.1:19530") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port != 19530
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("阶段 1 验证仅允许本机 http://127.0.0.1:19530")
    return uri


def create_collection(client: MilvusClient) -> None:
    """创建带 BM25 Function、Dense 与 Sparse 索引的固定 PoC Collection。"""
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("knowledge_id", DataType.VARCHAR, max_length=128)
    schema.add_field("content", DataType.VARCHAR, max_length=4096, enable_analyzer=True)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSIONS)
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


def _hit_groups(results: object, *, query_name: str) -> list[list[Mapping[str, object]]]:
    """标准化 pymilvus 查询结果，并在响应不符合预期时提供诊断错误。"""
    try:
        normalized_results = list(results)  # type: ignore[arg-type]
    except TypeError as exc:
        raise RuntimeError(f"{query_name} 返回了不支持的结果类型") from exc
    if not normalized_results:
        raise RuntimeError(f"{query_name} 未返回可用命中")
    groups: list[list[Mapping[str, object]]] = []
    for result_group in normalized_results:
        try:
            hits = list(result_group)
        except TypeError as exc:
            raise RuntimeError(f"{query_name} 返回了不支持的命中组类型") from exc
        if not hits:
            raise RuntimeError(f"{query_name} 未返回可用命中")
        if any(not isinstance(hit, Mapping) for hit in hits):
            raise RuntimeError(f"{query_name} 未返回可用命中")
        groups.append(hits)
    return groups


def first_hit_id(results: object, *, query_name: str) -> str:
    """返回单查询首个命中的 chunk ID，并在空结果时提供可诊断错误。"""
    hit_groups = _hit_groups(results, query_name=query_name)
    first_hit = hit_groups[0][0]
    if not isinstance(first_hit.get("chunk_id"), str):
        raise RuntimeError(f"{query_name} 未返回可用命中")
    return first_hit["chunk_id"]


def assert_results_scoped(results: object, *, query_name: str) -> None:
    """确认每个返回命中都受固定 knowledge_id filter 约束。"""
    out_of_scope = [
        hit.get("chunk_id")
        for group in _hit_groups(results, query_name=query_name)
        for hit in group
        if hit.get("knowledge_id") != KNOWLEDGE_ID
    ]
    if out_of_scope:
        raise RuntimeError(f"{query_name} 返回了 scope 外命中：{out_of_scope!r}")


def verify_hybrid(client: MilvusClient) -> dict[str, str]:
    """执行三类真实检索并断言固定 scope 内的预期文档均排名第一。"""
    output_fields = ["chunk_id", "knowledge_id", "title", "citation_id"]
    dense_request = AnnSearchRequest(
        data=[DENSE_QUERY],
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
        data=[DENSE_QUERY],
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
    dense_without_scope = client.search(
        collection_name=COLLECTION_NAME,
        data=[DENSE_QUERY],
        anns_field="embedding",
        search_params={"metric_type": "COSINE", "params": {}},
        limit=3,
        output_fields=output_fields,
    )
    if first_hit_id(dense_without_scope, query_name="未过滤 Dense") != OUTSIDE_SCOPE_CHUNK_ID:
        raise RuntimeError("未过滤 Dense 未优先命中 scope 外对照数据，无法验证过滤门禁")
    for query_name, query_results in (
        ("Dense", dense),
        ("BM25/Sparse", sparse),
        ("RRF Hybrid", hybrid),
    ):
        assert_results_scoped(query_results, query_name=query_name)
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
    uri = validate_local_milvus_uri(args.uri)
    client = MilvusClient(uri=uri, timeout=30)
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
