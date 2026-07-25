import asyncio
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
import click

from agent.agent import Agent
from agent.events import AgentEventType
from agent.persistence import PersistenceManager, SessionSnapshot
from agent.session import Session
from agent.workspace_snapshot import capture_workspace, restore_files
from config.config import ApprovalPolicy, Config
from control_plane.client import control_plane_get
from config.loader import load_config
from safety.policy import RolePolicy
from ui.tui import TUI, get_console

console = get_console()


def _package_version() -> str:
    try:
        return version("nyansa")
    except PackageNotFoundError:
        return "0.1.0"


class CLI:
    def __init__(self, config: Config):
        self.agent: Agent | None = None
        self.config = config
        self.tui = TUI(config, console)

    async def run_single(self, message: str) -> str | None:
        async with Agent(self.config) as agent:
            self.agent = agent
            if message.strip().startswith("/"):
                await self._handle_command(message)
                return ""
            return await self._process_message(message)

    async def run_interactive(self) -> str | None:
        self.tui.print_welcome(
            "Nyansa",
            lines=[
                f"model: {self.config.model_name}",
                f"cwd: {self.config.cwd}",
                "commands: /help /whoami /capabilities /audit verify /config /exit",
            ],
        )

        async with Agent(
            self.config,
            confirmation_callback=self.tui.handle_confirmation,
            trust_callback=self._prompt_trust_project_tools,
        ) as agent:
            self.agent = agent

            while True:
                try:
                    user_input = console.input("\n[user]>[/user] ").strip()
                    if not user_input:
                        continue

                    if user_input.startswith("/"):
                        should_continue = await self._handle_command(user_input)
                        if not should_continue:
                            break
                        continue

                    await self._process_message(user_input)
                except KeyboardInterrupt:
                    console.print("\n[dim]Use /exit to quit[/dim]")
                except EOFError:
                    break

        console.print("\n[dim]Goodbye![/dim]")

    def _prompt_trust_project_tools(self, directory, status) -> bool:
        console.print(
            f"\n[warning]This project defines custom tools in "
            f"{directory}/.nyansa[/warning]"
        )
        if getattr(status, "value", status) == "changed":
            console.print(
                "[warning]They have changed since you last approved them.[/warning]"
            )
        console.print(
            "[dim]Loading them runs this project's code. Only approve repositories "
            "you trust.[/dim]"
        )

        try:
            answer = console.input("Load and trust these tools? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return False

        return answer in {"y", "yes"}

    def _get_tool_kind(self, tool_name: str) -> str | None:
        tool = self.agent.session.tool_registry.get(tool_name)
        if not tool:
            return None

        return tool.kind.value

    def _todo_state(self) -> dict:
        todos_tool = self.agent.session.tool_registry.get("todos")

        return todos_tool.to_dict() if todos_tool else {}

    def _restore_modified_paths(self, session: Session, snapshot: SessionSnapshot) -> None:
        for entry in snapshot.files.get("files", []):
            path = entry.get("path")
            if path:
                session.record_modified_path(path)

    async def _process_message(self, message: str) -> str | None:
        if not self.agent:
            return None

        assistant_streaming = False
        final_response: str | None = None

        async for event in self.agent.run(message):
            if event.type == AgentEventType.TEXT_DELTA:
                content = event.data.get("content", "")
                if not assistant_streaming:
                    self.tui.begin_assistant()
                    assistant_streaming = True
                self.tui.stream_assistant_delta(content)
            elif event.type == AgentEventType.TEXT_COMPLETE:
                final_response = event.data.get("content")
                if assistant_streaming:
                    self.tui.end_assistant()
                    assistant_streaming = False
            elif event.type == AgentEventType.AGENT_ERROR:
                error = event.data.get("error", "Unknown error")
                console.print(f"\n[error]Error: {error}[/error]")
            elif event.type == AgentEventType.AGENT_WARNING:
                warning = event.data.get("message", "")
                console.print(f"\n[warning]{warning}[/warning]")
            elif event.type == AgentEventType.TOOL_CALL_START:
                tool_name = event.data.get("name", "unknown")
                tool_kind = self._get_tool_kind(tool_name)
                self.tui.tool_call_start(
                    event.data.get("call_id", ""),
                    tool_name,
                    tool_kind,
                    event.data.get("arguments", {}),
                )
            elif event.type == AgentEventType.TOOL_CALL_COMPLETE:
                tool_name = event.data.get("name", "unknown")
                tool_kind = self._get_tool_kind(tool_name)
                self.tui.tool_call_complete(
                    event.data.get("call_id", ""),
                    tool_name,
                    tool_kind,
                    event.data.get("success", False),
                    event.data.get("output", ""),
                    event.data.get("error"),
                    event.data.get("metadata"),
                    event.data.get("diff"),
                    event.data.get("truncated", False),
                    event.data.get("exit_code"),
                )

        return final_response

    async def _handle_command(self, command: str) -> bool:
        stripped = command.strip()
        parts = stripped.split(maxsplit=1)
        cmd_name = parts[0].lower()
        cmd_args = parts[1] if len(parts) > 1 else ""
        if cmd_name == "/exit" or cmd_name == "/quit":
            return False
        elif cmd_name == "/help":
            self.tui.show_help()
        elif cmd_name == "/clear":
            self.agent.session.context_manager.clear()
            self.agent.session.loop_detector.clear()
            console.print("[success]Conversation cleared [/success]")
        elif cmd_name == "/config":
            console.print("\n[bold]Current Configuration[/bold]")
            console.print(f"  Model: {self.config.model_name}")
            console.print(f"  Temperature: {self.config.temperature}")
            console.print(f"  Approval: {self.config.approval.value}")
            console.print(f"  Working Dir: {self.config.cwd}")
            console.print(f"  Max Turns: {self.config.max_turns}")
            console.print(f"  Hooks Enabled: {self.config.hooks_enabled}")
            console.print(f"  Control Plane: {self.config.control_plane.enabled}")
            console.print(f"  Actor: {self.config.actor.user_id}")
            console.print(f"  Organization: {self.config.actor.organization_id}")
            console.print(f"  Role: {self.config.actor.role}")
        elif cmd_name == "/whoami":
            self._show_whoami()
        elif cmd_name == "/capabilities":
            self._show_capabilities()
        elif cmd_name == "/audit" and cmd_args == "verify":
            await self._show_audit_verification()
        elif cmd_name == "/model":
            if cmd_args:
                self.config.model_name = cmd_args
                console.print(f"[success]Model changed to: {cmd_args} [/success]")
            else:
                console.print(f"Current model: {self.config.model_name}")
        elif cmd_name == "/approval":
            if cmd_args:
                try:
                    approval = ApprovalPolicy(cmd_args)
                    self.config.approval = approval
                    console.print(
                        f"[success]Approval policy changed to: {cmd_args} [/success]"
                    )
                except ValueError:
                    console.print(
                        f"[error]Incorrect approval policy: {cmd_args} [/error]"
                    )
                    console.print(
                        f"Valid options: {', '.join(p.value for p in ApprovalPolicy)}"
                    )
            else:
                console.print(f"Current approval policy: {self.config.approval.value}")
        elif cmd_name == "/stats":
            stats = self.agent.session.get_stats()
            console.print("\n[bold]Session Statistics [/bold]")
            for key, value in stats.items():
                console.print(f"   {key}: {value}")
        elif cmd_name == "/tools":
            tools = self.agent.session.tool_registry.get_tools()
            console.print(f"\n[bold]Available tools ({len(tools)}) [/bold]")
            for tool in tools:
                console.print(f"  • {tool.name}")
        elif cmd_name == "/mcp":
            mcp_servers = self.agent.session.mcp_manager.get_all_servers()
            console.print(f"\n[bold]MCP Servers ({len(mcp_servers)}) [/bold]")
            for server in mcp_servers:
                status = server["status"]
                status_color = "green" if status == "connected" else "red"
                console.print(
                    f"  • {server['name']}: [{status_color}]{status}[/{status_color}] ({server['tools']} tools)"
                )
        elif cmd_name == "/save":
            persistence_manager = PersistenceManager()
            session_snapshot = SessionSnapshot(
                session_id=self.agent.session.session_id,
                created_at=self.agent.session.created_at,
                updated_at=self.agent.session.updated_at,
                turn_count=self.agent.session.turn_count,
                messages=self.agent.session.context_manager.get_messages(),
                total_usage=self.agent.session.context_manager.total_usage,
                todos=self._todo_state(),
                files=capture_workspace(
                    self.config.cwd, self.agent.session.modified_paths
                ),
            )
            persistence_manager.save_session(session_snapshot)
            console.print(
                f"[success]Session saved: {self.agent.session.session_id}[/success]"
            )
        elif cmd_name == "/sessions":
            persistence_manager = PersistenceManager()
            sessions = persistence_manager.list_sessions()
            console.print("\n[bold]Saved Sessions[/bold]")
            for s in sessions:
                console.print(
                    f"  • {s['session_id']} (turns: {s['turn_count']}, updated: {s['updated_at']})"
                )
        elif cmd_name == "/checkpoints":
            persistence_manager = PersistenceManager()
            checkpoints = persistence_manager.list_checkpoints()
            console.print("\n[bold]Saved Checkpoints[/bold]")
            if not checkpoints:
                console.print("  No checkpoints saved yet")
            for checkpoint in checkpoints:
                console.print(
                    f"  • {checkpoint['checkpoint_id']} "
                    f"(session: {checkpoint['session_id']}, "
                    f"turns: {checkpoint['turn_count']}, "
                    f"updated: {checkpoint['updated_at']})"
                )
        elif cmd_name == "/resume":
            if not cmd_args:
                console.print(f"[error]Usage: /resume <session_id> [/error]")
            else:
                persistence_manager = PersistenceManager()
                snapshot = persistence_manager.load_session(cmd_args)
                if not snapshot:
                    console.print(f"[error]Session does not exist [/error]")
                else:
                    session = Session(
                        config=self.config,
                    )
                    await session.initialize()
                    session.session_id = snapshot.session_id
                    session.created_at = snapshot.created_at
                    session.updated_at = snapshot.updated_at
                    session.turn_count = snapshot.turn_count
                    session.context_manager.total_usage = snapshot.total_usage

                    todos_tool = session.tool_registry.get("todos")
                    if todos_tool and snapshot.todos:
                        todos_tool.from_dict(snapshot.todos)

                    self._restore_modified_paths(session, snapshot)

                    for msg in snapshot.messages:
                        if msg.get("role") == "system":
                            continue
                        elif msg["role"] == "user":
                            session.context_manager.add_user_message(
                                msg.get("content", "")
                            )
                        elif msg["role"] == "assistant":
                            session.context_manager.add_assistant_message(
                                msg.get("content", ""), msg.get("tool_calls")
                            )
                        elif msg["role"] == "tool":
                            session.context_manager.add_tool_result(
                                msg.get("tool_call_id", ""), msg.get("content", "")
                            )

                    await self.agent.session.shutdown(status="ended")

                    self.agent.session = session
                    console.print(
                        f"[success]Resumed session: {session.session_id}[/success]"
                    )
        elif cmd_name == "/checkpoint":
            persistence_manager = PersistenceManager()
            session_snapshot = SessionSnapshot(
                session_id=self.agent.session.session_id,
                created_at=self.agent.session.created_at,
                updated_at=self.agent.session.updated_at,
                turn_count=self.agent.session.turn_count,
                messages=self.agent.session.context_manager.get_messages(),
                total_usage=self.agent.session.context_manager.total_usage,
                todos=self._todo_state(),
                files=capture_workspace(
                    self.config.cwd, self.agent.session.modified_paths
                ),
            )
            checkpoint_id = persistence_manager.save_checkpoint(session_snapshot)
            console.print(f"[success]Checkpoint created: {checkpoint_id}[/success]")
        elif cmd_name == "/restore":
            if not cmd_args:
                console.print(f"[error]Usage: /restore <checkpoint_id> [/error]")
            else:
                persistence_manager = PersistenceManager()
                snapshot = persistence_manager.load_checkpoint(cmd_args)
                if not snapshot:
                    console.print(f"[error]Checkpoint does not exist [/error]")
                else:
                    session = Session(
                        config=self.config,
                    )
                    await session.initialize()
                    session.session_id = snapshot.session_id
                    session.created_at = snapshot.created_at
                    session.updated_at = snapshot.updated_at
                    session.turn_count = snapshot.turn_count
                    session.context_manager.total_usage = snapshot.total_usage

                    todos_tool = session.tool_registry.get("todos")
                    if todos_tool and snapshot.todos:
                        todos_tool.from_dict(snapshot.todos)

                    self._restore_modified_paths(session, snapshot)

                    for msg in snapshot.messages:
                        if msg.get("role") == "system":
                            continue
                        elif msg["role"] == "user":
                            session.context_manager.add_user_message(
                                msg.get("content", "")
                            )
                        elif msg["role"] == "assistant":
                            session.context_manager.add_assistant_message(
                                msg.get("content", ""), msg.get("tool_calls")
                            )
                        elif msg["role"] == "tool":
                            session.context_manager.add_tool_result(
                                msg.get("tool_call_id", ""), msg.get("content", "")
                            )

                    file_summary = restore_files(snapshot.files, self.config.cwd)

                    await self.agent.session.shutdown(status="ended")

                    self.agent.session = session
                    console.print(
                        f"[success]Restored session: {session.session_id}, checkpoint: {cmd_args}[/success]"
                    )
                    console.print(
                        f"  Files reverted: {len(file_summary.reverted)}, "
                        f"deleted: {len(file_summary.deleted)}, "
                        f"skipped: {len(file_summary.skipped)}"
                    )
                    for path, reason in file_summary.skipped:
                        console.print(f"  [warning]skipped {path}: {reason}[/warning]")
        else:
            console.print(f"[error]Unknown command: {cmd_name}[/error]")

        return True

    def _show_whoami(self) -> None:
        console.print("\n[bold]Current Actor[/bold]")
        console.print(f"  User: {self.config.actor.user_id}")
        console.print(f"  Organization: {self.config.actor.organization_id}")
        console.print(f"  Role: {self.config.actor.role}")
        console.print(
            f"  Control Plane: {'enabled' if self.config.control_plane.enabled else 'disabled'}"
        )
        if self.config.role_policy.allowed_tools is None:
            console.print("  Tool Policy: role default")
        else:
            console.print(
                f"  Tool Policy: {len(self.config.role_policy.allowed_tools)} server-allowed tools"
            )

    def _show_capabilities(self) -> None:
        if not self.agent:
            return

        policy = RolePolicy(self.config)
        allowed = []
        denied = []
        for tool in self.agent.session.tool_registry.get_tools():
            if policy.is_tool_allowed(tool.name):
                allowed.append(tool.name)
            else:
                denied.append(tool.name)

        console.print("\n[bold]Capabilities[/bold]")
        console.print(f"  Role: {self.config.actor.role}")
        console.print(f"  Allowed tools ({len(allowed)}): {', '.join(sorted(allowed))}")
        if denied:
            console.print(f"  Denied tools ({len(denied)}): {', '.join(sorted(denied))}")

        usage = self.config.usage_policy

        def _limit(value: int | None) -> str:
            return "no limit" if value is None else str(value)

        if not usage.enabled:
            console.print("  Usage limits: model use is disabled for your role")
        else:
            console.print("  Usage limits:")
            console.print(
                f"    requests — {_limit(usage.requests_per_minute)}/min, "
                f"{_limit(usage.requests_per_hour)}/hour, {_limit(usage.requests_per_day)}/day"
            )
            console.print(
                f"    session — up to {_limit(usage.max_session_minutes)} min, "
                f"{_limit(usage.max_turns_per_session)} turns, "
                f"{_limit(usage.max_context_tokens)} context tokens"
            )

    async def _show_audit_verification(self) -> None:
        if not self.config.control_plane.enabled:
            console.print("[warning]Control plane is disabled[/warning]")
            return

        try:
            result = await control_plane_get(self.config, "/audit/verify")
        except Exception as e:
            console.print(f"[error]Audit verification failed: {e}[/error]")
            return

        status = "valid" if result.get("valid") else "invalid"
        style = "success" if result.get("valid") else "error"
        console.print(f"[{style}]Audit chain: {status}[/{style}]")
        console.print(f"  Events: {result.get('event_count')}")
        if not result.get("valid"):
            console.print(f"  Broken index: {result.get('broken_index')}")
            console.print(f"  Expected: {result.get('expected_hash')}")
            console.print(f"  Actual: {result.get('actual_hash')}")


@click.command()
@click.version_option(version=_package_version(), prog_name="nyansa")
@click.argument("prompt", required=False)
@click.option(
    "--cwd",
    "-c",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Current working directory",
)
def main(
    prompt: str | None,
    cwd: Path | None,
):
    try:
        config = load_config(cwd=cwd)
    except Exception as e:
        console.print(f"[error]Configuration Error: {e}[/error]")
        sys.exit(1)

    errors = config.validate()

    if errors:
        for error in errors:
            console.print(f"[error]{error}[/error]")

        sys.exit(1)

    cli = CLI(config)

    # messages = [{"role": "user", "content": prompt}]
    if prompt:
        result = asyncio.run(cli.run_single(prompt))
        if result is None:
            console.print("[warning]Turn completed without a final text response[/warning]")
    else:
        asyncio.run(cli.run_interactive())

if __name__ == "__main__":
    main()
