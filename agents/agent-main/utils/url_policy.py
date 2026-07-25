"""网页抓取的统一 URL 信任边界。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """用户提供的 URL 不满足外部网页抓取安全策略。"""


def validate_external_url(url: str) -> str:
    """验证 URL 为可解析的公网 HTTP(S) 地址。

    在将 URL 交给任何第三方抓取服务前执行，拒绝凭据、环回、私网、保留地址和
    DNS 无法解析的主机，避免抓取器成为内部网络访问入口。
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL 不能为空")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("仅允许 http 或 https URL")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrlError("URL 必须包含无凭据的主机名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL 端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeUrlError("URL 端口无效")

    try:
        addresses = socket.getaddrinfo(parsed.hostname, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError("URL 主机无法解析") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise UnsafeUrlError("不允许访问内网或保留地址")
    return parsed.geturl()
