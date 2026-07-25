import asyncio
from types import SimpleNamespace
import unittest

from client.llm_client import (
    LLMClient,
    _build_claude_code_command,
    _build_anthropic_payload,
    _extract_claude_code_text,
    _extract_claude_code_usage,
    _messages_to_claude_code_prompt,
)
from client.response import StreamEventType
from config.config import Config


def function_delta(index, name=None, arguments=None, call_id=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.received_kwargs = None

    async def create(self, **kwargs):
        self.received_kwargs = kwargs

        async def iterator():
            for item in self._chunks:
                yield item

        return iterator()


class FakeClient:
    def __init__(self, chunks):
        self.completions = FakeCompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)


def collect(chunks):
    client = FakeClient(chunks)
    llm = LLMClient(Config())

    async def run():
        return [event async for event in llm._stream_response(client, {})]

    return asyncio.run(run())


def completed_tool_calls(events):
    return [e.tool_call for e in events if e.type == StreamEventType.TOOL_CALL_COMPLETE]


class StreamingToolCallTests(unittest.TestCase):
    def test_arguments_split_across_chunks_are_accumulated(self):
        """Regression: providers stream the name first, then arguments in fragments."""
        events = collect(
            [
                chunk(tool_calls=[function_delta(0, name="read_file", call_id="call_1")]),
                chunk(tool_calls=[function_delta(0, arguments='{"pa')]),
                chunk(tool_calls=[function_delta(0, arguments='th": "ma')]),
                chunk(tool_calls=[function_delta(0, arguments='in.py"}')]),
                chunk(finish_reason="tool_calls"),
            ]
        )

        calls = completed_tool_calls(events)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].call_id, "call_1")
        self.assertEqual(calls[0].arguments, {"path": "main.py"})

    def test_single_chunk_tool_call(self):
        events = collect(
            [
                chunk(
                    tool_calls=[
                        function_delta(
                            0,
                            name="read_file",
                            arguments='{"path": "main.py"}',
                            call_id="call_1",
                        )
                    ]
                ),
            ]
        )

        calls = completed_tool_calls(events)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].arguments, {"path": "main.py"})

    def test_parallel_tool_calls_accumulate_independently(self):
        events = collect(
            [
                chunk(
                    tool_calls=[
                        function_delta(0, name="read_file", call_id="call_1"),
                        function_delta(1, name="grep", call_id="call_2"),
                    ]
                ),
                chunk(
                    tool_calls=[
                        function_delta(0, arguments='{"path":'),
                        function_delta(1, arguments='{"pattern":'),
                    ]
                ),
                chunk(
                    tool_calls=[
                        function_delta(0, arguments=' "a.py"}'),
                        function_delta(1, arguments=' "TODO"}'),
                    ]
                ),
            ]
        )

        calls = completed_tool_calls(events)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].arguments, {"path": "a.py"})
        self.assertEqual(calls[1].arguments, {"pattern": "TODO"})
        self.assertEqual({c.call_id for c in calls}, {"call_1", "call_2"})

    def test_call_id_arriving_in_later_chunk_is_captured(self):
        events = collect(
            [
                chunk(tool_calls=[function_delta(0, name="read_file")]),
                chunk(
                    tool_calls=[
                        function_delta(0, arguments='{"path": "a.py"}', call_id="late_id")
                    ]
                ),
            ]
        )

        calls = completed_tool_calls(events)
        self.assertEqual(calls[0].call_id, "late_id")

    def test_delta_without_function_payload_is_ignored(self):
        events = collect(
            [
                chunk(tool_calls=[function_delta(0, name="read_file", call_id="call_1")]),
                chunk(tool_calls=[SimpleNamespace(index=0, id=None, function=None)]),
                chunk(tool_calls=[function_delta(0, arguments='{"path": "a.py"}')]),
            ]
        )

        calls = completed_tool_calls(events)
        self.assertEqual(calls[0].arguments, {"path": "a.py"})

    def test_text_deltas_are_streamed(self):
        events = collect([chunk(content="Hello "), chunk(content="world")])

        text = "".join(
            e.text_delta.content
            for e in events
            if e.type == StreamEventType.TEXT_DELTA
        )
        self.assertEqual(text, "Hello world")

    def test_usage_and_finish_reason_reach_message_complete(self):
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        )
        events = collect(
            [chunk(content="hi", finish_reason="stop"), chunk(usage=usage)]
        )

        final = events[-1]
        self.assertEqual(final.type, StreamEventType.MESSAGE_COMPLETE)
        self.assertEqual(final.finish_reason, "stop")
        self.assertEqual(final.usage.total_tokens, 15)
        self.assertEqual(final.usage.cached_tokens, 4)

    def test_chunks_without_choices_do_not_crash(self):
        events = collect([SimpleNamespace(choices=[], usage=None), chunk(content="ok")])

        self.assertEqual(events[-1].type, StreamEventType.MESSAGE_COMPLETE)


class RequestConstructionTests(unittest.TestCase):
    def test_stream_options_and_temperature_are_sent(self):
        config = Config()
        config.temperature = 0.3
        llm = LLMClient(config)
        client = FakeClient([chunk(content="ok")])

        async def run():
            kwargs = {
                "model": config.model_name,
                "messages": [],
                "stream": True,
                "temperature": config.temperature,
                "stream_options": {"include_usage": True},
            }
            return [event async for event in llm._stream_response(client, kwargs)]

        asyncio.run(run())

        sent = client.completions.received_kwargs
        self.assertEqual(sent["temperature"], 0.3)
        self.assertEqual(sent["stream_options"], {"include_usage": True})


class ClaudeCodeProviderTests(unittest.TestCase):
    def test_claude_code_command_uses_print_stream_json(self):
        config = Config()
        config.model.provider = "claude_code"
        config.model.name = "sonnet"
        config.model.claude_code_permission_mode = "plan"
        config.model.claude_code_max_turns = 3

        command = _build_claude_code_command(config)

        self.assertEqual(command[:4], ["claude", "-p", "--output-format", "stream-json"])
        self.assertIn("--model", command)
        self.assertIn("sonnet", command)
        self.assertIn("--permission-mode", command)
        self.assertIn("plan", command)
        self.assertEqual(command[-2:], ["--max-turns", "3"])

    def test_claude_code_prompt_contains_conversation_roles(self):
        prompt = _messages_to_claude_code_prompt(
            [
                {"role": "system", "content": "Be careful."},
                {"role": "user", "content": "Implement feature."},
                {"role": "assistant", "content": "I will."},
            ]
        )

        self.assertIn("## System\n\nBe careful.", prompt)
        self.assertIn("## User\n\nImplement feature.", prompt)
        self.assertIn("## Assistant\n\nI will.", prompt)

    def test_claude_code_stream_json_text_and_usage_are_parsed(self):
        text = _extract_claude_code_text(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            }
        )
        usage = _extract_claude_code_usage(
            {
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 2,
                    }
                }
            }
        )

        self.assertEqual(text, "hello")
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 14)
        self.assertEqual(usage.cached_tokens, 2)


class AnthropicProviderTests(unittest.TestCase):
    def test_anthropic_payload_converts_system_tools_and_tool_results(self):
        config = Config()
        config.model.provider = "anthropic"
        config.model.name = "claude-sonnet-4-20250514"

        payload = _build_anthropic_payload(
            config,
            [
                {"role": "system", "content": "Be careful."},
                {"role": "user", "content": "Read main.py"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "main.py"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_1",
                    "content": "file contents",
                },
            ],
            tools=[
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
            stream=True,
        )

        self.assertEqual(payload["system"], "Be careful.")
        self.assertEqual(payload["tools"][0]["input_schema"]["properties"]["path"]["type"], "string")
        self.assertEqual(payload["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(payload["messages"][2]["content"][0]["type"], "tool_result")
        self.assertEqual(payload["messages"][2]["content"][0]["tool_use_id"], "toolu_1")

    def test_streaming_anthropic_tool_use_events_are_accumulated(self):
        llm = LLMClient(Config())

        async def lines():
            for payload in [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": ' "main.py"}'}},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
                {"type": "message_stop"},
            ]:
                yield "data: " + __import__("json").dumps(payload)

        class FakeStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def raise_for_status(self):
                return None

            def aiter_lines(self):
                return lines()

        class FakeHttpClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, *args, **kwargs):
                return FakeStream()

        original = __import__("client.llm_client", fromlist=["httpx"]).httpx.AsyncClient
        try:
            __import__("client.llm_client", fromlist=["httpx"]).httpx.AsyncClient = lambda *args, **kwargs: FakeHttpClient()
            events = asyncio.run(
                _collect_async(llm._stream_anthropic_payload({"messages": [], "model": "x"}))
            )
        finally:
            __import__("client.llm_client", fromlist=["httpx"]).httpx.AsyncClient = original

        calls = completed_tool_calls(events)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "main.py"})


async def _collect_async(iterator):
    return [event async for event in iterator]


if __name__ == "__main__":
    unittest.main()
