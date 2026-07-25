import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.config import Config, SandboxConfig
from safety import sandbox
from safety.sandbox import (
    build_rlimit_preexec,
    build_sandboxed_command,
    bubblewrap_command,
    resolve_backend,
    sandbox_exec_command,
    sbpl_profile,
)
from tools.base import ToolInvocation
from tools.builtin.shell import ShellTool, is_blocked_command

POSIX = os.name == "posix" and sandbox.resource is not None


class TestBackendResolution(unittest.TestCase):
    def test_auto_prefers_bubblewrap(self):
        policy = SandboxConfig(backend="auto")
        with mock.patch.object(sandbox, "bubblewrap_available", return_value=True):
            with mock.patch.object(sandbox, "sandbox_exec_available", return_value=True):
                self.assertEqual(resolve_backend(policy), "bubblewrap")

    def test_auto_falls_to_sandbox_exec(self):
        policy = SandboxConfig(backend="auto")
        with mock.patch.object(sandbox, "bubblewrap_available", return_value=False):
            with mock.patch.object(sandbox, "sandbox_exec_available", return_value=True):
                self.assertEqual(resolve_backend(policy), "sandbox_exec")

    def test_auto_falls_to_rlimit_when_nothing_available(self):
        policy = SandboxConfig(backend="auto")
        with mock.patch.object(sandbox, "bubblewrap_available", return_value=False):
            with mock.patch.object(sandbox, "sandbox_exec_available", return_value=False):
                self.assertEqual(resolve_backend(policy), "rlimit")

    def test_explicit_unavailable_backend_falls_back_with_warning(self):
        policy = SandboxConfig(backend="bubblewrap")
        with mock.patch.object(sandbox, "bubblewrap_available", return_value=False):
            with self.assertLogs("safety.sandbox", level="WARNING") as cm:
                self.assertEqual(resolve_backend(policy), "rlimit")
        self.assertTrue(any("bwrap" in line for line in cm.output))

    def test_none_backend_disables(self):
        self.assertEqual(resolve_backend(SandboxConfig(backend="none")), "none")

    def test_disabled_config_disables(self):
        self.assertEqual(resolve_backend(SandboxConfig(enabled=False)), "none")


class TestBubblewrapWrapping(unittest.TestCase):
    def setUp(self):
        self.cwd = Path("/work/space")
        self.base = ["/bin/bash", "-c", "echo hi"]

    def test_confined_no_network(self):
        policy = SandboxConfig(confine_to_workspace=True, allow_network=False)
        argv = bubblewrap_command(policy, self.base, self.cwd)
        self.assertEqual(argv[0], "bwrap")
        self.assertIn("--ro-bind", argv)
        self.assertIn("--unshare-net", argv)
        self.assertEqual(argv[-3:], self.base)
        i = argv.index("--chdir")
        self.assertEqual(argv[i + 1], str(self.cwd))
        b = argv.index("--bind")
        self.assertEqual(argv[b + 1 : b + 3], [str(self.cwd), str(self.cwd)])

    def test_network_allowed_has_no_unshare(self):
        policy = SandboxConfig(allow_network=True)
        argv = bubblewrap_command(policy, self.base, self.cwd)
        self.assertNotIn("--unshare-net", argv)

    def test_writable_paths_are_bound(self):
        policy = SandboxConfig(writable_paths=["/data"])
        argv = bubblewrap_command(policy, self.base, self.cwd)
        joined = " ".join(argv)
        self.assertIn("--bind /data /data", joined)

    def test_unconfined_binds_root_readwrite(self):
        policy = SandboxConfig(confine_to_workspace=False)
        argv = bubblewrap_command(policy, self.base, self.cwd)
        joined = " ".join(argv)
        self.assertIn("--bind / /", joined)
        self.assertNotIn("--ro-bind / /", joined)


class TestSandboxExecWrapping(unittest.TestCase):
    def setUp(self):
        self.cwd = Path("/work/space")
        self.base = ["/bin/bash", "-c", "echo hi"]

    def test_wrapper_shape(self):
        policy = SandboxConfig()
        argv = sandbox_exec_command(policy, self.base, self.cwd)
        self.assertEqual(argv[0], "sandbox-exec")
        self.assertEqual(argv[1], "-p")
        self.assertEqual(argv[3:], self.base)

    def test_profile_denies_writes_and_network(self):
        policy = SandboxConfig(confine_to_workspace=True, allow_network=False)
        profile = sbpl_profile(policy, self.cwd)
        self.assertIn("(deny file-write*)", profile)
        self.assertIn('(subpath "/work/space")', profile)
        self.assertIn("(deny network*)", profile)

    def test_profile_allows_network_when_configured(self):
        policy = SandboxConfig(allow_network=True)
        profile = sbpl_profile(policy, self.cwd)
        self.assertNotIn("(deny network*)", profile)

    def test_unconfined_profile_has_no_write_deny(self):
        policy = SandboxConfig(confine_to_workspace=False)
        profile = sbpl_profile(policy, self.cwd)
        self.assertNotIn("(deny file-write*)", profile)


@unittest.skipUnless(POSIX, "resource limits require POSIX")
class TestRlimitPreexec(unittest.TestCase):
    def test_cpu_limit_is_applied_in_child(self):
        policy = SandboxConfig(cpu_seconds=37)
        preexec = build_rlimit_preexec(policy)
        self.assertIsNotNone(preexec)

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import resource;print(resource.getrlimit(resource.RLIMIT_CPU)[0])",
            ],
            capture_output=True,
            text=True,
            preexec_fn=preexec,
        )
        self.assertEqual(result.stdout.strip(), "37")

    def test_core_dumps_disabled(self):
        policy = SandboxConfig()
        preexec = build_rlimit_preexec(policy)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import resource;print(resource.getrlimit(resource.RLIMIT_CORE)[0])",
            ],
            capture_output=True,
            text=True,
            preexec_fn=preexec,
        )
        self.assertEqual(result.stdout.strip(), "0")


class TestSandboxedEcho(unittest.TestCase):
    def test_echo_runs_on_resolved_backend(self):
        policy = SandboxConfig()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cmd = build_sandboxed_command(
                policy, ["/bin/bash", "-c", "echo sandbox-ok"], cwd
            )
            result = subprocess.run(
                cmd.argv,
                capture_output=True,
                text=True,
                cwd=cwd,
                preexec_fn=cmd.preexec_fn,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sandbox-ok", result.stdout)


@unittest.skipUnless(
    sandbox.sandbox_exec_available(), "sandbox-exec binary not present"
)
class TestSandboxExecEnforcement(unittest.TestCase):
    def _run(self, policy, cwd, command):
        cmd = build_sandboxed_command(policy, ["/bin/bash", "-c", command], cwd)
        return subprocess.run(
            cmd.argv, capture_output=True, text=True, cwd=cwd, preexec_fn=cmd.preexec_fn
        )

    def test_filesystem_confinement_blocks_outside_writes(self):
        policy = SandboxConfig(backend="sandbox_exec", confine_to_workspace=True)
        target = Path.home() / f".sandbox_confine_test_{os.getpid()}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = self._run(policy, Path(tmp), f"echo x > {target}")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
        finally:
            target.unlink(missing_ok=True)

    def test_workspace_writes_are_allowed(self):
        policy = SandboxConfig(backend="sandbox_exec", confine_to_workspace=True)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self._run(policy, cwd, "echo x > inside.txt")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((cwd / "inside.txt").exists())

    def test_network_is_denied(self):
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3).close()
        except OSError:
            self.skipTest("no baseline network connectivity")

        policy = SandboxConfig(backend="sandbox_exec", allow_network=False)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(
                policy,
                Path(tmp),
                'python3 -c "import socket;socket.create_connection((\'8.8.8.8\',53),3)"',
            )
        self.assertNotEqual(result.returncode, 0)


class TestShellIntegration(unittest.TestCase):
    def _execute(self, config, command):
        tool = ShellTool(config)
        with tempfile.TemporaryDirectory() as tmp:
            invocation = ToolInvocation(params={"command": command}, cwd=Path(tmp))
            return asyncio.run(tool.execute(invocation))

    def test_command_runs_with_sandbox_enabled(self):
        config = Config()
        config.sandbox.enabled = True
        result = self._execute(config, "echo integration-ok")
        self.assertTrue(result.success, result.error)
        self.assertIn("integration-ok", result.output)

    def test_command_runs_with_sandbox_disabled(self):
        config = Config()
        config.sandbox.enabled = False
        result = self._execute(config, "echo raw-ok")
        self.assertTrue(result.success, result.error)
        self.assertIn("raw-ok", result.output)

    def test_disabled_sandbox_skips_wrapping(self):
        config = Config()
        config.sandbox.enabled = False
        tool = ShellTool(config)
        with mock.patch(
            "tools.builtin.shell.build_sandboxed_command"
        ) as build:
            with tempfile.TemporaryDirectory() as tmp:
                invocation = ToolInvocation(
                    params={"command": "echo hi"}, cwd=Path(tmp)
                )
                asyncio.run(tool.execute(invocation))
        build.assert_not_called()

    def test_destructive_command_detection_is_case_and_flag_order_insensitive(self):
        self.assertTrue(is_blocked_command("RM -fr /tmp/something"))
        self.assertTrue(is_blocked_command("echo ok; rm -Rf /"))
        self.assertTrue(is_blocked_command("DD if=/dev/zero of=disk.img"))
        self.assertFalse(is_blocked_command("printf 'rm -rf is documented here'"))


if __name__ == "__main__":
    unittest.main()
