"""生成 Agent 的真实检索评测；默认不配置服务时跳过。"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest


EVALUATION_SET = {'schema_version': 'muye.ai/evaluation-set/v1', 'evaluation_set_id': 'hotel_employee_eval', 'revision': 'evaluation/hotel-employee@1', 'checksum': 'ff02f4804bcb20ac65aa42206a7c8e80236f2387211ddfd61d522ee70cf10544', 'recall_at_k': 5, 'min_recall': 0.8, 'min_mrr': 0.6, 'min_citation_coverage': 1.0, 'cases': [{'case_id': 'case_001', 'query': '入职需要提交哪些证件？', 'relevant_chunk_ids': ['chunk_812c80c742067fb401c2f3b25ec66209'], 'required_citation_ids': ['citation_4cb465c52e3c8e0a7b67792d37909b6e']}, {'case_id': 'case_002', 'query': '转正考核不合格会怎样？', 'relevant_chunk_ids': ['chunk_51a4d57e370f3dd7682ea1e8b67675ef'], 'required_citation_ids': ['citation_81416de5efc6f8709f559bbd587f2b35']}, {'case_id': 'case_003', 'query': '晋升主要考核什么？', 'relevant_chunk_ids': ['chunk_60f7af247ba5a9a09811273c49ee93d7'], 'required_citation_ids': ['citation_77b7f260c10977e911c47706031939c3']}, {'case_id': 'case_004', 'query': '什么情况下会被立即辞退？', 'relevant_chunk_ids': ['chunk_dfdb812e70dfb48244a666da833ea54b'], 'required_citation_ids': ['citation_a7cda2af7bcd86670c6c93c919fe66b8']}, {'case_id': 'case_005', 'query': '离职工资什么时候发放？', 'relevant_chunk_ids': ['chunk_4e6fee2d0900953a692e27f8221ea456'], 'required_citation_ids': ['citation_fd22af93316873dee3b6bc4ef3b4fb06']}, {'case_id': 'case_006', 'query': '薪资升降级依据什么？', 'relevant_chunk_ids': ['chunk_ac8f58c6a43b01cdf0616447481af543'], 'required_citation_ids': ['citation_a06de2891d0d12b23f9bb151873b8f78']}, {'case_id': 'case_007', 'query': '排班提前多久公示？可以私自调班吗？', 'relevant_chunk_ids': ['chunk_eb2cafdd413e4598a693354d20a9ad37'], 'required_citation_ids': ['citation_278a0ac7c9de8e2257b574c50096f30d']}, {'case_id': 'case_008', 'query': '加班如何补偿？', 'relevant_chunk_ids': ['chunk_61a9fc99717abddd625528b5a3deca6d'], 'required_citation_ids': ['citation_d4d77a173e4784e8815108a92ac53a05']}, {'case_id': 'case_009', 'query': '请假需要什么流程？', 'relevant_chunk_ids': ['chunk_a2200e568b47de1cdabc34966473930e'], 'required_citation_ids': ['citation_483943d3e051bcf943a1bc38afb84cc0']}, {'case_id': 'case_010', 'query': '事假每月最多能请几天？', 'relevant_chunk_ids': ['chunk_a2200e568b47de1cdabc34966473930e'], 'required_citation_ids': ['citation_483943d3e051bcf943a1bc38afb84cc0']}, {'case_id': 'case_011', 'query': '工伤假期间工资怎么发？', 'relevant_chunk_ids': ['chunk_99db5689f59728d58b5c3b1e43d2430a'], 'required_citation_ids': ['citation_403beab62702d91f15abd98991e118ed']}, {'case_id': 'case_012', 'query': '产假需要什么条件？', 'relevant_chunk_ids': ['chunk_f50337e72997a4072df8a81847f6717b'], 'required_citation_ids': ['citation_8b2d62854ea3f0593e43de76d030176d']}]}
RESOURCE_ID = "kb.hotel_employee"
DATA_BASE_URL = os.environ.get("MUYE_TEST_DATA_BASE_URL", "").strip().rstrip("/")
DATA_TOKEN = os.environ.get("MUYE_TEST_DATA_TOKEN", "").strip()


def _data_url() -> str:
    """读取显式测试数据服务地址，避免普通单测意外访问网络。"""
    if EVALUATION_SET is None:
        pytest.skip("该 Agent 未固化评测集；请通过 agent create 重新生成")
    if not DATA_BASE_URL:
        pytest.skip("设置 MUYE_TEST_DATA_BASE_URL 后运行真实检索评测")
    parsed = urlsplit(DATA_BASE_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        pytest.fail("MUYE_TEST_DATA_BASE_URL 必须是不含凭据的 HTTP(S) 地址")
    return DATA_BASE_URL


def _headers() -> dict[str, str]:
    """可选地将本地测试的独立 Data token 附在请求头中。"""
    return {"Authorization": f"Bearer {DATA_TOKEN}"} if DATA_TOKEN else {}


def _retrieve(client: httpx.Client, *, query: str, pipeline: str, trace_id: str) -> list[dict[str, Any]]:
    """调用公开检索 API，并将响应收紧为评测所需的稳定字段。"""
    try:
        response = client.post(
            "/api/v1/retrieve",
            json={
                "resource": RESOURCE_ID,
                "query": query,
                "pipeline": pipeline,
                "top_k": EVALUATION_SET["recall_at_k"],
                "return_fields": ["citation_id"],
                "trace_id": trace_id,
            },
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"muye-data 检索请求失败（{type(exc).__name__}）；请确认测试服务已在 {DATA_BASE_URL} 就绪")
    payload = response.json()
    hits = payload.get("hits") if isinstance(payload, dict) else None
    assert isinstance(hits, list), "muye-data 响应必须包含 hits 数组"
    normalized: list[dict[str, Any]] = []
    for hit in hits:
        assert isinstance(hit, dict) and isinstance(hit.get("id"), str), "命中必须包含稳定 chunk id"
        fields = hit.get("fields")
        citation_id = fields.get("citation_id") if isinstance(fields, dict) else None
        assert citation_id is None or isinstance(citation_id, str), "citation_id 必须是字符串"
        normalized.append({"id": hit["id"], "citation_id": citation_id})
    return normalized


def test_retrieval_evaluation() -> None:
    """对 Dense、Keyword、Hybrid 计算创建时固化的 Recall、MRR 与引用覆盖率。"""
    base_url = _data_url()
    failures: list[str] = []
    with httpx.Client(base_url=base_url, timeout=30.0, headers=_headers(), trust_env=False) as client:
        for pipeline in ("dense", "keyword", "hybrid"):
            recall_total = 0.0
            reciprocal_rank_total = 0.0
            citation_total = 0.0
            for case in EVALUATION_SET["cases"]:
                hits = _retrieve(
                    client,
                    query=case["query"],
                    pipeline=pipeline,
                    trace_id=f"generated-retrieval-{pipeline}-{case['case_id']}",
                )
                expected_chunks = set(case["relevant_chunk_ids"])
                actual_chunks = [hit["id"] for hit in hits]
                recall_total += len(expected_chunks.intersection(actual_chunks)) / len(expected_chunks)
                first_rank = next((index for index, chunk_id in enumerate(actual_chunks, start=1) if chunk_id in expected_chunks), None)
                reciprocal_rank_total += 0.0 if first_rank is None else 1.0 / first_rank
                expected_citations = set(case["required_citation_ids"])
                actual_citations = {hit["citation_id"] for hit in hits if hit["citation_id"] is not None}
                citation_total += 1.0 if not expected_citations else len(expected_citations.intersection(actual_citations)) / len(expected_citations)
            case_count = len(EVALUATION_SET["cases"])
            recall = recall_total / case_count
            mrr = reciprocal_rank_total / case_count
            citation_coverage = citation_total / case_count
            if recall < EVALUATION_SET["min_recall"] or mrr < EVALUATION_SET["min_mrr"] or citation_coverage < EVALUATION_SET["min_citation_coverage"]:
                failures.append(
                    f"{pipeline}: recall={recall:.3f}, mrr={mrr:.3f}, citation_coverage={citation_coverage:.3f}"
                )
    assert not failures, "检索评测未达创建时门禁：" + "; ".join(failures)
