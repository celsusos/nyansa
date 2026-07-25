import os
import shlex
import stat
import sys
import unittest
from pathlib import Path

from config.config import Config, HookConfig, HookTrigger
from hooks.hook_system import HookSystem
from safety.approval import ApprovalManager
from tools.base import ToolResult
from tools.registry import create_default_registry


def make_config(*hooks, cwd=None, enabled=True, allowed_tools=None):
    config = Config(cwd=cwd or Path.cwd())
    config.audit.enabled = False
    config.sandbox.enabled = False
    config.hooks_enabled = enabled
    config.hooks = list(hooks)
    config.role_policy.allowed_tools = allowed_tools
    return config


def hook(command, trigger=HookTrigger.BEFORE_TOOL, name="test-hook", **kwargs):
    return HookConfig(name=name, trigger=trigger, command=command, **kwargs)


class HookResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_zero_allows(self):
        system = HookSystem(make_config(hook("exit 0")))

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertTrue(decision.allowed)

    async def test_non_zero_exit_denies(self):
        system = HookSystem(make_config(hook("exit 1")))

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertFalse(decision.allowed)

    async def test_stderr_becomes_the_denial_reason(self):
        system = HookSystem(make_config(hook("echo 'policy: no shell' >&2; exit 2")))

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertFalse(decision.allowed)
        self.assertIn("policy: no shell", decision.reason)
        self.assertEqual(decision.hook_name, "test-hook")

    async def test_stdout_is_returned_as_context_when_allowed(self):
        system = HookSystem(make_config(hook("echo 'repo is read-only today'")))

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertTrue(decision.allowed)
        self.assertIn("repo is read-only today", decision.context)

    async def test_first_denial_stops_later_hooks(self):
        marker = Path.cwd() / ".hook-should-not-exist"
        system = HookSystem(
            make_config(
                hook("exit 3", name="deny"),
                hook(f"touch {marker}", name="second"),
            )
        )

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.hook_name, "deny")
        self.assertFalse(marker.exists())

    async def test_timeout_denies(self):
        system = HookSystem(make_config(hook("sleep 5", timeout_sec=0.2)))

        with self.assertLogs("hooks.hook_system", level="WARNING"):
            decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertFalse(decision.allowed)
        self.assertIn("timed out", decision.reason)

    async def test_tool_params_are_exposed_to_the_hook(self):
        system = HookSystem(
            make_config(hook('case "$NYANSA_TOOL_PARAMS" in *danger*) exit 1;; esac'))
        )

        allowed = await system.trigger_before_tool("shell", {"command": "ls"})
        denied = await system.trigger_before_tool("shell", {"command": "danger"})

        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)

    async def test_disabled_hooks_do_not_block(self):
        system = HookSystem(make_config(hook("exit 1"), enabled=False))

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertTrue(decision.allowed)

    async def test_after_tool_hook_does_not_gate_before_tool(self):
        system = HookSystem(
            make_config(hook("exit 1", trigger=HookTrigger.AFTER_TOOL))
        )

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertTrue(decision.allowed)
        # after_tool is a notification: it must not raise even when it fails.
        await system.trigger_after_tool(
            "shell", {"command": "ls"}, ToolResult.success_result("ok")
        )


class HookFailureHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_hook_launch_failure_is_logged_and_denies(self):
        system = HookSystem(make_config(hook("exit 0")))

        async def broken(*args, **kwargs):
            raise OSError("cannot spawn")

        system._run_command = broken

        with self.assertLogs("hooks.hook_system", level="ERROR") as logs:
            decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertFalse(decision.allowed)
        self.assertTrue(any("cannot spawn" in line for line in logs.output))

    async def test_on_error_hook_is_triggered(self):
        marker = Path.cwd() / ".hook-on-error-marker"
        marker.unlink(missing_ok=True)
        system = HookSystem(
            make_config(
                hook(f"touch {marker}", trigger=HookTrigger.ON_ERROR),
            )
        )

        try:
            await system.trigger_on_error(RuntimeError("boom"))
            self.assertTrue(marker.exists())
        finally:
            marker.unlink(missing_ok=True)


class InlineScriptHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_script_runs_and_can_deny(self):
        system = HookSystem(
            make_config(
                HookConfig(
                    name="inline",
                    trigger=HookTrigger.BEFORE_TOOL,
                    script="echo denied >&2\nexit 1\n",
                )
            )
        )

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertFalse(decision.allowed)
        self.assertIn("denied", decision.reason)

    async def test_inline_script_is_not_world_accessible(self):
        # The script reports its own directory and file modes before exiting.
        python = shlex.quote(sys.executable)
        system = HookSystem(
            make_config(
                HookConfig(
                    name="inline-perms",
                    trigger=HookTrigger.BEFORE_TOOL,
                    script=(
                        f"{python} -c 'import os, stat, sys; "
                        "print(\"%o %o\" % ("
                        "stat.S_IMODE(os.stat(sys.argv[1]).st_mode), "
                        "stat.S_IMODE(os.stat(sys.argv[2]).st_mode)))' "
                        '"$0" "$(dirname "$0")"\n'
                    ),
                )
            )
        )

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        self.assertTrue(decision.allowed)
        modes = [int(part, 8) for part in decision.context.split()[:2]]
        for mode in modes:
            self.assertFalse(mode & stat.S_IRWXO, f"world bits set in {oct(mode)}")
            self.assertFalse(mode & stat.S_IRWXG, f"group bits set in {oct(mode)}")

    async def test_script_directory_is_removed_afterwards(self):
        system = HookSystem(
            make_config(
                HookConfig(
                    name="inline-cleanup",
                    trigger=HookTrigger.BEFORE_TOOL,
                    script='echo "$0"\n',
                )
            )
        )

        decision = await system.trigger_before_tool("shell", {"command": "ls"})

        script_path = decision.context.strip()
        self.assertTrue(script_path)
        self.assertFalse(os.path.exists(script_path))
        self.assertFalse(os.path.exists(os.path.dirname(script_path)))


class RegistryHookEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_denying_hook_prevents_tool_execution(self):
        marker = Path.cwd() / ".hook-registry-marker"
        marker.unlink(missing_ok=True)
        config = make_config(
            hook("echo 'shell is disabled' >&2; exit 2"),
            allowed_tools=["shell"],
        )
        registry = create_default_registry(config)

        result = await registry.invoke(
            "shell",
            {"command": f"touch {marker}"},
            config.cwd,
            HookSystem(config),
        )

        try:
            self.assertFalse(result.success)
            self.assertIn("shell is disabled", result.error)
            self.assertFalse(marker.exists(), "tool ran despite hook denial")
        finally:
            marker.unlink(missing_ok=True)

    async def test_allowing_hook_lets_tool_run(self):
        config = make_config(hook("exit 0"), allowed_tools=["shell"])
        registry = create_default_registry(config)

        result = await registry.invoke(
            "shell",
            {"command": "echo ran"},
            config.cwd,
            HookSystem(config),
        )

        self.assertTrue(result.success)
        self.assertIn("ran", result.output)

    async def test_hook_denial_escalates_on_failure_policy(self):
        from config.config import ApprovalPolicy

        config = make_config(hook("exit 1"), allowed_tools=["shell"])
        registry = create_default_registry(config)
        approval_manager = ApprovalManager(ApprovalPolicy.ON_FAILURE, config.cwd)

        await registry.invoke(
            "shell",
            {"command": "echo hi"},
            config.cwd,
            HookSystem(config),
            approval_manager=approval_manager,
        )

        self.assertTrue(approval_manager.failure_escalated)


if __name__ == "__main__":
    unittest.main()
