import asyncio
import json
import shlex
from typing import Any, AsyncGenerator
import uuid
import httpx
from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from control_plane.client import check_model_usage
from client.response import (
    StreamEventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolCallDelta,
    parse_tool_call_arguments,
)
from config.config import Config
from safety.redaction import SensitiveDataRedactor


class LLMClient:
    def __init__(self, config: Config) -> None:
        self._client: AsyncOpenAI | None = None
        self._max_retries: int = 3
        self.config = config
        self.redactor = SensitiveDataRedactor(config)
        self.session_id = str(uuid.uuid4())
        self.turn_number = 0

    def get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key or "local",
                base_url=self.config.base_url,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def _build_tools(self, tools: list[dict[str, Any]]):
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "parameters",
                        {
                            "type": "object",
                            "properties": {},
                        },
                    ),
                },
            }
            for tool in tools
        ]

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        outbound_messages = messages

        if self.config.sensitive_data.redact_before_model:
            redaction_result = self.redactor.redact_value(messages)
            outbound_messages = redaction_result.value

        self.turn_number += 1
        try:
            await check_model_usage(
                self.config,
                model=self.config.model_name,
                session_id=self.session_id,
                turn_number=self.turn_number,
                context_tokens=_estimate_context_tokens(outbound_messages),
            )
        except RuntimeError as error:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error=str(error),
            )
            return

        if self.config.model.provider == "claude_code":
            async for event in self._claude_code_response(outbound_messages):
                yield event
            return

        if self.config.model.provider == "anthropic":
            async for event in self._anthropic_response(
                outbound_messages,
                tools=tools,
                stream=stream,
            ):
                yield event
            return

        client = self.get_client()
        kwargs = {
            "model": self.config.model_name,
            "messages": outbound_messages,
            "stream": stream,
            "temperature": self.config.temperature,
        }

        if stream and self.config.model.supports_stream_options:
            kwargs["stream_options"] = {"include_usage": True}

        if tools:
            kwargs["tools"] = self._build_tools(tools)
            kwargs["tool_choice"] = "auto"

        for attempt in range(self._max_retries + 1):
            try:
                if stream:
                    async for event in self._stream_response(client, kwargs):
                        yield event
                else:
                    async for event in self._non_stream_response(client, kwargs):
                        yield event
                return
            except RateLimitError as e:
                if attempt < self._max_retries:
                    wait_time = 2**attempt
                    await asyncio.sleep(wait_time)
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"Rate limit exceeded: {e}",
                    )
                    return
            except APIConnectionError as e:
                if attempt < self._max_retries:
                    wait_time = 2**attempt
                    await asyncio.sleep(wait_time)
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"Connection error: {e}",
                    )
                    return
            except APIError as e:
                status = getattr(e, "status_code", None)
                retryable = status is None or status >= 500
                if retryable and attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue

                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    error=f"API error: {e}",
                )
                return

    async def _stream_response(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        response = await client.chat.completions.create(**kwargs)

        finish_reason: str | None = None
        usage: TokenUsage | None = None
        tool_calls: dict[int, dict[str, Any]] = {}

        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
                usage = TokenUsage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                    cached_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
                )

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                finish_reason = choice.finish_reason

            if delta.content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(delta.content),
                )

            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    idx = tool_call_delta.index

                    if idx not in tool_calls:
                        tool_calls[idx] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }

                    if tool_call_delta.id:
                        tool_calls[idx]["id"] = tool_call_delta.id

                    function = getattr(tool_call_delta, "function", None)
                    if not function:
                        continue

                    if function.name:
                        tool_calls[idx]["name"] = function.name
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_START,
                            tool_call_delta=ToolCallDelta(
                                call_id=tool_calls[idx]["id"],
                                name=function.name,
                            ),
                        )

                    if function.arguments:
                        tool_calls[idx]["arguments"] += function.arguments

                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call_delta=ToolCallDelta(
                                call_id=tool_calls[idx]["id"],
                                name=tool_calls[idx]["name"],
                                arguments_delta=function.arguments,
                            ),
                        )

        for idx, tc in tool_calls.items():
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tc["id"],
                    name=tc["name"],
                    arguments=parse_tool_call_arguments(tc["arguments"]),
                ),
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def _non_stream_response(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        text_delta = None
        if message.content:
            text_delta = TextDelta(content=message.content)
            yield StreamEvent(
                type=StreamEventType.TEXT_DELTA,
                text_delta=text_delta,
            )

        if message.tool_calls:
            for tc in message.tool_calls:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_COMPLETE,
                    tool_call=ToolCall(
                        call_id=tc.id,
                        name=tc.function.name,
                        arguments=parse_tool_call_arguments(tc.function.arguments),
                    ),
                )

        usage = None
        if response.usage:
            prompt_details = getattr(response.usage, "prompt_tokens_details", None)
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cached_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=choice.finish_reason,
            usage=usage,
        )

    async def _anthropic_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> AsyncGenerator[StreamEvent, None]:
        if not self.config.api_key:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error=f"No Anthropic API key found. Set {self.config.model.api_key_env}.",
            )
            return

        payload = _build_anthropic_payload(self.config, messages, tools, stream=stream)

        for attempt in range(self._max_retries + 1):
            try:
                if stream:
                    async for event in self._stream_anthropic_payload(payload):
                        yield event
                else:
                    async for event in self._non_stream_anthropic_payload(payload):
                        yield event
                return
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if status in {429} or status >= 500:
                    if attempt < self._max_retries:
                        await asyncio.sleep(2**attempt)
                        continue
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    error=f"Anthropic API error: {_safe_httpx_error(error.response)}",
                )
                return
            except httpx.HTTPError as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    error=f"Anthropic connection error: {error}",
                )
                return

    async def _stream_anthropic_payload(
        self,
        payload: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        payload = {**payload, "stream": True}
        tool_blocks: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        finish_reason: str | None = None

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                _anthropic_messages_url(self.config),
                headers=_anthropic_headers(self.config),
                json=payload,
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue

                    data = line.removeprefix("data:").strip()
                    if not data or data == "[DONE]":
                        continue

                    event = json.loads(data)
                    event_type = event.get("type")

                    if event_type == "message_start":
                        event_usage = event.get("message", {}).get("usage", {})
                        usage.prompt_tokens = int(event_usage.get("input_tokens") or 0)
                        usage.cached_tokens = int(event_usage.get("cache_creation_input_tokens") or 0) + int(
                            event_usage.get("cache_read_input_tokens") or 0
                        )
                    elif event_type == "content_block_start":
                        index = int(event.get("index", 0))
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            tool_blocks[index] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": "",
                            }
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_delta=ToolCallDelta(
                                    call_id=tool_blocks[index]["id"],
                                    name=tool_blocks[index]["name"],
                                ),
                            )
                    elif event_type == "content_block_delta":
                        index = int(event.get("index", 0))
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield StreamEvent(
                                type=StreamEventType.TEXT_DELTA,
                                text_delta=TextDelta(delta["text"]),
                            )
                        elif delta.get("type") == "input_json_delta":
                            partial = delta.get("partial_json") or ""
                            if index in tool_blocks:
                                tool_blocks[index]["arguments"] += partial
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_DELTA,
                                    tool_call_delta=ToolCallDelta(
                                        call_id=tool_blocks[index]["id"],
                                        name=tool_blocks[index]["name"],
                                        arguments_delta=partial,
                                    ),
                                )
                    elif event_type == "message_delta":
                        delta = event.get("delta") or {}
                        finish_reason = delta.get("stop_reason") or finish_reason
                        event_usage = event.get("usage") or {}
                        usage.completion_tokens = int(event_usage.get("output_tokens") or usage.completion_tokens)
                    elif event_type == "message_stop":
                        break

        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        for block in tool_blocks.values():
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=block["id"],
                    name=block["name"],
                    arguments=parse_tool_call_arguments(block["arguments"]),
                ),
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def _non_stream_anthropic_payload(
        self,
        payload: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        payload = {**payload, "stream": False}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                _anthropic_messages_url(self.config),
                headers=_anthropic_headers(self.config),
                json=payload,
            )
            response.raise_for_status()

        data = response.json()
        text_delta = None
        for block in data.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                text_delta = TextDelta(block["text"])
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=text_delta,
                )
            elif block.get("type") == "tool_use":
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_COMPLETE,
                    tool_call=ToolCall(
                        call_id=block.get("id", ""),
                        name=block.get("name"),
                        arguments=block.get("input") or {},
                    ),
                )

        usage = _anthropic_usage(data.get("usage") or {})
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=data.get("stop_reason"),
            usage=usage,
        )

    async def _claude_code_response(
        self,
        messages: list[dict[str, Any]],
    ) -> AsyncGenerator[StreamEvent, None]:
        prompt = _messages_to_claude_code_prompt(messages)
        command = _build_claude_code_command(self.config)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                prompt,
                cwd=str(self.config.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error=(
                    "Claude Code command was not found. Install Claude Code, run "
                    "`claude` once to authenticate your subscription, or set "
                    "model.claude_code_command."
                ),
            )
            return
        except OSError as error:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error=f"Failed to start Claude Code: {error}",
            )
            return

        usage: TokenUsage | None = None
        emitted_text = False
        buffered_result = ""

        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                emitted_text = True
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(line + "\n"),
                )
                continue

            text = _extract_claude_code_text(payload)
            if text:
                emitted_text = True
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(text),
                )

            event_usage = _extract_claude_code_usage(payload)
            if event_usage:
                usage = event_usage

            if payload.get("type") == "result":
                if payload.get("is_error"):
                    buffered_result = str(payload.get("result") or payload.get("error") or "")
                elif not emitted_text and payload.get("result"):
                    buffered_result = str(payload["result"])

        stderr = ""
        if process.stderr is not None:
            stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()

        return_code = await process.wait()
        if return_code != 0:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error=stderr or buffered_result or f"Claude Code exited with status {return_code}",
            )
            return

        if buffered_result:
            yield StreamEvent(
                type=StreamEventType.TEXT_DELTA,
                text_delta=TextDelta(buffered_result),
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason="stop",
            usage=usage,
        )


def _estimate_context_tokens(messages: list[dict[str, Any]]) -> int:
    total_chars = 0
    for message in messages:
        total_chars += len(str(message.get("content", "")))
        if message.get("tool_calls"):
            total_chars += len(str(message["tool_calls"]))
    return max(total_chars // 4, 1)


def _anthropic_messages_url(config: Config) -> str:
    base_url = config.model.base_url or "https://api.anthropic.com"
    return f"{base_url.rstrip('/')}/v1/messages"


def _anthropic_headers(config: Config) -> dict[str, str]:
    return {
        "x-api-key": config.api_key or "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def _anthropic_usage(raw: dict[str, Any]) -> TokenUsage:
    prompt_tokens = int(raw.get("input_tokens") or 0)
    completion_tokens = int(raw.get("output_tokens") or 0)
    cache_creation = int(raw.get("cache_creation_input_tokens") or 0)
    cache_read = int(raw.get("cache_read_input_tokens") or 0)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cached_tokens=cache_creation + cache_read,
    )


def _safe_httpx_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]
    return f"HTTP {response.status_code}: {body}"


def _build_anthropic_payload(
    config: Config,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    stream: bool,
) -> dict[str, Any]:
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": pending_tool_results,
                }
            )
            pending_tool_results = []

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")

        if role == "system":
            if content:
                system_parts.append(str(content))
            continue

        if role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": str(content),
                    "is_error": bool(message.get("is_error", False)),
                }
            )
            continue

        flush_tool_results()

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": parse_tool_call_arguments(
                            function.get("arguments", "{}")
                        ),
                    }
                )
            if blocks:
                anthropic_messages.append({"role": "assistant", "content": blocks})
            continue

        anthropic_messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": str(content)}],
            }
        )

    flush_tool_results()

    payload: dict[str, Any] = {
        "model": config.model_name,
        "messages": anthropic_messages,
        "max_tokens": config.model.max_output_tokens,
        "temperature": config.temperature,
        "stream": stream,
    }

    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    if tools:
        payload["tools"] = [_to_anthropic_tool(tool) for tool in tools]

    return payload


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function") or {}
    parameters = tool.get("parameters")
    if not parameters and function:
        parameters = function.get("parameters")

    return {
        "name": tool.get("name") or function.get("name", ""),
        "description": tool.get("description") or function.get("description", ""),
        "input_schema": parameters or {"type": "object", "properties": {}},
    }


def _build_claude_code_command(config: Config) -> list[str]:
    command = shlex.split(config.model.claude_code_command)
    if not command:
        command = ["claude"]

    args = [
        *command,
        "-p",
        "--output-format",
        "stream-json",
        "--model",
        config.model_name,
        "--permission-mode",
        config.model.claude_code_permission_mode,
    ]

    if config.model.claude_code_max_turns:
        args.extend(["--max-turns", str(config.model.claude_code_max_turns)])

    args.extend(config.model.claude_code_extra_args)
    return args


def _messages_to_claude_code_prompt(messages: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if not content and message.get("tool_calls"):
            content = json.dumps(message["tool_calls"], ensure_ascii=False)

        if not content:
            continue

        heading = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool Result",
        }.get(str(role), str(role).title())
        sections.append(f"## {heading}\n\n{content}")

    return "\n\n".join(sections)


def _extract_claude_code_text(payload: dict[str, Any]) -> str:
    if payload.get("type") == "content_block_delta":
        delta = payload.get("delta") or {}
        text = delta.get("text")
        return text if isinstance(text, str) else ""

    if payload.get("type") == "assistant":
        message = payload.get("message") or {}
        parts = message.get("content") or payload.get("content") or []
        text_parts = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(text_parts)

    return ""


def _extract_claude_code_usage(payload: dict[str, Any]) -> TokenUsage | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        message = payload.get("message")
        if isinstance(message, dict):
            usage = message.get("usage")

    if not isinstance(usage, dict):
        return None

    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(
        usage.get("output_tokens") or usage.get("completion_tokens") or 0
    )
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cached_tokens = cache_creation + cache_read
    total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)

    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
    )
