from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from agent.events import AgentEvent, AgentEventType

EDIT_TOOL_NAMES = frozenset({"edit"})
MUTATING_TOOL_NAMES = frozenset({"edit", "write_file"})


@dataclass
class RunMetrics:
    turns: int = 0
    wall_clock_sec: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    edit_calls: int = 0
    edit_failures: int = 0
    successful_mutations: int = 0
    confirmations_denied: int = 0
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    agent_errors: list[str] = field(default_factory=list)

    @property
    def edit_failure_rate(self) -> float:
        if not self.edit_calls:
            return 0.0
        return self.edit_failures / self.edit_calls

    def to_dict(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "wall_clock_sec": round(self.wall_clock_sec, 2),
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "edit_calls": self.edit_calls,
            "edit_failures": self.edit_failures,
            "edit_failure_rate": round(self.edit_failure_rate, 4),
            "successful_mutations": self.successful_mutations,
            "confirmations_denied": self.confirmations_denied,
            "tool_call_counts": dict(sorted(self.tool_call_counts.items())),
            "agent_errors": list(self.agent_errors),
        }


class MetricsCollector:
    def __init__(self) -> None:
        self.metrics = RunMetrics()

    def record_event(self, event: AgentEvent) -> None:
        if event.type is AgentEventType.TOOL_CALL_COMPLETE:
            self._record_tool_complete(event)
        elif event.type is AgentEventType.AGENT_ERROR:
            self.metrics.agent_errors.append(str(event.data.get("error", "")))

    def _record_tool_complete(self, event: AgentEvent) -> None:
        name = str(event.data.get("name") or "unknown")
        success = bool(event.data.get("success"))

        self.metrics.tool_calls += 1
        self.metrics.tool_call_counts[name] = self.metrics.tool_call_counts.get(name, 0) + 1

        if not success:
            self.metrics.tool_failures += 1

        if name in EDIT_TOOL_NAMES:
            self.metrics.edit_calls += 1
            if not success:
                self.metrics.edit_failures += 1

        if success and name in MUTATING_TOOL_NAMES:
            self.metrics.successful_mutations += 1

    def record_session(self, agent: Any) -> None:
        session = getattr(agent, "session", None)
        if session is None:
            return

        self.metrics.turns = int(getattr(session, "turn_count", 0) or 0)

        context_manager = getattr(session, "context_manager", None)
        usage = getattr(context_manager, "total_usage", None)
        if usage is None:
            return

        self.metrics.total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        self.metrics.prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        self.metrics.completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
