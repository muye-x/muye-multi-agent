"""Agent Creation 的 LLM Profile 格式修复回归测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

import tools.agent_creation.proposals as proposals_module
from tools.agent_creation.models import AgentProjectSpecV1
from tools.agent_creation.proposals import MuyeLLMProposalClient


class _Response:
    """提供受控 Chat 响应的最小 HTTPX 替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "success": True,
            "code": 200,
            "message": "ok",
            "data": {"content": json.dumps(self._payload, ensure_ascii=False)},
        }


class _FailedResponse(_Response):
    """模拟 muye-llm 以成功 HTTP 状态返回的结构化业务失败。"""

    def json(self) -> dict[str, object]:
        return {"success": False, "code": 502, "message": "LLM 返回空内容", "data": {}}


class _Client:
    """记录请求并按顺序返回固定模型结果，绝不访问网络。"""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, _: str, *, json: dict[str, Any]) -> _Response:
        self.requests.append(json)
        return self._responses.pop(0)


def _project() -> AgentProjectSpecV1:
    return AgentProjectSpecV1.model_validate(
        {
            "schema_version": "muye.ai/agent-project/v1",
            "slug": "hotel-employee",
            "agent_id": "agent_hotel_employee",
            "display_name": "员工手册助手",
            "objective": "回答员工手册制度问题。",
            "prohibited_actions": ["不得执行审批"],
        }
    )


def _profile(*, intent_count: int, examples: object) -> dict[str, object]:
    return {
        "schema_version": "muye.ai/agent-profile-proposal/v1",
        "display_name": "员工手册助手",
        "description": "依据员工手册回答制度问题。",
        "supported_intents": [f"制度查询 {index}" for index in range(intent_count)],
        "instructions": "只依据检索结果回答。",
        "do_not_use_when": ["用户要求执行审批"],
        "examples": examples,
    }


def _proposal(profile: dict[str, object], *, case_id: object = "leave_policy") -> dict[str, object]:
    return {
        "profile": profile,
        "cases": [
            {
                "case_id": case_id,
                "query": "请事假需要提前多久申请？",
                "relevant_chunk_ids": ["chunk-1"],
            }
        ],
    }


def test_propose_repairs_invalid_profile_and_case_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型误输出 Profile 与整数 case_id 时，单次修复完整提案。"""

    initial = _proposal(_profile(intent_count=22, examples=[{"query": "请假怎么办"}]), case_id=1)
    repaired = _profile(intent_count=2, examples=["请事假需要提前多久申请？"])
    repaired_proposal = _proposal(repaired)
    client = _Client([_Response(initial), _Response(repaired_proposal)])
    monkeypatch.setattr(proposals_module.httpx, "Client", lambda **_: client)

    result = MuyeLLMProposalClient(base_url="http://muye-llm.test").propose(
        project=_project(),
        chunks=[{"chunk_id": "chunk-1", "citation_id": "citation-1", "source": "manual.md", "content": "请假制度"}],
    )

    assert result == repaired_proposal
    assert len(client.requests) == 2
    repair_body = json.loads(client.requests[1]["messages"][1]["content"])
    assert repair_body["proposal"] == initial
    assert "chunks" not in repair_body


def test_propose_reports_error_when_single_profile_repair_is_still_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次格式修复仍失败时，拒绝继续重试或静默篡改内容。"""

    client = _Client(
        [
            _Response(_proposal(_profile(intent_count=22, examples=[{"query": "请假怎么办"}]))),
            _Response(_proposal(_profile(intent_count=21, examples=[{"query": "仍是对象"}]), case_id=1)),
        ]
    )
    monkeypatch.setattr(proposals_module.httpx, "Client", lambda **_: client)

    with pytest.raises(ValueError, match="一次格式修复后仍无效"):
        MuyeLLMProposalClient(base_url="http://muye-llm.test").propose(
            project=_project(),
            chunks=[{"chunk_id": "chunk-1", "citation_id": "citation-1", "source": "manual.md", "content": "请假制度"}],
        )

    assert len(client.requests) == 2


def test_propose_repairs_case_that_spans_multiple_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨条款评测问题必须拆分，避免完整 citation coverage 门禁偶发失败。"""

    profile = _profile(intent_count=2, examples=["产假怎么休？"])
    initial = _proposal(profile)
    initial["cases"][0]["relevant_chunk_ids"] = ["chunk-1", "chunk-2"]  # type: ignore[index]
    repaired = _proposal(profile)
    client = _Client([_Response(initial), _Response(repaired)])
    monkeypatch.setattr(proposals_module.httpx, "Client", lambda **_: client)

    result = MuyeLLMProposalClient(base_url="http://muye-llm.test").propose(
        project=_project(),
        chunks=[{"chunk_id": "chunk-1", "citation_id": "citation-1", "source": "manual.md", "content": "产假制度"}],
    )

    assert result == repaired
    assert len(client.requests) == 2
    repair_body = json.loads(client.requests[1]["messages"][1]["content"])
    assert "每个评测用例必须且只能关联一个 relevant_chunk_id" in repair_body["validation_errors"][0]["reason"]


def test_propose_reports_muye_llm_business_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """网关以 HTTP 200 返回失败 payload 时，保留其可行动的错误消息。"""

    client = _Client([_FailedResponse({})])
    monkeypatch.setattr(proposals_module.httpx, "Client", lambda **_: client)

    with pytest.raises(RuntimeError, match="未能生成 Agent 创建提案：LLM 返回空内容"):
        MuyeLLMProposalClient(base_url="http://muye-llm.test").propose(
            project=_project(),
            chunks=[{"chunk_id": "chunk-1", "citation_id": "citation-1", "source": "manual.md", "content": "请假制度"}],
        )
