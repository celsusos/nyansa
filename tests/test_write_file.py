import os
import tempfile
import unittest
from pathlib import Path

from config.config import Config
from tools.base import ToolInvocation
from tools.builtin.write_file import WriteFileTool, read_existing_content


class WriteFileTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name)
        self.tool = WriteFileTool(Config(cwd=self.cwd))

    def tearDown(self) -> None:
        for path in self.cwd.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self._tmpdir.cleanup()

    async def write(self, **params):
        return await self.tool.execute(ToolInvocation(params=params, cwd=self.cwd))

    async def confirm(self, **params):
        return await self.tool.get_confirmation(
            ToolInvocation(params=params, cwd=self.cwd)
        )


class ReadExistingContentTests(WriteFileTestCase):
    def test_returns_content_for_readable_text(self):
        path = self.cwd / "a.txt"
        path.write_text("hello", encoding="utf-8")

        content, error = read_existing_content(path)

        self.assertEqual(content, "hello")
        self.assertIsNone(error)

    def test_reports_error_for_non_utf8_file(self):
        path = self.cwd / "bin.dat"
        path.write_bytes(b"\xff\xfe\x00binary")

        content, error = read_existing_content(path)

        self.assertIsNone(content)
        self.assertIn("UTF-8", error)

    def test_reports_error_for_unreadable_file(self):
        path = self.cwd / "locked.txt"
        path.write_text("secret", encoding="utf-8")
        path.chmod(0o000)

        if os.access(path, os.R_OK):
            self.skipTest("file permissions are not enforced for this user")

        content, error = read_existing_content(path)

        self.assertIsNone(content)
        self.assertIn("could not be read", error)


class OverwriteDiffTests(WriteFileTestCase):
    async def test_normal_overwrite_reports_previous_content(self):
        (self.cwd / "a.txt").write_text("old", encoding="utf-8")

        result = await self.write(path="a.txt", content="new")

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.diff.old_content, "old")
        self.assertTrue(result.metadata["old_content_available"])

    async def test_unreadable_file_does_not_produce_a_bogus_empty_diff(self):
        path = self.cwd / "bin.dat"
        path.write_bytes(b"\xff\xfe\x00binary")

        result = await self.write(path="bin.dat", content="replacement")

        self.assertTrue(result.success, result.error)
        # The old behaviour silently claimed old_content == "".
        self.assertIsNone(result.diff)
        self.assertFalse(result.metadata["old_content_available"])
        self.assertIn("WARNING", result.output)
        self.assertEqual(
            path.read_text(encoding="utf-8"), "replacement"
        )

    async def test_confirmation_warns_when_previous_content_is_unreadable(self):
        path = self.cwd / "bin.dat"
        path.write_bytes(b"\xff\xfe\x00binary")

        confirmation = await self.confirm(path="bin.dat", content="replacement")

        self.assertIsNone(confirmation.diff)
        self.assertIn("WARNING", confirmation.description)
        self.assertTrue(confirmation.is_dangerous)

    async def test_new_file_still_gets_a_diff(self):
        result = await self.write(path="fresh.txt", content="hello")

        self.assertTrue(result.success, result.error)
        self.assertTrue(result.diff.is_new_file)
        self.assertEqual(result.diff.old_content, "")
        self.assertTrue(result.metadata["old_content_available"])

    async def test_creates_parent_directories(self):
        result = await self.write(path="a/b/c.txt", content="deep")

        self.assertTrue(result.success, result.error)
        self.assertEqual(
            (self.cwd / "a" / "b" / "c.txt").read_text(encoding="utf-8"), "deep"
        )

    async def test_missing_parent_without_create_directories_errors(self):
        result = await self.write(
            path="missing/c.txt", content="x", create_directories=False
        )

        self.assertFalse(result.success)
        self.assertIn("Parent directory does not exist", result.error)


if __name__ == "__main__":
    unittest.main()
