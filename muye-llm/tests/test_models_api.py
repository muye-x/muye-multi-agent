"""多模型 API 的向下兼容与白名单响应测试。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from config.settings import LLMModelSettings
from pydantic import ValidationError

from src.api.chat import ChatMessage, ChatRequest, chat_stream, list_models
from src.core.llm_client import LLMCallResult, LLMNodeConfig, LLMStreamEvent, MultiLLMClient
from src.core.model_registry import ModelRegistry
from src.utils.exceptions import InvalidRequestException


class _CloseableFake:
    async def close(self) -> None:
        return None


class _StreamingClient:
    def resolve_model(self, *_args: object) -> None:
        return None

    async def chat_stream_result(self, *_args: object, **_kwargs: object):
        yield LLMStreamEvent(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ]
        )
        yield LLMStreamEvent(
            result=LLMCallResult(
                content="",
                model="test",
                provider="test",
                base_url="http://llm.test",
                finish_reason="tool_calls",
            )
        )


def _client() -> MultiLLMClient:
    registry = ModelRegistry(
        [
            LLMModelSettings(
                id="fast",
                name="Fast Model",
                provider_model="provider-fast",
                supports_thinking=False,
            ),
            LLMModelSettings(
                id="reasoning",
                name="Reasoning Model",
                provider_model="provider-reasoning",
                supports_thinking=True,
            ),
        ],
        default_model="fast",
        default_thinking=False,
    )
    return MultiLLMClient(
        api_config=LLMNodeConfig(
            base_url="http://llm.test/v1",
            api_key="test",
            extra_body={},
        ),
        client=_CloseableFake(),
        embedding_client=_CloseableFake(),
        model_registry=registry,
    )


def _request(client: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(llm_client=client)))


class ModelsApiTestCase(unittest.TestCase):
    def test_legacy_chat_request_keeps_new_fields_optional(self) -> None:
        request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])

        self.assertIsNone(request.model)
        self.assertIsNone(request.enable_thinking)

    def test_removed_usage_context_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ChatRequest.model_validate(
                {"messages": [{"role": "user", "content": "hello"}], "usage_context": {}}
            )

    def test_tool_call_messages_are_accepted_by_strict_schema(self) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "查询天气"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "weather", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "晴"},
                ]
            }
        )

        payload = [message.model_dump(exclude_none=True) for message in request.messages]
        self.assertEqual(payload[1]["tool_calls"][0]["function"]["name"], "weather")

    def test_models_endpoint_returns_only_public_capabilities(self) -> None:
        response = asyncio.run(list_models(_request(_client())))
        body = response.model_dump()

        self.assertEqual(body["data"]["default_model"], "fast")
        self.assertFalse(body["data"]["default_thinking"])
        self.assertEqual(
            body["data"]["models"],
            [
                {
                    "id": "fast",
                    "name": "Fast Model",
                    "supports_thinking": False,
                    "is_default": True,
                },
                {
                    "id": "reasoning",
                    "name": "Reasoning Model",
                    "supports_thinking": True,
                    "is_default": False,
                },
            ],
        )
        self.assertNotIn("provider_model", str(body))

    def test_stream_rejects_unknown_model_before_response_starts(self) -> None:
        async def run() -> None:
            with self.assertRaises(InvalidRequestException):
                await chat_stream(
                    ChatRequest(
                        messages=[ChatMessage(role="user", content="hello")],
                        model="unknown",
                    ),
                    _request(_client()),
                )

        asyncio.run(run())

    def test_stream_encodes_tool_calls_as_a_dedicated_sse_event(self) -> None:
        async def collect() -> bytes:
            response = await chat_stream(
                ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
                _request(_StreamingClient()),
            )
            return b"".join([chunk async for chunk in response.body_iterator])

        body = asyncio.run(collect())

        self.assertIn(b"event: tool_calls", body)
        self.assertIn(b'"name": "lookup"', body)


if __name__ == "__main__":
    unittest.main()
