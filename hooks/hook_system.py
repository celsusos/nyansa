import asyncio
from dataclasses import dataclass
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from typing import Any
from config.config import Config, HookConfig, HookTrigger
from tools.base import ToolResult

logger = logging.getLogger(__name__)

MAX_HOOK_OUTPUT_CHARS = 4000


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""

    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_HOOK_OUTPUT_CHARS:
        return text[:MAX_HOOK_OUTPUT_CHARS] + "\n[truncated]"

    return text


@dataclass
class HookResult:
    """Outcome of a single hook process."""

    hook_name: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None

    @property
    def blocked(self) -> bool:
        # Blocking protocol: exit 0 allows, any non-zero exit denies.
        return self.exit_code != 0

    @property
    def reason(self) -> str:
        for candidate in (self.stderr, self.stdout, self.error):
            if candidate and candidate.strip():
                return candidate.strip()

        if self.timed_out:
            return f"Hook '{self.hook_name}' timed out"

        return f"Hook '{self.hook_name}' exited with code {self.exit_code}"


@dataclass
class HookDecision:
    """Aggregate verdict of every hook registered for a trigger."""

    allowed: bool = True
    reason: str | None = None
    hook_name: str | None = None
    context: str = ""

    @classmethod
    def denied(cls, result: HookResult) -> "HookDecision":
        return cls(allowed=False, reason=result.reason, hook_name=result.hook_name)


class HookSystem:
    def __init__(self, config: Config):
        self.config = config
        self.hooks: list[HookConfig] = []
        if self.config.hooks_enabled:
            self.hooks = [hook for hook in self.config.hooks if hook.enabled]

    async def _run_hook(self, hook: HookConfig, env: dict[str, str]) -> HookResult:
        try:
            if hook.command:
                return await self._run_command(
                    hook.name, hook.command, hook.timeout_sec, env
                )

            return await self._run_script(hook, env)
        except Exception as e:
            logger.exception("Hook '%s' failed to run", hook.name)
            return HookResult(
                hook_name=hook.name,
                exit_code=1,
                error=f"Hook '{hook.name}' failed to run: {e}",
            )

    async def _run_script(self, hook: HookConfig, env: dict[str, str]) -> HookResult:
        # mkdtemp gives a 0o700 directory owned by this user, so the script is
        # never readable or writable by other local accounts between write and
        # execution (the shared-/tmp swap window).
        script_dir = tempfile.mkdtemp(prefix="nyansa-hook-")
        try:
            script_path = os.path.join(script_dir, "hook.sh")
            with open(
                os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700),
                "w",
            ) as f:
                f.write("#!/bin/bash\n")
                f.write(hook.script)

            return await self._run_command(
                hook.name, script_path, hook.timeout_sec, env
            )
        finally:
            shutil.rmtree(script_dir, ignore_errors=True)

    async def _run_command(
        self,
        hook_name: str,
        command: str,
        timeout: float,
        env: dict[str, str],
    ) -> HookResult:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.cwd,
            env=env,
            start_new_session=True,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
            logger.warning("Hook '%s' timed out after %ss", hook_name, timeout)
            return HookResult(
                hook_name=hook_name,
                exit_code=124,
                timed_out=True,
                error=f"Hook '{hook_name}' timed out after {timeout}s",
            )

        result = HookResult(
            hook_name=hook_name,
            exit_code=process.returncode if process.returncode is not None else 1,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
        )

        if result.blocked:
            logger.warning(
                "Hook '%s' exited with code %s: %s",
                hook_name,
                result.exit_code,
                result.reason,
            )

        return result

    def _build_env(
        self,
        trigger: HookTrigger,
        tool_name: str | None = None,
        user_message: str | None = None,
        error: Exception | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["NYANSA_TRIGGER"] = trigger.value
        env["NYANSA_CWD"] = str(self.config.cwd)

        if tool_name:
            env["NYANSA_TOOL_NAME"] = tool_name

        if user_message:
            env["NYANSA_USER_MESSAGE"] = user_message

        if error:
            env["NYANSA_ERROR"] = str(error)

        return env

    def _hooks_for(self, trigger: HookTrigger) -> list[HookConfig]:
        return [hook for hook in self.hooks if hook.trigger == trigger]

    async def _run_blocking(
        self,
        trigger: HookTrigger,
        env: dict[str, str],
    ) -> HookDecision:
        """Run every hook for a trigger, stopping at the first denial."""
        context: list[str] = []

        for hook in self._hooks_for(trigger):
            result = await self._run_hook(hook, env)
            if result.blocked:
                return HookDecision.denied(result)

            if result.stdout.strip():
                context.append(result.stdout.strip())

        return HookDecision(allowed=True, context="\n".join(context))

    async def _run_notifying(self, trigger: HookTrigger, env: dict[str, str]) -> None:
        """Run every hook for a trigger. The verdict cannot block anything here."""
        for hook in self._hooks_for(trigger):
            await self._run_hook(hook, env)

    async def trigger_before_agent(self, user_message: str) -> HookDecision:
        env = self._build_env(
            HookTrigger.BEFORE_AGENT,
            user_message=user_message,
        )

        return await self._run_blocking(HookTrigger.BEFORE_AGENT, env)

    async def trigger_after_agent(
        self,
        user_message: str,
        agent_response: str | None,
    ) -> None:
        env = self._build_env(
            HookTrigger.AFTER_AGENT,
            user_message=user_message,
        )
        env["NYANSA_RESPONSE"] = agent_response or ""

        await self._run_notifying(HookTrigger.AFTER_AGENT, env)

    async def trigger_before_tool(
        self,
        tool_name: str,
        tool_params: dict[str, Any],
    ) -> HookDecision:
        """Gate a tool call.

        A before_tool hook that exits non-zero denies the call; its stderr is
        surfaced to the model as the denial reason. A timeout or a hook that
        cannot be launched also denies: an unanswered gate is a closed gate.
        """
        env = self._build_env(HookTrigger.BEFORE_TOOL, tool_name=tool_name)
        env["NYANSA_TOOL_PARAMS"] = json.dumps(tool_params)

        return await self._run_blocking(HookTrigger.BEFORE_TOOL, env)

    async def trigger_after_tool(
        self,
        tool_name: str,
        tool_params: dict[str, Any],
        tool_result: ToolResult,
    ) -> None:
        env = self._build_env(HookTrigger.AFTER_TOOL, tool_name=tool_name)
        env["NYANSA_TOOL_PARAMS"] = json.dumps(tool_params)
        env["NYANSA_TOOL_RESULT"] = tool_result.to_model_output()

        await self._run_notifying(HookTrigger.AFTER_TOOL, env)

    async def trigger_on_error(self, error: Exception) -> None:
        env = self._build_env(HookTrigger.ON_ERROR, error=error)

        await self._run_notifying(HookTrigger.ON_ERROR, env)
