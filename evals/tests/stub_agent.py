from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent.events import AgentEvent
from client.response import TokenUsage
from config.config import Config
from tools.base import ToolResult


@dataclass
class StubContextManager:
    total_usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class StubSession:
    turn_count: int = 0
    context_manager: StubContextManager = field(default_factory=StubContextManager)


class StubAgent:
    """Applies a scripted set of file writes instead of calling a model."""

    def __init__(
        self,
        config: Config,
        writes: dict[str, str] | None = None,
        tool_events: list[tuple[str, bool]] | None = None,
        turns: int = 1,
        usage: TokenUsage | None = None,
        raises: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self.config = config
        self.writes = writes or {}
        self.tool_events = tool_events or []
        self.raises = raises
        self.hang = hang
        self.session = StubSession(
            turn_count=turns,
            context_manager=StubContextManager(usage or TokenUsage()),
        )
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> StubAgent:
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.exited = True
        self.session = None

    async def run(self, message: str):
        yield AgentEvent.agent_start(message)

        if self.raises:
            raise self.raises

        if self.hang:
            import asyncio

            await asyncio.sleep(3600)

        for index, (name, success) in enumerate(self.tool_events):
            arguments = {"path": "unused"}
            yield AgentEvent.tool_call_start(f"call-{index}", name, arguments)
            result = (
                ToolResult.success_result("ok")
                if success
                else ToolResult.error_result("stubbed failure")
            )
            yield AgentEvent.tool_call_complete(f"call-{index}", name, result)

        for relative, content in self.writes.items():
            path = Path(self.config.cwd) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))

        yield AgentEvent.text_complete("done")
        yield AgentEvent.agent_end("done")


def stub_factory(**kwargs) -> Callable[[Config, Any], StubAgent]:
    def factory(config: Config, _on_confirmation: Any) -> StubAgent:
        return StubAgent(config, **kwargs)

    return factory
