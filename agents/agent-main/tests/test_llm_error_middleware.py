"""LLM 错误分类与重试边界的回归测试。"""

from __future__ import annotations

import httpx

from llm_error_classification import classify_llm_error, extract_retry_after
from muye_multi_agent_sdk.integrations.muye_llm import MuyeLlmError


def _wrapped_http_error(status_code: int, *, retry_after: str | None = None) -> MuyeLlmError:
    """构造与 SDK 实际抛出结构一致的异常链。"""
    request = httpx.Request("POST", "http://llm.test/api/v2/chat/stream")
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    response = httpx.Response(status_code, request=request, headers=headers)
    http_error = httpx.HTTPStatusError(
        "模型网关返回错误",
        request=request,
        response=response,
    )
    wrapped = MuyeLlmError("muye-llm流式调用失败")
    wrapped.__cause__ = http_error
    return wrapped


def test_http_400_is_not_retried() -> None:
    assert classify_llm_error(_wrapped_http_error(400)) == "no_retry"


def test_http_429_is_retried_and_preserves_retry_after() -> None:
    error = _wrapped_http_error(429, retry_after="2.5")

    assert classify_llm_error(error) == "retry"
    assert extract_retry_after(error) == 2.5


def test_http_500_is_retried() -> None:
    assert classify_llm_error(_wrapped_http_error(500)) == "retry"
