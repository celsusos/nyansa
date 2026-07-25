import asyncio
from pathlib import Path
import tempfile
import time
import unittest

from config.config import Config
from tools.base import ToolInvocation
from tools.subagents import (
    MAX_SUBAGENT_DEPTH,
    MAX_SUBAGENT_TIMEOUT_SECONDS,
    MAX_SUBAGENT_TURNS,
    SubagentDefinition,
    SubagentOutcome,
    SubagentTool,
    _definition_from_dict,
    _subagent_depth,
    current_subagent_depth,
    load_subagent_definitions,
)

BASE = {
    "name": "agent",
    "description": "d",
    "goal_prompt": "g",
    "allowed_tools": ["read_file"],
}


def definition(**overrides) -> SubagentDefinition:
    return _definition_from_dict({**BASE, **overrides}, Path("x.toml"))


class DefinitionParsingTests(unittest.TestCase):
    def test_valid_definition(self):
        d = definition(timeout_seconds=30, max_turns=5)

        self.assertEqual(d.name, "agent")
        self.assertEqual(d.allowed_tools, ["read_file"])
        self.assertEqual(d.timeout_seconds, 30.0)
        self.assertEqual(d.max_turns, 5)

    def test_invalid_name_rejected(self):
        for bad in ["Agent", "my agent", "1x", "a-b", ""]:
            with self.assertRaises(ValueError):
                definition(name=bad)

    def test_missing_prompt_rejected(self):
        with self.assertRaises(ValueError):
            _definition_from_dict(
                {"name": "a", "description": "d", "allowed_tools": ["read_file"]},
                Path("x"),
            )

    def test_empty_allowed_tools_rejected(self):
        with self.assertRaises(ValueError):
            definition(allowed_tools=[])

    def test_oversized_timeout_rejected(self):
        with self.assertRaises(ValueError):
            definition(timeout_seconds=MAX_SUBAGENT_TIMEOUT_SECONDS + 1)

    def test_zero_timeout_rejected(self):
        with self.assertRaises(ValueError):
            definition(timeout_seconds=0)

    def test_oversized_max_turns_rejected(self):
        with self.assertRaises(ValueError):
            definition(max_turns=MAX_SUBAGENT_TURNS + 1)

    def test_unknown_keys_are_ignored_not_fatal(self):
        d = _definition_from_dict({**BASE, "surprise": 1}, Path("x"))
        self.assertEqual(d.name, "agent")


class LoadDefinitionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, body: str):
        (self.root / name).write_text(body, encoding="utf-8")

    def test_loads_valid_toml_definitions(self):
        self.write(
            "reviewer.toml",
            'name = "reviewer"\ndescription = "d"\ngoal_prompt = "g"\n'
            'allowed_tools = ["read_file", "grep"]\n',
        )

        loaded = load_subagent_definitions(self.root)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "reviewer")

    def test_broken_definition_is_skipped_not_fatal(self):
        self.write("good.toml", 'name = "good"\ndescription = "d"\ngoal_prompt = "g"\nallowed_tools = ["read_file"]\n')
        self.write("bad.toml", "name = = =")

        loaded = load_subagent_definitions(self.root)

        self.assertEqual([d.name for d in loaded], ["good"])

    def test_empty_directory(self):
        self.assertEqual(load_subagent_definitions(self.root), [])


class DepthGuardTests(unittest.TestCase):
    def test_execute_refuses_beyond_max_depth(self):
        tool = SubagentTool(Config(), definition())
        token = _subagent_depth.set(MAX_SUBAGENT_DEPTH)
        try:
            result = asyncio.run(
                tool.execute(ToolInvocation(params={"goal": "x"}, cwd=Path.cwd()))
            )
        finally:
            _subagent_depth.reset(token)

        self.assertFalse(result.success)
        self.assertIn("nesting limit", result.error)

    def test_depth_defaults_to_zero(self):
        self.assertEqual(current_subagent_depth(), 0)


class TimeoutTests(unittest.TestCase):
    def test_timeout_fires_on_a_hung_consume(self):
        class HangTool(SubagentTool):
            async def _consume_events(self, agent, prompt, outcome):
                await asyncio.sleep(30)

        tool = HangTool(Config(), definition(timeout_seconds=0.3))

        start = time.monotonic()
        result = asyncio.run(
            tool.execute(ToolInvocation(params={"goal": "x"}, cwd=Path.cwd()))
        )
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 20)
        self.assertIn("timed out", result.output.lower())

    def test_depth_is_reset_after_a_run(self):
        class QuickTool(SubagentTool):
            async def _consume_events(self, agent, prompt, outcome):
                outcome.final_response = "done"

        tool = QuickTool(Config(), definition())
        asyncio.run(tool.execute(ToolInvocation(params={"goal": "x"}, cwd=Path.cwd())))

        self.assertEqual(current_subagent_depth(), 0)

    def test_empty_goal_is_rejected(self):
        tool = SubagentTool(Config(), definition())
        result = asyncio.run(
            tool.execute(ToolInvocation(params={"goal": ""}, cwd=Path.cwd()))
        )

        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
