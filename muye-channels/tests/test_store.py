"""微信通道持久化与隐私边界回归测试。"""
from __future__ import annotations

import asyncio
import base64
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import ChannelStore, CryptoBox


def test_store_encrypts_credentials_and_deduplicates_messages(tmp_path) -> None:
    async def run() -> None:
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        path = tmp_path / "channels.db"
        store = ChannelStore(path, CryptoBox(key))
        binding = await store.activate("usr_1", "private-token", "https://ilinkai.weixin.qq.com/ilink/bot")
        stored = await store.binding("usr_1")
        assert stored is not None and stored.bot_token == "private-token"
        accepted = await store.record_messages(binding, "cursor-1", [("message-1", "sender", "context-token", "你好")])
        duplicate = await store.record_messages(binding, "cursor-2", [("message-1", "sender", "context-token", "你好")])
        assert len(accepted) == 1
        assert duplicate == []
        assert await store.claim(accepted[0][0]) is True
        assert await store.claim(accepted[0][0]) is False
        assert b"private-token" not in path.read_bytes()
        assert b"context-token" not in path.read_bytes()

    asyncio.run(run())
