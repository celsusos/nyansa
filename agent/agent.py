from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable
from agent.events import AgentEvent, AgentEventType
from agent.session import Session
from client.response import StreamEventType, TokenUsage, ToolCall, ToolResultMessage
from config.config import Config
from context.compaction import CompactionError
from control_plane.client import send_attestation
from agent.provenance import branch, commit_sha, hash_text, repository_name
from prompts.system import create_loop_breaker_prompt
from tools.base import ToolConfirmation, ToolResult
from tools.builtin.file_filter import ExclusionFilter
from safety.approval import ConfirmationResponse


def format_tool_call_for_context(tool_call: ToolCall) -> dict:
    return {
        "id": tool_call.call_id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.arguments),
        },
    }


class Agent:
    def __init__(
        self,
        config: Config,
        confirmation_callback: Callable[[ToolConfirmation], ConfirmationResponse] | None = None,
        trust_callback: Callable[..., bool] | None = None,
    ):
        self.config = config
        self.session: Session | None = Session(self.config, trust_callback=trust_callback)
        self.session.approval_manager.confirmation_callback = confirmation_callback

    async def run(self, message: str):
        self.session.audit_logger.record(
            "agent.start",
            {
                "session_id": self.session.session_id,
                "message": message,
            },
        )
        await self.session.hook_system.trigger_before_agent(message)
        yield AgentEvent.agent_start(message)
        self.session.context_manager.add_user_message(message)

        final_response: str | None = None

        async for event in self._agentic_loop():
            yield event

            if event.type == AgentEventType.TEXT_COMPLETE:
                final_response = event.data.get("content")

        await self.session.hook_system.trigger_after_agent(message, final_response)
        self.session.audit_logger.record(
            "agent.end",
            {
                "session_id": self.session.session_id,
                "response": final_response or "",
            },
        )

        async for event in self._emit_attestation(message):
            yield event

        yield AgentEvent.agent_end(final_response)

    async def _emit_attestation(
        self, message: str
    ) -> AsyncGenerator[AgentEvent, None]:
        if not self.session.file_hashes:
            return
        if not self.config.control_plane.enabled:
            return
        if not self.config.control_plane.send_attestations:
            return

        cwd = self.config.cwd
        payload = {
            "repository": repository_name(cwd),
            "commit_sha": commit_sha(cwd),
            "branch": branch(cwd),
            "model_id": self.config.model_name,
            "prompt_sha256": hash_text(message),
            "file_hashes": dict(self.session.file_hashes),
            "policy_version": self.config.role_policy.version,
            "organization_id": self.config.actor.organization_id,
            # Identity is derived server-side from the token; sent only as a hint.
            "actor": {
                "user_id": self.config.actor.user_id,
                "organization_id": self.config.actor.organization_id,
                "role": self.config.actor.role,
            },
        }

        try:
            await send_attestation(self.config, payload)
        except RuntimeError as error:
            yield AgentEvent.agent_warning(f"Attestation was not recorded: {error}")

    def _is_parallel_safe(self, tool_call: ToolCall) -> bool:
        tool = self.session.tool_registry.get(tool_call.name)
        if tool is None:
            return False

        try:
            return not tool.is_mutating(tool_call.arguments)
        except Exception:
            return False

    def _group_parallel_calls(
        self, tool_calls: list[ToolCall]
    ) -> list[list[ToolCall]]:
        """Batch consecutive read-only calls; mutating calls stay serial and ordered."""
        groups: list[list[ToolCall]] = []
        batch: list[ToolCall] = []

        for tool_call in tool_calls:
            if self._is_parallel_safe(tool_call):
                batch.append(tool_call)
                continue

            if batch:
                groups.append(batch)
                batch = []
            groups.append([tool_call])

        if batch:
            groups.append(batch)

        return groups

    async def _invoke_tool(self, tool_call: ToolCall) -> ToolResult:
        return await self.session.tool_registry.invoke(
            tool_call.name,
            tool_call.arguments,
            self.config.cwd,
            self.session.hook_system,
            self.session.approval_manager,
            self.session.audit_logger,
        )

    async def _execute_tool_group(self, group: list[ToolCall]) -> list[ToolResult]:
        if len(group) == 1:
            return [await self._invoke_tool(group[0])]

        results = await asyncio.gather(
            *(self._invoke_tool(tool_call) for tool_call in group),
            return_exceptions=True,
        )

        return [
            (
                ToolResult.error_result(f"Tool execution failed: {result}")
                if isinstance(result, BaseException)
                else result
            )
            for result in results
        ]

    def _should_verify(self, files_modified: bool) -> bool:
        verifier = self.session.verifier

        return (
            files_modified
            and verifier.enabled
            and verifier.attempts_remaining()
            and verifier.detect_command() is not None
        )

    def _capture_workspace_hashes(self) -> dict[str, str]:
        cwd = self.config.cwd.resolve()
        exclusions = ExclusionFilter(cwd)
        hashes: dict[str, str] = {}

        for path in cwd.rglob("*"):
            if not path.is_file():
                continue
            if exclusions.is_excluded(path, is_dir=False):
                continue

            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue

            hashes[str(path.relative_to(cwd))] = digest

        return hashes

    def _record_external_runtime_changes(self, before: dict[str, str]) -> list[str]:
        after = self._capture_workspace_hashes()
        changed = sorted(
            path
            for path, digest in after.items()
            if before.get(path) != digest
        )
        deleted = sorted(path for path in before if path not in after)

        for path in changed:
            self.session.record_modified_path(path, after[path])
        for path in deleted:
            self.session.record_modified_path(path, "deleted")

        return [*changed, *deleted]

    async def _agentic_loop(self) -> AsyncGenerator[AgentEvent, None]:
        max_turns = self.config.max_turns
        files_modified = False

        for turn_num in range(max_turns):
            await self.session.record_turn()
            response_text = ""

            # check for context overflow
            if self.session.context_manager.needs_compression():
                try:
                    summary, usage = await self.session.chat_compactor.compress(
                        self.session.context_manager
                    )
                    self.session.context_manager.replace_with_summary(summary)

                    if usage:
                        self.session.context_manager.set_latest_usage(usage)
                        self.session.context_manager.add_usage(usage)
                except CompactionError as error:
                    removed = self.session.context_manager.emergency_trim()
                    yield AgentEvent.agent_warning(
                        f"Context compaction failed ({error}). "
                        f"Dropped the {removed} oldest messages to keep going."
                    )

            tool_schemas = self.session.tool_registry.get_schemas()

            tool_calls: list[ToolCall] = []
            usage: TokenUsage | None = None
            stream_error: str | None = None
            external_before: dict[str, str] | None = None

            if self.config.model.provider == "claude_code":
                external_before = self._capture_workspace_hashes()

            async for event in self.session.client.chat_completion(
                self.session.context_manager.get_messages(),
                tools=tool_schemas if tool_schemas else None,
            ):
                if event.type == StreamEventType.TEXT_DELTA:
                    if event.text_delta:
                        content = event.text_delta.content
                        response_text += content
                        yield AgentEvent.text_delta(content)
                elif event.type == StreamEventType.TOOL_CALL_COMPLETE:
                    if event.tool_call:
                        tool_calls.append(event.tool_call)
                elif event.type == StreamEventType.ERROR:
                    stream_error = event.error or "Unknown error occurred."
                elif event.type == StreamEventType.MESSAGE_COMPLETE:
                    usage = event.usage

            if external_before is not None:
                external_changes = self._record_external_runtime_changes(external_before)
                if external_changes:
                    files_modified = True
                    preview = ", ".join(external_changes[:5])
                    suffix = "" if len(external_changes) <= 5 else f" and {len(external_changes) - 5} more"
                    yield AgentEvent.agent_warning(
                        f"Claude Code modified {len(external_changes)} file(s): {preview}{suffix}"
                    )

            if stream_error:
                if not response_text and not tool_calls:
                    yield AgentEvent.agent_error(stream_error)
                    return

                yield AgentEvent.agent_warning(
                    f"The model response was interrupted: {stream_error}"
                )

            self.session.context_manager.add_assistant_message(
                response_text or None,
                (
                    [format_tool_call_for_context(tc) for tc in tool_calls]
                    if tool_calls
                    else None
                ),
            )
            if response_text:
                yield AgentEvent.text_complete(response_text)
                self.session.loop_detector.record_action(
                    "response",
                    text=response_text,
                )

            if not tool_calls:
                if usage:
                    self.session.context_manager.set_latest_usage(usage)
                    self.session.context_manager.add_usage(usage)

                if self._should_verify(files_modified):
                    command = self.session.verifier.detect_command()
                    yield AgentEvent.agent_warning(f"Verifying with `{command}`...")

                    verification = await self.session.verifier.run()

                    if verification and not verification.passed:
                        files_modified = False
                        self.session.context_manager.add_user_message(
                            verification.to_feedback()
                        )
                        yield AgentEvent.agent_warning(
                            f"`{command}` failed — sending the failure back to the agent."
                        )
                        self.session.context_manager.prune_tool_outputs()
                        continue

                self.session.context_manager.prune_tool_outputs()
                return

            tool_call_results: list[ToolResultMessage] = []

            for group in self._group_parallel_calls(tool_calls):
                for tool_call in group:
                    yield AgentEvent.tool_call_start(
                        tool_call.call_id,
                        tool_call.name,
                        tool_call.arguments,
                    )
                    self.session.audit_logger.record(
                        "tool.start",
                        {
                            "session_id": self.session.session_id,
                            "call_id": tool_call.call_id,
                            "tool_name": tool_call.name,
                            "arguments": tool_call.arguments,
                        },
                    )

                    self.session.loop_detector.record_action(
                        "tool_call",
                        tool_name=tool_call.name,
                        args=tool_call.arguments,
                    )

                results = await self._execute_tool_group(group)

                for tool_call, result in zip(group, results):
                    if result.success and result.diff:
                        files_modified = True
                        self.session.record_modified_path(
                            result.diff.path,
                            hash_text(result.diff.new_content),
                        )

                    yield AgentEvent.tool_call_complete(
                        tool_call.call_id,
                        tool_call.name,
                        result,
                    )
                    self.session.audit_logger.record(
                        "tool.complete",
                        {
                            "session_id": self.session.session_id,
                            "call_id": tool_call.call_id,
                            "tool_name": tool_call.name,
                            "success": result.success,
                            "exit_code": result.exit_code,
                            "error": result.error,
                            "output": result.output,
                        },
                    )

                    tool_call_results.append(
                        ToolResultMessage(
                            tool_call_id=tool_call.call_id,
                            content=result.to_model_output(),
                            is_error=not result.success,
                        )
                    )

            for tool_result in tool_call_results:
                self.session.context_manager.add_tool_result(
                    tool_result.tool_call_id,
                    tool_result.content,
                    is_error=tool_result.is_error,
                )

            loop_detection = self.session.loop_detector.check_for_loop()
            if loop_detection:
                if self.session.loop_detector.should_abort():
                    yield AgentEvent.agent_error(
                        f"Stopped: {loop_detection.message}. The agent repeated itself "
                        "without making progress. Try rephrasing the request."
                    )
                    return

                loop_prompt = create_loop_breaker_prompt(loop_detection.message)
                self.session.context_manager.add_user_message(loop_prompt)

            if usage:
                self.session.context_manager.set_latest_usage(usage)
                self.session.context_manager.add_usage(usage)

            self.session.context_manager.prune_tool_outputs()
        yield AgentEvent.agent_error(
            f"Stopped after the maximum of {max_turns} turns with work still in "
            f"progress{' and files modified' if files_modified else ''}. "
            "The conversation is intact — send another message to continue, or raise "
            "max_turns in config."
        )

    async def __aenter__(self) -> Agent:
        await self.session.initialize()
        return self

    async def __aexit__(
        self,
        exc_type,
        exc_val,
        exc_tb,
    ) -> None:
        if self.session and self.session.client and self.session.mcp_manager:
            await self.session.shutdown(status="ended")
            self.session = None
