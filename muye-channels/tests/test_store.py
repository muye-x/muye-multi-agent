"""微信通道持久化与隐私边界回归测试。"""
from __future__ import annotations

import base64
import secrets
import sys
from xml.etree import ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import ChannelConfig, CryptoBox, _render_qr_svg


def test_crypto_box_encrypts_provider_secrets_and_stable_ids() -> None:
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    crypto = CryptoBox(key)
    encrypted = crypto.encrypt("private-token")
    assert encrypted != b"private-token"
    assert crypto.decrypt(encrypted) == "private-token"
    assert crypto.stable_id("binding", "provider-message") == crypto.stable_id("binding", "provider-message")


def test_render_qr_svg_returns_data_uri() -> None:
    rendered = _render_qr_svg("https://example.test/wechat/qr")

    assert rendered.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(rendered.partition(",")[2])
    root = ElementTree.fromstring(svg)

    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.findall("{http://www.w3.org/2000/svg}path")


def test_channel_agent_timeout_is_bounded() -> None:
    """渠道调用 MainAgent 应允许联网工具完成，但不能无限等待。"""
    config = ChannelConfig(
        caller_token="caller-token-12345",
        main_url="http://agent-main:9860",
        main_token="main-token-123456",
        encryption_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        database_url="postgresql://user:password@postgres:5432/muye",
        agent_timeout_seconds=240,
        ilink_base_url="https://ilinkai.weixin.qq.com/ilink/bot",
        allowed_hosts={"ilinkai.weixin.qq.com"},
    )

    assert config.agent_timeout_seconds == 240
