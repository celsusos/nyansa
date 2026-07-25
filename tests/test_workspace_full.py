from pathlib import Path
import tempfile
import unittest

from agent.workspace_snapshot import capture_workspace, restore_files


class CaptureWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def write(self, rel: str, content: bytes = b"x") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def paths_in(self, snapshot) -> set[str]:
        return {
            Path(e["path"]).name
            for e in snapshot["files"]
            if "content" in e
        }

    def test_captures_non_excluded_files(self):
        self.write("src/app.py", b"print(1)")
        self.write("README.md", b"# hi")
        snapshot = capture_workspace(self.root)

        self.assertTrue(snapshot["full"])
        self.assertIn("app.py", self.paths_in(snapshot))
        self.assertIn("README.md", self.paths_in(snapshot))

    def test_excludes_vendored_and_gitignored(self):
        self.write(".gitignore", b"secret.txt\n")
        self.write("keep.py", b"1")
        self.write("node_modules/pkg/index.js", b"1")
        self.write(".git/config", b"1")
        self.write("secret.txt", b"1")

        captured = self.paths_in(capture_workspace(self.root))

        self.assertIn("keep.py", captured)
        self.assertNotIn("index.js", captured)
        self.assertNotIn("config", captured)
        self.assertNotIn("secret.txt", captured)

    def test_shell_style_change_is_reverted(self):
        target = self.write("data.txt", b"original")
        snapshot = capture_workspace(self.root)

        target.write_bytes(b"mutated by sed -i")
        summary = restore_files(snapshot, self.root)

        self.assertEqual(target.read_bytes(), b"original")
        self.assertTrue(any("data.txt" in p for p in summary.reverted))

    def test_file_created_after_checkpoint_is_deleted(self):
        self.write("keep.py", b"1")
        snapshot = capture_workspace(self.root)

        created = self.write("scratch/new.py", b"created later")
        summary = restore_files(snapshot, self.root)

        self.assertFalse(created.exists())
        self.assertTrue(any("new.py" in p for p in summary.deleted))

    def test_renamed_file_both_restored_and_removed(self):
        original = self.write("a.txt", b"body")
        snapshot = capture_workspace(self.root)

        # emulate `mv a.txt b.txt`
        moved = self.root / "b.txt"
        original.rename(moved)

        restore_files(snapshot, self.root)

        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), b"body")
        self.assertFalse(moved.exists())

    def test_extra_paths_are_always_included(self):
        self.write(".gitignore", b"build/\n")
        ignored = self.write("build/out.py", b"tracked edit")

        snapshot = capture_workspace(self.root, extra_paths=[ignored])

        self.assertIn("out.py", self.paths_in(snapshot))

    def test_unchanged_workspace_restore_is_noop(self):
        self.write("a.py", b"1")
        self.write("b.py", b"2")
        snapshot = capture_workspace(self.root)

        summary = restore_files(snapshot, self.root)

        self.assertEqual(summary.deleted, [])
        self.assertEqual(self.paths_in(snapshot), {"a.py", "b.py"})

    def test_prune_does_not_touch_excluded_files(self):
        self.write(".gitignore", b"logs/\n")
        snapshot = capture_workspace(self.root)

        log = self.write("logs/run.log", b"noise created later")
        restore_files(snapshot, self.root)

        self.assertTrue(log.exists())


if __name__ == "__main__":
    unittest.main()
