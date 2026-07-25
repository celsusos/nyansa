import re
import unittest
from pathlib import Path

from config.config import Config
from tools.base import ToolInvocation
from tools.builtin.todo import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    TodosTool,
)


class TodosToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cwd = Path.cwd()
        self.tool = TodosTool(Config(cwd=self.cwd))

    async def run_tool(self, **params):
        return await self.tool.execute(ToolInvocation(params=params, cwd=self.cwd))

    async def add(self, content: str, **params):
        result = await self.run_tool(action="add", content=content, **params)
        self.assertTrue(result.success, result.error)
        match = re.search(r"\[([0-9a-f]{8})\]", result.output)
        return match.group(1)

    async def test_add_and_list(self):
        await self.add("write tests")

        result = await self.run_tool(action="list")

        self.assertIn("write tests", result.output)
        self.assertIn("(medium)", result.output)

    async def test_list_empty(self):
        result = await self.run_tool(action="list")

        self.assertEqual(result.output, "No todos")

    async def test_add_requires_content(self):
        result = await self.run_tool(action="add")

        self.assertFalse(result.success)

    async def test_add_rejects_invalid_priority(self):
        result = await self.run_tool(action="add", content="x", priority="urgent")

        self.assertFalse(result.success)

    async def test_completed_todo_stays_visible(self):
        todo_id = await self.add("write tests")

        await self.run_tool(action="complete", id=todo_id)
        result = await self.run_tool(action="list")

        self.assertIn("write tests", result.output)
        self.assertIn(f"[x] [{todo_id}]", result.output)
        self.assertIn("1 completed", result.output)

    async def test_complete_unknown_id(self):
        result = await self.run_tool(action="complete", id="deadbeef")

        self.assertFalse(result.success)

    async def test_start_marks_in_progress(self):
        todo_id = await self.add("write tests")

        result = await self.run_tool(action="start", id=todo_id)

        self.assertTrue(result.success)
        self.assertIn(f"[~] [{todo_id}]", (await self.run_tool(action="list")).output)

    async def test_only_one_todo_in_progress(self):
        first = await self.add("first")
        second = await self.add("second")

        await self.run_tool(action="start", id=first)
        result = await self.run_tool(action="start", id=second)

        self.assertFalse(result.success)
        self.assertIn(first, result.error)

    async def test_start_allowed_after_completing_active(self):
        first = await self.add("first")
        second = await self.add("second")

        await self.run_tool(action="start", id=first)
        await self.run_tool(action="complete", id=first)
        result = await self.run_tool(action="start", id=second)

        self.assertTrue(result.success)

    async def test_start_rejects_completed_todo(self):
        todo_id = await self.add("first")
        await self.run_tool(action="complete", id=todo_id)

        result = await self.run_tool(action="start", id=todo_id)

        self.assertFalse(result.success)

    async def test_start_blocked_by_dependency(self):
        first = await self.add("first")
        second = await self.add("second", depends_on=[first])

        result = await self.run_tool(action="start", id=second)

        self.assertFalse(result.success)
        self.assertIn(first, result.error)

        await self.run_tool(action="complete", id=first)

        self.assertTrue((await self.run_tool(action="start", id=second)).success)

    async def test_add_rejects_unknown_dependency(self):
        result = await self.run_tool(
            action="add", content="x", depends_on=["deadbeef"]
        )

        self.assertFalse(result.success)

    async def test_clear(self):
        await self.add("first")
        await self.add("second")

        result = await self.run_tool(action="clear")

        self.assertIn("2", result.output)
        self.assertEqual((await self.run_tool(action="list")).output, "No todos")

    async def test_unknown_action(self):
        result = await self.run_tool(action="frobnicate")

        self.assertFalse(result.success)

    async def test_plan_writes_full_list_in_one_call(self):
        result = await self.run_tool(
            action="plan",
            todos=[
                {"id": "a1", "content": "explore", "priority": "high"},
                {"id": "a2", "content": "implement", "depends_on": ["a1"]},
                {"id": "a3", "content": "verify", "priority": "low"},
            ],
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(self.tool.to_dict()["todos"][0]["id"], "a1")
        self.assertIn("explore", result.output)
        self.assertIn("3 pending", result.output)

    async def test_plan_preserves_order(self):
        await self.run_tool(
            action="plan",
            todos=[{"content": "one"}, {"content": "two"}, {"content": "three"}],
        )

        contents = [todo["content"] for todo in self.tool.to_dict()["todos"]]

        self.assertEqual(contents, ["one", "two", "three"])

    async def test_plan_replaces_existing_todos(self):
        await self.add("stale")

        await self.run_tool(action="plan", todos=[{"content": "fresh"}])
        result = await self.run_tool(action="list")

        self.assertNotIn("stale", result.output)
        self.assertIn("fresh", result.output)

    async def test_plan_requires_todos(self):
        result = await self.run_tool(action="plan")

        self.assertFalse(result.success)

    async def test_plan_rejects_entry_without_content(self):
        result = await self.run_tool(action="plan", todos=[{"priority": "high"}])

        self.assertFalse(result.success)

    async def test_plan_rejects_unknown_dependency(self):
        result = await self.run_tool(
            action="plan",
            todos=[{"id": "a1", "content": "one", "depends_on": ["nope"]}],
        )

        self.assertFalse(result.success)

    async def test_plan_rejects_duplicate_ids(self):
        result = await self.run_tool(
            action="plan",
            todos=[{"id": "a1", "content": "one"}, {"id": "a1", "content": "two"}],
        )

        self.assertFalse(result.success)

    async def test_plan_rejects_multiple_in_progress(self):
        result = await self.run_tool(
            action="plan",
            todos=[
                {"content": "one", "status": STATUS_IN_PROGRESS},
                {"content": "two", "status": STATUS_IN_PROGRESS},
            ],
        )

        self.assertFalse(result.success)

    async def test_plan_rejects_invalid_status(self):
        result = await self.run_tool(
            action="plan", todos=[{"content": "one", "status": "doing"}]
        )

        self.assertFalse(result.success)

    async def test_serialization_round_trip(self):
        await self.run_tool(
            action="plan",
            todos=[
                {"id": "a1", "content": "explore", "priority": "high"},
                {"id": "a2", "content": "implement", "depends_on": ["a1"]},
            ],
        )
        await self.run_tool(action="complete", id="a1")
        await self.run_tool(action="start", id="a2")

        state = self.tool.to_dict()
        restored = TodosTool(Config(cwd=self.cwd))
        restored.from_dict(state)

        self.assertEqual(restored.to_dict(), state)

        listing = await restored.execute(
            ToolInvocation(params={"action": "list"}, cwd=self.cwd)
        )
        self.assertIn("[x] [a1]", listing.output)
        self.assertIn("[~] [a2]", listing.output)
        self.assertIn("depends on: a1", listing.output)

    async def test_from_dict_handles_empty_state(self):
        self.tool.from_dict(None)

        self.assertEqual(self.tool.to_dict(), {"todos": []})

    async def test_from_dict_defaults_unknown_fields(self):
        self.tool.from_dict(
            {"todos": [{"id": "a1", "content": "one", "status": "bogus"}]}
        )

        todo = self.tool.to_dict()["todos"][0]

        self.assertEqual(todo["status"], STATUS_PENDING)
        self.assertEqual(todo["priority"], "medium")

    async def test_status_constants_round_trip(self):
        await self.run_tool(action="plan", todos=[{"id": "a1", "content": "one"}])
        await self.run_tool(action="complete", id="a1")

        self.assertEqual(self.tool.to_dict()["todos"][0]["status"], STATUS_COMPLETED)


if __name__ == "__main__":
    unittest.main()
