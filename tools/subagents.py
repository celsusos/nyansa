import asyncio
from contextvars import ContextVar
import logging
from pathlib import Path
import re
from typing import Any
from config.config import Config
from tools.base import Tool, ToolInvocation, ToolResult
from dataclasses import dataclass, field
from pydantic import BaseModel, Field

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

logger = logging.getLogger(__name__)

MAX_SUBAGENT_DEPTH = 1
MAX_SUBAGENT_TURNS = 100
MAX_SUBAGENT_TIMEOUT_SECONDS = 3600

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

_subagent_depth: ContextVar[int] = ContextVar("subagent_depth", default=0)


def current_subagent_depth() -> int:
    return _subagent_depth.get()


class SubagentParams(BaseModel):
    goal: str = Field(
        ..., description="The specific task or goal for the subagent to accomplish"
    )


@dataclass
class SubagentDefinition:
    name: str
    description: str
    goal_prompt: str
    allowed_tools: list[str] | None = None
    max_turns: int = 20
    timeout_seconds: float = 600


@dataclass
class SubagentOutcome:
    termination: str = "goal"
    final_response: str | None = None
    error: str | None = None
    tool_calls: list[str] = field(default_factory=list)


def _definition_from_dict(data: dict[str, Any], source: Path) -> SubagentDefinition:
    name = data.get("name")
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise ValueError(
            f"'name' must be lowercase alphanumeric with underscores, got {name!r}"
        )

    for key in ("description", "goal_prompt"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"'{key}' is required and must be a non-empty string")

    allowed_tools = data.get("allowed_tools")
    if not isinstance(allowed_tools, list) or not allowed_tools:
        raise ValueError(
            "'allowed_tools' is required and must be a non-empty list of tool names"
        )
    if not all(isinstance(tool, str) for tool in allowed_tools):
        raise ValueError("'allowed_tools' must contain only tool names")

    max_turns = data.get("max_turns", 20)
    if not isinstance(max_turns, int) or not 1 <= max_turns <= MAX_SUBAGENT_TURNS:
        raise ValueError(f"'max_turns' must be between 1 and {MAX_SUBAGENT_TURNS}")

    timeout_seconds = data.get("timeout_seconds", 600)
    if not isinstance(timeout_seconds, (int, float)) or not (
        0 < timeout_seconds <= MAX_SUBAGENT_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"'timeout_seconds' must be between 0 and {MAX_SUBAGENT_TIMEOUT_SECONDS}"
        )

    unknown = set(data) - {
        "name",
        "description",
        "goal_prompt",
        "allowed_tools",
        "max_turns",
        "timeout_seconds",
    }
    if unknown:
        logger.warning(
            f"Ignoring unknown sub-agent keys in {source}: {', '.join(sorted(unknown))}"
        )

    return SubagentDefinition(
        name=name,
        description=data["description"],
        goal_prompt=data["goal_prompt"],
        allowed_tools=list(allowed_tools),
        max_turns=max_turns,
        timeout_seconds=float(timeout_seconds),
    )


def load_subagent_definitions(directory: Path) -> list[SubagentDefinition]:
    definitions: list[SubagentDefinition] = []

    for path in sorted(directory.glob("*.toml")):
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)

            definitions.append(_definition_from_dict(data, path))
        except Exception:
            logger.exception(f"Failed to load sub-agent definition {path}")

    return definitions


class SubagentTool(Tool):
    def __init__(self, config: Config, definition: SubagentDefinition):
        super().__init__(config)
        self.definition = definition

    @property
    def name(self) -> str:
        return f"subagent_{self.definition.name}"

    @property
    def description(self) -> str:
        return f"subagent_{self.definition.description}"

    schema = SubagentParams

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return True

    def _build_prompt(self, goal: str) -> str:
        return f"""You are a specialized sub-agent with a specific task to complete.

        {self.definition.goal_prompt}

        YOUR TASK:
        {goal}

        IMPORTANT:
        - Focus only on completing the specified task
        - Do not engage in unrelated actions
        - Once you have completed the task or have the answer, provide your final response
        - Be concise and direct in your output
        """

    def _build_config(self) -> Config:
        config_dict = self.config.to_dict()
        config_dict["max_turns"] = self.definition.max_turns
        if self.definition.allowed_tools:
            config_dict["allowed_tools"] = self.definition.allowed_tools

        return Config(**config_dict)

    async def _consume_events(
        self, agent: Any, prompt: str, outcome: SubagentOutcome
    ) -> None:
        from agent.events import AgentEventType

        async for event in agent.run(prompt):
            if event.type == AgentEventType.TOOL_CALL_START:
                outcome.tool_calls.append(event.data.get("name"))
            elif event.type == AgentEventType.TEXT_COMPLETE:
                outcome.final_response = event.data.get("content")
            elif event.type == AgentEventType.AGENT_END:
                if outcome.final_response is None:
                    outcome.final_response = event.data.get("response")
            elif event.type == AgentEventType.AGENT_ERROR:
                outcome.termination = "error"
                outcome.error = event.data.get("error", "Unknown")
                outcome.final_response = f"Sub-agent error: {outcome.error}"
                return

    async def _run(self, prompt: str, outcome: SubagentOutcome) -> None:
        from agent.agent import Agent

        depth_token = _subagent_depth.set(current_subagent_depth() + 1)
        try:
            async with Agent(self._build_config()) as agent:
                await asyncio.wait_for(
                    self._consume_events(agent, prompt, outcome),
                    timeout=self.definition.timeout_seconds,
                )
        finally:
            _subagent_depth.reset(depth_token)

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = SubagentParams(**invocation.params)
        if not params.goal:
            return ToolResult.error_result("No goal specified for sub-agent")

        if current_subagent_depth() >= MAX_SUBAGENT_DEPTH:
            return ToolResult.error_result(
                f"Sub-agents cannot start further sub-agents "
                f"(nesting limit of {MAX_SUBAGENT_DEPTH} reached). "
                f"Complete this task directly instead."
            )

        outcome = SubagentOutcome()

        try:
            await self._run(self._build_prompt(params.goal), outcome)
        except (asyncio.TimeoutError, TimeoutError):
            outcome.termination = "timeout"
            outcome.final_response = (
                f"Sub-agent timed out after {self.definition.timeout_seconds} seconds"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            outcome.termination = "error"
            outcome.error = str(e)
            outcome.final_response = f"Sub-agent failed: {e}"

        result = f"""Sub-agent '{self.definition.name}' completed.
        Termination: {outcome.termination}
        Tools called: {', '.join(outcome.tool_calls) if outcome.tool_calls else 'None'}

        Result:
        {outcome.final_response or 'No response'}
        """

        if outcome.error:
            return ToolResult.error_result(result)

        return ToolResult.success_result(result)


CODEBASE_INVESTIGATOR = SubagentDefinition(
    name="codebase_investigator",
    description="Investigates the codebase to answer questions about code structure, patterns, and implementations",
    goal_prompt="""You are a codebase investigation specialist.
Your job is to explore and understand code to answer questions.
Use read_file, grep, glob, and list_dir to investigate.
Do NOT modify any files.""",
    allowed_tools=["read_file", "grep", "glob", "list_dir"],
)

CODE_REVIEWER = SubagentDefinition(
    name="code_reviewer",
    description="Reviews code changes and provides feedback on quality, bugs, and improvements",
    goal_prompt="""You are a code review specialist.
Your job is to review code and provide constructive feedback.
Look for bugs, code smells, security issues, and improvement opportunities.
Use read_file, list_dir and grep to examine the code.
Do NOT modify any files.""",
    allowed_tools=["read_file", "grep", "list_dir"],
    max_turns=10,
    timeout_seconds=300,
)


def get_default_subagent_definitions() -> list[SubagentDefinition]:
    return [
        CODEBASE_INVESTIGATOR,
        CODE_REVIEWER,
    ]
