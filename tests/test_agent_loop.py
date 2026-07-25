import asyncio
import unittest

from agent.agent import Agent
from client.response import ToolCall
from config.config import Config
from tools.base import ToolResult


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"id_{name}", name=name, arguments=arguments)


def names(groups) -> list[list[str]]:
    return [[tool_call.name for tool_call in group] for group in groups]


class ParallelGroupingTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(Config())

    def test_consecutive_read_only_calls_are_batched(self):
        groups = self.agent._group_parallel_calls(
            [
                call("read_file", path="a.py"),
                call("read_file", path="b.py"),
                call("grep", pattern="TODO"),
            ]
        )

        self.assertEqual(names(groups), [["read_file", "read_file", "grep"]])

    def test_mutating_call_runs_alone(self):
        groups = self.agent._group_parallel_calls(
            [
                call("read_file", path="a.py"),
                call("edit", path="a.py"),
                call("read_file", path="b.py"),
            ]
        )

        self.assertEqual(names(groups), [["read_file"], ["edit"], ["read_file"]])

    def test_shell_is_never_batched(self):
        groups = self.agent._group_parallel_calls(
            [call("shell", command="ls"), call("shell", command="pwd")]
        )

        self.assertEqual(names(groups), [["shell"], ["shell"]])

    def test_unknown_tool_is_not_parallel_safe(self):
        groups = self.agent._group_parallel_calls(
            [call("read_file", path="a.py"), call("does_not_exist")]
        )

        self.assertEqual(names(groups), [["read_file"], ["does_not_exist"]])

    def test_ordering_is_preserved_across_groups(self):
        calls = [
            call("grep", pattern="a"),
            call("write_file", path="x"),
            call("glob", pattern="*.py"),
            call("list_dir", path="."),
        ]

        flattened = [
            tool_call
            for group in self.agent._group_parallel_calls(calls)
            for tool_call in group
        ]

        self.assertEqual([c.name for c in flattened], [c.name for c in calls])

    def test_empty_call_list(self):
        self.assertEqual(self.agent._group_parallel_calls([]), [])


class ExecuteGroupTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(Config())

    def test_exceptions_become_error_results_without_failing_the_group(self):
        async def fake_invoke(tool_call):
            if tool_call.name == "boom":
                raise RuntimeError("exploded")
            return ToolResult.success_result(f"ran {tool_call.name}")

        self.agent._invoke_tool = fake_invoke

        results = asyncio.run(
            self.agent._execute_tool_group(
                [call("read_file"), call("boom"), call("grep")]
            )
        )

        self.assertEqual(len(results), 3)
        self.assertTrue(results[0].success)
        self.assertFalse(results[1].success)
        self.assertIn("exploded", results[1].error)
        self.assertTrue(results[2].success)

    def test_single_call_group_is_awaited_directly(self):
        async def fake_invoke(tool_call):
            return ToolResult.success_result("only")

        self.agent._invoke_tool = fake_invoke

        results = asyncio.run(self.agent._execute_tool_group([call("read_file")]))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].output, "only")


class VerificationGateTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(Config())

    def test_no_verification_when_nothing_was_modified(self):
        self.assertFalse(self.agent._should_verify(False))

    def test_no_verification_when_disabled(self):
        self.agent.config.verification.enabled = False
        self.agent.config.verification.command = "exit 0"

        self.assertFalse(self.agent._should_verify(True))

    def test_no_verification_when_attempts_exhausted(self):
        self.agent.config.verification.command = "exit 0"
        self.agent.session.verifier.attempts = self.agent.config.verification.max_attempts

        self.assertFalse(self.agent._should_verify(True))

    def test_verifies_when_files_modified_and_command_available(self):
        self.agent.config.verification.command = "exit 0"

        self.assertTrue(self.agent._should_verify(True))


if __name__ == "__main__":
    unittest.main()
