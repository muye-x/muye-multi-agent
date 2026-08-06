"""从经 Gateway 认证的 Header 构造 Main 请求可信用户身份。"""

from __future__ import annotations

import re
from secrets import compare_digest

from fastapi import HTTPException, Request

from config import get_config


_USER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")


def trusted_user_id(request: Request) -> str | None:
    """Control 未启用时返回 None；启用时拒绝缺失或伪造的 CallerContext。"""
    config = get_config().catalog
    if not config.enabled:
        return None
    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    token = authorization[len(prefix) :].strip() if authorization.startswith(prefix) else ""
    user_id = request.headers.get("x-muye-user-id", "").strip()
    if (
        not token
        or not compare_digest(token, config.trusted_caller_token)
        or _USER_ID_PATTERN.fullmatch(user_id) is None
    ):
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTHENTICATION_ERROR", "message": "可信 CallerContext 无效"},
        )
    if config.local_dev_enabled and user_id != config.local_dev_user_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTHENTICATION_ERROR", "message": "local-dev 用户身份无效"},
        )
    return user_id
