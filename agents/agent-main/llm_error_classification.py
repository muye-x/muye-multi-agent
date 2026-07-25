"""LLM 异常链的状态提取与重试分类。"""

from __future__ import annotations

from collections.abc import Iterator


def iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """按因果关系遍历异常链，并防止异常对象循环引用。"""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def http_status_code(exception_chain: tuple[BaseException, ...]) -> int | None:
    """从异常链携带的 HTTP 响应中提取状态码。"""
    for current in exception_chain:
        response = getattr(current, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code
    return None


def classify_llm_error(exc: BaseException) -> str:
    """将 LLM 异常分类为可重试、不可重试或未知。"""
    exception_chain = tuple(iter_exception_chain(exc))
    status_code = http_status_code(exception_chain)
    if status_code is not None:
        if status_code in {408, 409, 425, 429} or status_code >= 500:
            return "retry"
        if 400 <= status_code < 500:
            return "no_retry"

    error_text = " ".join(
        f"{type(current).__name__} {current}".lower()
        for current in exception_chain
    )
    no_retry_keywords = (
        "quota",
        "insufficient_quota",
        "authentication",
        "invalid_api_key",
        "unauthorized",
        "permission",
        "forbidden",
        "invalid_request",
    )
    if any(keyword in error_text for keyword in no_retry_keywords):
        return "no_retry"

    retry_keywords = (
        "rate_limit",
        "too_many_requests",
        "timeout",
        "timed out",
        "busy",
        "overloaded",
        "connection",
        "network",
        "unavailable",
        "service_unavailable",
        "bad_gateway",
        "gateway_timeout",
    )
    if any(keyword in error_text for keyword in retry_keywords):
        return "retry"
    return "unknown"


def extract_retry_after(exc: BaseException) -> float | None:
    """从异常链的 HTTP 响应头读取重试等待秒数。"""
    for current in iter_exception_chain(exc):
        response = getattr(current, "response", None)
        if response is None:
            continue
        headers = getattr(response, "headers", {})
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                return None
    return None
