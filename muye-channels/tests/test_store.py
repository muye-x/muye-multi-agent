"""微信通道持久化与隐私边界回归测试。"""
from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import CryptoBox, _render_qr_svg


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
    assert len(rendered) > len("data:image/svg+xml;base64,")
