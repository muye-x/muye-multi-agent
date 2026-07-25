"""MultiLLMClient 的回归测试，隔离真实模型与网络。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from config.settings import LLMModelSettings
from src.core.llm_client import LLMNodeConfig, MultiLLMClient, ThinkTagFilter, ToolCallAccumulator
from src.core.model_registry import ModelRegistry, ModelSelectionError


class _FakeCompletions:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.response.pop(0) if isinstance(self.response, list) else self.response
        if isinstance(response, Exception):
            raise response
        return response


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.completions = _FakeCompletions(response)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self.chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.closed = True


class _FakeTracer:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, metadata: dict[str, Any]) -> None:
        self.records.append(metadata)


def _config(*, extra_body: dict[str, Any] | None = None) -> LLMNodeConfig:
    return LLMNodeConfig(
        base_url="http://llm.test/v1",
        api_key="test",
        extra_body=extra_body or {},
    )


def _text_chunk(content: str, *, finish_reason: str | None = None) -> Any:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                delta=SimpleNamespace(content=content, tool_calls=None),
            )
        ],
    )


def _tool_chunk(*, finish_reason: str | None = None) -> Any:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                delta=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call_1",
                            function=SimpleNamespace(name="lookup", arguments='{"id":1}'),
                        )
                    ],
                ),
            )
        ],
    )


class ThinkTagFilterTestCase(unittest.TestCase):
    def test_filter_handles_tags_split_across_chunks(self) -> None:
        parser = ThinkTagFilter()
        visible = "".join(
            parser.feed(part)
            for part in ("before <thi", "nk>private", " reasoning</thi", "nk> after")
        ) + parser.finalize()

        self.assertEqual(visible, "before  after")

    def test_tool_accumulator_allows_missing_function(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add(SimpleNamespace(index=0, id="call_", function=None))
        accumulator.add(
            SimpleNamespace(
                index=0,
                id="1",
                function=SimpleNamespace(name="lookup", arguments='{"id":'),
            )
        )
        accumulator.add(
            SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments="42}"))
        )

        self.assertEqual(
            accumulator.to_list(),
            [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"id":42}'}}],
        )


class MultiLLMClientTestCase(unittest.TestCase):
    def test_request_passes_thinking_setting_through_extra_body(self) -> None:
        completion = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="OK", tool_calls=None))],
            usage=None,
        )
        fake_client = _FakeClient(completion)
        client = MultiLLMClient(
            api_config=_config(extra_body={"service_tier": "default"}),
            client=fake_client,
            embedding_client=_FakeClient(None),
        )

        result = asyncio.run(
            client.chat_result(
                [{"role": "user", "content": "hi"}],
                model="qwen-flash",
                enable_thinking=True,
            )
        )

        self.assertEqual(result.content, "OK")
        self.assertEqual(fake_client.completions.calls[0]["model"], "qwen-flash")
        self.assertEqual(
            fake_client.completions.calls[0]["extra_body"],
            {"service_tier": "default", "enable_thinking": True},
        )
        self.assertEqual(result.model_alias, "qwen-flash")
        self.assertEqual(result.model_name, "Qwen Flash")
        self.assertTrue(result.thinking_enabled)

    def test_stream_filters_split_thinking_tags_and_closes_stream(self) -> None:
        stream = _FakeStream(
            [
                _text_chunk("<thi"),
                _text_chunk("nk>hidden"),
                _text_chunk("</thi"),
                _text_chunk("nk>visible", finish_reason="stop"),
            ]
        )
        fake_client = _FakeClient(stream)
        client = MultiLLMClient(
            api_config=_config(),
            client=fake_client,
            embedding_client=_FakeClient(None),
        )

        async def collect() -> list[Any]:
            return [event async for event in client.chat_stream_result([{"role": "user", "content": "hi"}])]

        events = asyncio.run(collect())

        self.assertEqual([event.content for event in events if event.content], ["visible"])
        self.assertEqual(events[-1].result.content, "visible")
        self.assertTrue(stream.closed)

    def test_stream_emits_tool_calls_as_a_structured_event(self) -> None:
        stream = _FakeStream([_tool_chunk(finish_reason="tool_calls")])
        client = MultiLLMClient(
            api_config=_config(),
            client=_FakeClient(stream),
            embedding_client=_FakeClient(None),
        )

        async def collect() -> list[Any]:
            return [event async for event in client.chat_stream_result([{"role": "user", "content": "hi"}])]

        events = asyncio.run(collect())

        self.assertEqual(events[0].tool_calls[0]["function"]["name"], "lookup")
        self.assertEqual(events[-1].result.content, "")

    def test_retry_reuses_the_only_chat_client(self) -> None:
        completion = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="OK", tool_calls=None))],
            usage=None,
        )
        fake_client = _FakeClient([asyncio.TimeoutError(), completion])
        client = MultiLLMClient(
            api_config=_config(),
            client=fake_client,
            embedding_client=_FakeClient(None),
        )

        async def call() -> Any:
            client._sleep_before_retry = lambda attempt: asyncio.sleep(0)
            return await client.chat_result([{"role": "user", "content": "hi"}])

        result = asyncio.run(call())

        self.assertEqual(result.content, "OK")
        self.assertEqual(len(fake_client.completions.calls), 2)

    def test_trace_records_metadata_without_messages(self) -> None:
        completion = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="OK", tool_calls=None))],
        )
        tracer = _FakeTracer()
        client = MultiLLMClient(
            api_config=_config(),
            client=_FakeClient(completion),
            embedding_client=_FakeClient(None),
            tracer=tracer,
        )

        result = asyncio.run(
            client.chat_result(
                [{"role": "user", "content": "敏感任务正文"}],
                trace_id="trace-1",
                tools=[{"type": "function"}],
            )
        )

        self.assertEqual(result.content, "OK")
        self.assertEqual(
            tracer.records,
            [{
                "trace_id": "trace-1",
                "model_alias": "deepseek-v4-flash",
                "upstream_model": "deepseek-v4-flash",
                "thinking": False,
                "latency_ms": result.latency_ms,
                "tool_count": 1,
                "status": "success",
            }],
        )

class ModelRegistryTestCase(unittest.TestCase):
    def test_model_settings_normalize_text_fields(self) -> None:
        model = LLMModelSettings(
            id=" fast ",
            name=" Fast Model ",
            provider_model=" provider-fast ",
            supports_thinking=False,
        )

        self.assertEqual(model.id, "fast")
        self.assertEqual(model.name, "Fast Model")
        self.assertEqual(model.provider_model, "provider-fast")

    def test_default_and_request_overrides_are_resolved(self) -> None:
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

        default_selection = registry.resolve(None, None)
        override_selection = registry.resolve("reasoning", True)

        self.assertEqual(default_selection.id, "fast")
        self.assertFalse(default_selection.thinking_enabled)
        self.assertEqual(override_selection.provider_model, "provider-reasoning")
        self.assertTrue(override_selection.thinking_enabled)

    def test_unknown_model_and_unsupported_thinking_are_rejected(self) -> None:
        registry = ModelRegistry(
            [
                LLMModelSettings(
                    id="fast",
                    name="Fast Model",
                    provider_model="provider-fast",
                    supports_thinking=False,
                )
            ],
            default_model="fast",
            default_thinking=False,
        )

        with self.assertRaises(ModelSelectionError):
            registry.resolve("unknown", False)
        with self.assertRaises(ModelSelectionError):
            registry.resolve("fast", True)


if __name__ == "__main__":
    unittest.main()
