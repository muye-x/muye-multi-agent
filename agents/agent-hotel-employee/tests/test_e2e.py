"""生成 Agent 的真实问答抽测；默认不配置服务时跳过。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from secrets import token_hex
import sys
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest


EVALUATION_SET = {'schema_version': 'muye.ai/evaluation-set/v1', 'evaluation_set_id': 'hotel_employee_eval', 'revision': 'evaluation/hotel-employee@1', 'checksum': 'caaf9508747d15a0179a33f1bf41fe2b670ee7c06f1a9090885cd16e2c615964', 'recall_at_k': 5, 'min_recall': 0.8, 'min_mrr': 0.6, 'min_citation_coverage': 1.0, 'cases': [{'case_id': 'case_001', 'query': '入职需要提交哪些证件？', 'relevant_chunk_ids': ['chunk_812c80c742067fb401c2f3b25ec66209'], 'required_citation_ids': ['citation_4cb465c52e3c8e0a7b67792d37909b6e']}, {'case_id': 'case_002', 'query': '转正考核不合格会怎样？', 'relevant_chunk_ids': ['chunk_51a4d57e370f3dd7682ea1e8b67675ef'], 'required_citation_ids': ['citation_81416de5efc6f8709f559bbd587f2b35']}, {'case_id': 'case_003', 'query': '晋升主要考核什么？', 'relevant_chunk_ids': ['chunk_60f7af247ba5a9a09811273c49ee93d7'], 'required_citation_ids': ['citation_77b7f260c10977e911c47706031939c3']}, {'case_id': 'case_004', 'query': '什么情况下会被立即辞退？', 'relevant_chunk_ids': ['chunk_dfdb812e70dfb48244a666da833ea54b'], 'required_citation_ids': ['citation_a7cda2af7bcd86670c6c93c919fe66b8']}, {'case_id': 'case_005', 'query': '离职工资什么时候发放？', 'relevant_chunk_ids': ['chunk_4e6fee2d0900953a692e27f8221ea456'], 'required_citation_ids': ['citation_fd22af93316873dee3b6bc4ef3b4fb06']}, {'case_id': 'case_006', 'query': '薪资升降级依据什么？', 'relevant_chunk_ids': ['chunk_ac8f58c6a43b01cdf0616447481af543'], 'required_citation_ids': ['citation_a06de2891d0d12b23f9bb151873b8f78']}, {'case_id': 'case_007', 'query': '排班提前多久公示？可以私自调班吗？', 'relevant_chunk_ids': ['chunk_eb2cafdd413e4598a693354d20a9ad37'], 'required_citation_ids': ['citation_278a0ac7c9de8e2257b574c50096f30d']}, {'case_id': 'case_008', 'query': '加班如何补偿？', 'relevant_chunk_ids': ['chunk_61a9fc99717abddd625528b5a3deca6d'], 'required_citation_ids': ['citation_d4d77a173e4784e8815108a92ac53a05']}, {'case_id': 'case_009', 'query': '请事假需要提前多久申请？每月事假有上限吗？', 'relevant_chunk_ids': ['chunk_a2200e568b47de1cdabc34966473930e'], 'required_citation_ids': ['citation_483943d3e051bcf943a1bc38afb84cc0']}, {'case_id': 'case_010', 'query': '工伤假期间工资怎么发？', 'relevant_chunk_ids': ['chunk_99db5689f59728d58b5c3b1e43d2430a'], 'required_citation_ids': ['citation_403beab62702d91f15abd98991e118ed']}, {'case_id': 'case_011', 'query': '产假需要什么条件？', 'relevant_chunk_ids': ['chunk_f50337e72997a4072df8a81847f6717b'], 'required_citation_ids': ['citation_8b2d62854ea3f0593e43de76d030176d']}, {'case_id': 'case_012', 'query': '员工可以使用客用设施吗？', 'relevant_chunk_ids': ['chunk_25dab03732d9449c93a61d8a4baebfac'], 'required_citation_ids': ['citation_4a1f4d3a92f4fbb43627ca1701545e21']}]}
DATA_BASE_URL = os.environ.get("MUYE_TEST_DATA_BASE_URL", "").strip().rstrip("/")
LLM_BASE_URL = os.environ.get("MUYE_TEST_LLM_BASE_URL", "").strip().rstrip("/")
DATA_TOKEN = os.environ.get("MUYE_TEST_DATA_TOKEN", "").strip()


def _required_test_url(value: str, name: str) -> str:
    """只接受显式的无凭据本地测试服务地址。"""
    if not value:
        pytest.skip(f"设置 {name} 后运行真实端到端问答抽测")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        pytest.fail(f"{name} 必须是不含凭据的 HTTP(S) 地址")
    return value


def _assert_data_service_ready() -> None:
    """在调用模型前确认测试检索服务可达，避免模型无检索依据时返回误导性文字。"""
    base_url = _required_test_url(DATA_BASE_URL, "MUYE_TEST_DATA_BASE_URL")
    try:
        response = httpx.get(f"{base_url}/ready", timeout=5.0, trust_env=False)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"muye-data 未就绪（{type(exc).__name__}）；请确认测试服务已在 {base_url} 启动")


def _load_application(monkeypatch: pytest.MonkeyPatch):
    """在隔离环境中加载内部 HTTP 入口，避免测试凭据写入 Agent .env。"""
    main_token, control_token, data_token = token_hex(32), token_hex(32), DATA_TOKEN or token_hex(32)
    while len({main_token, control_token, data_token}) != 3:
        control_token = token_hex(32)
    monkeypatch.setenv("MUYE_LLM_BASE_URL", _required_test_url(LLM_BASE_URL, "MUYE_TEST_LLM_BASE_URL"))
    monkeypatch.setenv("MUYE_SDK_DATA_BASE_URL", _required_test_url(DATA_BASE_URL, "MUYE_TEST_DATA_BASE_URL"))
    monkeypatch.setenv("MUYE_SDK_API_PROFILES", "internal")
    monkeypatch.setenv("MUYE_AGENT_MAIN_TOKEN", main_token)
    monkeypatch.setenv("MUYE_AGENT_CONTROL_TOKEN", control_token)
    monkeypatch.setenv("MUYE_AGENT_DATA_TOKEN", data_token)
    monkeypatch.setenv("MUYE_AGENT_SERVICE_ID", "local-generated-test")
    monkeypatch.setenv("MUYE_AGENT_DEPLOYMENT_ID", "local-generated-test")
    monkeypatch.setenv("MUYE_AGENT_DESCRIPTOR_CHECKSUM", "a" * 64)
    monkeypatch.setenv("MUYE_AGENT_SOURCE_TREE_CHECKSUM", "b" * 64)
    project_directory = Path(__file__).resolve().parents[1]
    module_name = "generated_agent_e2e_main"
    sys.modules.pop(module_name, None)
    sys.modules.pop("agent", None)
    sys.path.insert(0, str(project_directory))
    try:
        specification = importlib.util.spec_from_file_location(module_name, project_directory / "main.py")
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        sys.modules[module_name] = module
        specification.loader.exec_module(module)
        return module.app, main_token
    finally:
        sys.path.pop(0)


def test_internal_invoke_returns_answer_and_trusted_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    """以一个已评测问题贯通 internal /invoke、模型、scoped retrieval 和引用投影。"""
    if EVALUATION_SET is None:
        pytest.skip("该 Agent 未固化评测集；请通过 agent create 重新生成")
    _assert_data_service_ready()
    application, main_token = _load_application(monkeypatch)
    sample = EVALUATION_SET["cases"][0]
    with TestClient(application) as client:
        response = client.post(
            "/invoke",
            headers={"Authorization": f"Bearer {main_token}"},
            json={
                "task": sample["query"],
                "context": {
                    "user_id": "generated-e2e-user",
                    "session_id": "generated-e2e-session",
                    "trace_id": f"generated-e2e-{sample['case_id']}",
                },
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("status") == "success", body
    payload = body.get("payload")
    assert isinstance(payload, dict) and isinstance(payload.get("result_data"), dict), body
    assert isinstance(payload["result_data"].get("markdown"), str) and payload["result_data"]["markdown"].strip(), body
    assert body.get("tool_calls_made"), "问答抽测必须实际调用检索工具"
    citations = payload["result_data"].get("_muye_citations")
    assert isinstance(citations, list) and citations, "问答抽测必须返回可信引用"
    citation_ids = {item.get("citation_id") for item in citations if isinstance(item, dict)}
    required = set(sample["required_citation_ids"])
    assert not required or citation_ids.intersection(required), "问答抽测未命中评测问题要求的引用"
