import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.agent import format_tool_call_for_context
from client.response import ToolCall
from config.config import ApprovalPolicy, Config
from hooks.hook_system import HookSystem
from safety.audit import AuditLogger
from safety.policy import RolePolicy
from safety.redaction import SensitiveDataRedactor
from main import CLI
from tools.registry import create_default_registry
from utils.text import truncate_text


class RegressionTests(unittest.TestCase):
    def test_auto_edit_approval_policy_value(self):
        self.assertEqual(ApprovalPolicy("auto-edit"), ApprovalPolicy.AUTO_EDIT)

    def test_temperature_setter_updates_model_temperature(self):
        config = Config(cwd=Path.cwd())

        config.temperature = 0.25

        self.assertEqual(config.model.temperature, 0.25)

    def test_self_hosted_model_config_does_not_require_api_key_by_default(self):
        config = Config(cwd=Path.cwd())

        self.assertEqual(config.base_url, "http://localhost:8000/v1")
        self.assertFalse(config.model.requires_api_key)
        self.assertEqual(config.validate(), [])

    def test_cli_get_tool_kind_handles_missing_tool(self):
        config = Config(cwd=Path.cwd())
        cli = CLI(config)
        cli.agent = SimpleNamespace(
            session=SimpleNamespace(
                tool_registry=SimpleNamespace(get=lambda name: None),
            ),
        )

        self.assertIsNone(cli._get_tool_kind("missing_tool"))

    def test_truncate_text_supports_default_model_short_form(self):
        result = truncate_text("word " * 1000, 10)

        self.assertIn("[truncated]", result)

    def test_tool_call_context_arguments_are_json(self):
        message = format_tool_call_for_context(
            ToolCall(
                call_id="call_123",
                name="read_file",
                arguments={"path": "README.md", "limit": 10},
            )
        )

        parsed = json.loads(message["function"]["arguments"])
        self.assertEqual(parsed, {"path": "README.md", "limit": 10})

    def test_sensitive_data_redactor_masks_common_secret_shapes(self):
        config = Config(cwd=Path.cwd())
        redactor = SensitiveDataRedactor(config)

        result = redactor.redact_text("API_KEY=sk-abcdefghijklmnopqrstuvwxyz")

        self.assertGreaterEqual(result.redaction_count, 1)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result.value)

    def test_audit_logger_hashes_content_but_keeps_governance_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "audit.jsonl"
            config = Config(cwd=Path.cwd())
            config.audit.log_path = path
            logger = AuditLogger(config)

            logger.record(
                "tool.start",
                {
                    "tool_name": "shell",
                    "decision": "approved",
                    "message": "inspect proprietary source",
                },
            )

            event = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(event["event_type"], "tool.start")
        self.assertEqual(event["details"]["tool_name"], "shell")
        self.assertEqual(event["details"]["decision"], "approved")
        self.assertIn("sha256", event["details"]["message"])
        self.assertNotIn("inspect proprietary source", json.dumps(event))

    def test_role_policy_restricts_intern_shell_access(self):
        config = Config(cwd=Path.cwd())
        config.actor.role = "intern"

        self.assertFalse(RolePolicy(config).is_tool_allowed("shell"))
        self.assertTrue(RolePolicy(config).is_tool_allowed("read_file"))

    def test_role_policy_uses_control_plane_allowed_tool_override(self):
        config = Config(cwd=Path.cwd())
        config.actor.role = "developer"
        config.role_policy.allowed_tools = ["read_file"]

        self.assertTrue(RolePolicy(config).is_tool_allowed("read_file"))
        self.assertFalse(RolePolicy(config).is_tool_allowed("shell"))


class PolicyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_registry_denies_role_disallowed_tool_before_execution(self):
        config = Config(cwd=Path.cwd())
        config.actor.role = "intern"
        config.audit.enabled = False
        registry = create_default_registry(config)

        result = await registry.invoke(
            "shell",
            {"command": "echo should-not-run"},
            config.cwd,
            HookSystem(config),
            audit_logger=AuditLogger(config),
        )

        self.assertFalse(result.success)
        self.assertIn("not allowed for role", result.error)


class CliUxTests(unittest.TestCase):
    def test_whoami_and_capabilities_commands_do_not_require_model_call(self):
        config = Config(cwd=Path.cwd())
        config.audit.enabled = False
        cli = CLI(config)
        registry = create_default_registry(config)
        cli.agent = SimpleNamespace(
            session=SimpleNamespace(
                tool_registry=registry,
            ),
        )

        cli._show_whoami()
        cli._show_capabilities()


class CLICommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_command_is_case_insensitive(self):
        config = Config(cwd=Path.cwd())
        cli = CLI(config)
        called = []
        cli.tui = SimpleNamespace(show_help=lambda: called.append(True))

        self.assertTrue(await cli._handle_command("/Help"))
        self.assertEqual(called, [True])

    async def test_model_command_preserves_model_id_case(self):
        config = Config(cwd=Path.cwd())
        cli = CLI(config)

        await cli._handle_command("/model Qwen/Qwen2.5-Coder-32B-Instruct")

        self.assertEqual(
            config.model_name,
            "Qwen/Qwen2.5-Coder-32B-Instruct",
        )

    async def test_checkpoints_command_uses_persistence_listing(self):
        config = Config(cwd=Path.cwd())
        cli = CLI(config)
        manager = SimpleNamespace(
            list_checkpoints=lambda: [
                {
                    "checkpoint_id": "sess-1_20260101_120000",
                    "session_id": "sess-1",
                    "turn_count": 3,
                    "updated_at": "2026-01-01T12:00:00",
                }
            ]
        )

        with patch("main.PersistenceManager", return_value=manager):
            self.assertTrue(await cli._handle_command("/checkpoints"))


if __name__ == "__main__":
    unittest.main()
