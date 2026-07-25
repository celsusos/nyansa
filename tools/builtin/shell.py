import asyncio
import logging
import os
from pathlib import Path
import re
import signal
import sys
from safety.sandbox import build_sandboxed_command
from tools.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Tool,
    ToolConfirmation,
    ToolInvocation,
    ToolKind,
    ToolResult,
    cap_output,
)
from pydantic import BaseModel, Field
import fnmatch

logger = logging.getLogger(__name__)

BLOCKED_COMMANDS = {
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "mkfs",
    "fdisk",
    "parted",
    ":(){ :|:& };:",  # Fork bomb
    "chmod 777 /",
    "chmod -R 777",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init 0",
    "init 6",
}


BLOCKED_COMMAND_PATTERNS = [
    re.compile(r"(^|[;&|]\s*)rm\s+-[a-z]*r[a-z]*f[a-z]*(\s|$)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)rm\s+-[a-z]*f[a-z]*r[a-z]*(\s|$)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)dd\s+if=/dev/(zero|random)(\s|$)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)(mkfs|fdisk|parted|shutdown|reboot|halt|poweroff)(\s|$)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)init\s+[06](\s|$)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)chmod\s+(-R\s+)?777\s+/", re.IGNORECASE),
    re.compile(r":\(\)\{\s*:\|:&\s*\};:", re.IGNORECASE),
]


def is_blocked_command(command: str) -> bool:
    normalized = command.strip()
    lowered = normalized.lower()
    if any(blocked in lowered for blocked in BLOCKED_COMMANDS):
        return True
    return any(pattern.search(normalized) for pattern in BLOCKED_COMMAND_PATTERNS)


class ShellParams(BaseModel):
    command: str = Field(..., description="The shell command to execute")
    timeout: int = Field(
        120, ge=1, le=600, description="Timeout in seconds (default: 120)"
    )
    cwd: str | None = Field(None, description="Working directory for the command")


class ShellTool(Tool):
    name = "shell"
    kind = ToolKind.SHELL
    description = "Execute a shell command. Use this for running system commands, scripts and CLI tools."

    schema = ShellParams

    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

    async def get_confirmation(
        self, invocation: ToolInvocation
    ) -> ToolConfirmation | None:
        params = ShellParams(**invocation.params)

        if is_blocked_command(params.command):
            return ToolConfirmation(
                tool_name=self.name,
                params=invocation.params,
                description=f"Execute (BLOCKED): {params.command}",
                command=params.command,
                is_dangerous=True,
            )

        return ToolConfirmation(
            tool_name=self.name,
            params=invocation.params,
            description=f"Execute: {params.command}",
            command=params.command,
            is_dangerous=False,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = ShellParams(**invocation.params)

        if is_blocked_command(params.command):
            return ToolResult.error_result(
                f"Command blocked for safety: {params.command}",
                metadata={"blocked": True},
            )

        if params.cwd:
            cwd = Path(params.cwd)
            if not cwd.is_absolute():
                cwd = invocation.cwd / cwd
        else:
            cwd = invocation.cwd

        if not cwd.exists():
            return ToolResult.error_result(f"Working directory doesn't exist: {cwd}")

        env = self._build_environment()
        if sys.platform == "win32":
            base_cmd = ["cmd.exe", "/c", params.command]
        else:
            base_cmd = ["/bin/bash", "-c", params.command]

        shell_cmd = base_cmd
        preexec_fn = None
        backend = "none"
        if self.config.sandbox.enabled:
            sandboxed = build_sandboxed_command(self.config.sandbox, base_cmd, cwd)
            shell_cmd = sandboxed.argv
            preexec_fn = sandboxed.preexec_fn
            backend = sandboxed.backend

        try:
            process = await self._spawn(shell_cmd, cwd, env, preexec_fn)
        except (FileNotFoundError, OSError) as exc:
            if backend not in ("bubblewrap", "sandbox_exec"):
                raise
            logger.warning(
                "Sandbox wrapper '%s' failed to launch (%s); falling back to "
                "rlimit-only execution",
                backend,
                exc,
            )
            process = await self._spawn(base_cmd, cwd, env, preexec_fn)

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=params.timeout,
            )
        except asyncio.TimeoutError:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
            return ToolResult.error_result(f"Command timed out after {params.timeout}s")

        stdout = stdout_data.decode("utf-8", errors="replace")
        stderr = stderr_data.decode("utf-8", errors="replace")
        exit_code = process.returncode

        output = ""
        if stdout.strip():
            output += stdout.rstrip()

        if stderr.strip():
            output += "\n--- stderr ---\n"
            output += stderr.rstrip()

        output, truncated = cap_output(
            output,
            hint=(
                "Re-run piping the command through a filter such as `head`, `tail` "
                "or `grep` to see the relevant portion."
            ),
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )

        if exit_code != 0:
            output += f"\nExit code: {exit_code}"

        return ToolResult(
            success=exit_code == 0,
            output=output,
            error=stderr if exit_code != 0 else None,
            exit_code=exit_code,
            truncated=truncated,
        )

    async def _spawn(self, argv, cwd, env, preexec_fn):
        return await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )

    def _build_environment(self) -> dict[str, str]:
        env = os.environ.copy()

        shell_environment = self.config.shell_environment

        if not shell_environment.ignore_default_excludes:
            for pattern in shell_environment.exclude_patterns:
                keys_to_remove = [
                    k for k in env.keys() if fnmatch.fnmatch(k.upper(), pattern.upper())
                ]

                for k in keys_to_remove:
                    del env[k]

        if shell_environment.set_vars:
            env.update(shell_environment.set_vars)

        return env
