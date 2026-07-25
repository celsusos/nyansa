import os
from pathlib import Path
import tempfile
import unittest

from config.config import Config
from tools.discovery import (
    TRUST_STORE_ENV_VAR,
    AGENT_DIR_NAME,
    TOOLS_SUBDIR,
    TrustStatus,
    ToolDiscoveryManager,
    fingerprint_directory,
    revoke_directory,
    trust_directory,
    trust_status,
)
from tools.registry import ToolRegistry

TOOL_SOURCE = '''
from tools.base import Tool, ToolInvocation, ToolKind, ToolResult
from pydantic import BaseModel


class PingParams(BaseModel):
    pass


class PingTool(Tool):
    name = "ping"
    description = "ping"
    kind = ToolKind.READ
    schema = PingParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult.success_result("pong")
'''


class DiscoveryTrustTests(unittest.TestCase):
    def setUp(self):
        self._store = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._store.close()
        os.unlink(self._store.name)
        os.environ[TRUST_STORE_ENV_VAR] = self._store.name
        self.addCleanup(self._cleanup)

        self._project = tempfile.TemporaryDirectory()
        self.project = Path(self._project.name)
        self.addCleanup(self._project.cleanup)

        tool_dir = self.project / AGENT_DIR_NAME / TOOLS_SUBDIR
        tool_dir.mkdir(parents=True)
        (tool_dir / "ping.py").write_text(TOOL_SOURCE, encoding="utf-8")

    def _cleanup(self):
        os.environ.pop(TRUST_STORE_ENV_VAR, None)
        Path(self._store.name).unlink(missing_ok=True)

    def registry(self) -> ToolRegistry:
        return ToolRegistry(Config())

    def manager(self, registry, trust_callback=None) -> ToolDiscoveryManager:
        config = Config()
        config.cwd = self.project
        return ToolDiscoveryManager(config, registry, trust_callback=trust_callback)

    def test_untrusted_directory_is_not_executed(self):
        registry = self.registry()
        self.manager(registry).discover_from_directory(self.project)

        self.assertIsNone(registry.get("ping"))
        self.assertEqual(trust_status(self.project), TrustStatus.UNTRUSTED)

    def test_declining_the_prompt_does_not_load(self):
        registry = self.registry()
        self.manager(registry, trust_callback=lambda d, s: False).discover_from_directory(
            self.project
        )

        self.assertIsNone(registry.get("ping"))

    def test_accepting_the_prompt_loads_and_persists_trust(self):
        registry = self.registry()
        prompts: list = []

        def approve(directory, status):
            prompts.append((directory, status))
            return True

        self.manager(registry, trust_callback=approve).discover_from_directory(
            self.project
        )

        self.assertIsNotNone(registry.get("ping"))
        self.assertEqual(len(prompts), 1)
        self.assertEqual(trust_status(self.project), TrustStatus.TRUSTED)

    def test_previously_trusted_directory_loads_without_prompting(self):
        trust_directory(self.project)
        registry = self.registry()

        def fail(directory, status):
            raise AssertionError("should not prompt for an already-trusted dir")

        self.manager(registry, trust_callback=fail).discover_from_directory(self.project)

        self.assertIsNotNone(registry.get("ping"))

    def test_changed_directory_requires_reapproval(self):
        trust_directory(self.project)
        tool_file = self.project / AGENT_DIR_NAME / TOOLS_SUBDIR / "ping.py"
        tool_file.write_text(TOOL_SOURCE + "\n# edited\n", encoding="utf-8")

        self.assertEqual(trust_status(self.project), TrustStatus.CHANGED)

        registry = self.registry()
        self.manager(registry, trust_callback=lambda d, s: False).discover_from_directory(
            self.project
        )
        self.assertIsNone(registry.get("ping"))

    def test_fingerprint_changes_with_content(self):
        before = fingerprint_directory(self.project)
        (self.project / AGENT_DIR_NAME / TOOLS_SUBDIR / "ping.py").write_text(
            TOOL_SOURCE + "\n# x\n", encoding="utf-8"
        )

        self.assertNotEqual(before, fingerprint_directory(self.project))

    def test_revoke_returns_to_untrusted(self):
        trust_directory(self.project)
        self.assertEqual(trust_status(self.project), TrustStatus.TRUSTED)

        self.assertTrue(revoke_directory(self.project))
        self.assertEqual(trust_status(self.project), TrustStatus.UNTRUSTED)

    def test_broken_tool_file_does_not_abort_discovery(self):
        tool_dir = self.project / AGENT_DIR_NAME / TOOLS_SUBDIR
        (tool_dir / "broken.py").write_text("this is not valid python !!!", encoding="utf-8")
        trust_directory(self.project)

        registry = self.registry()
        self.manager(registry).discover_from_directory(self.project)

        self.assertIsNotNone(registry.get("ping"))

    def test_discovery_disabled_by_config(self):
        trust_directory(self.project)
        registry = self.registry()
        config = Config()
        config.cwd = self.project
        config.tool_discovery_enabled = False

        ToolDiscoveryManager(config, registry).discover_all()

        self.assertIsNone(registry.get("ping"))


if __name__ == "__main__":
    unittest.main()
