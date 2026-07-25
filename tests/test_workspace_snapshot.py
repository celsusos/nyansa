import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from agent.persistence import PersistenceManager, SessionSnapshot
from agent.workspace_snapshot import capture_files, restore_files
from client.response import TokenUsage


def make_snapshot(**overrides):
    data = {
        "session_id": "sess-1",
        "created_at": datetime(2026, 1, 1, 12, 0, 0),
        "updated_at": datetime(2026, 1, 1, 12, 5, 0),
        "turn_count": 3,
        "messages": [{"role": "user", "content": "hi"}],
        "total_usage": TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    }
    data.update(overrides)
    return SessionSnapshot(**data)


class SessionSnapshotTests(unittest.TestCase):
    def test_round_trips_binary_file_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blob.bin"
            target.write_bytes(bytes(range(256)))
            files = capture_files([target])

            snapshot = make_snapshot(files=files)
            revived = SessionSnapshot.from_dict(
                json.loads(json.dumps(snapshot.to_dict()))
            )

            self.assertEqual(revived.files, files)

            target.write_bytes(b"clobbered")
            restore_files(revived.files, tmp)
            self.assertEqual(target.read_bytes(), bytes(range(256)))

    def test_persistence_lists_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("agent.persistence.get_data_dir", return_value=Path(tmp)):
                manager = PersistenceManager()
                checkpoint_id = manager.save_checkpoint(make_snapshot())

                checkpoints = manager.list_checkpoints()

        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["checkpoint_id"], checkpoint_id)
        self.assertEqual(checkpoints[0]["session_id"], "sess-1")

    def test_old_snapshot_without_files_field_loads(self):
        snapshot = make_snapshot()
        data = snapshot.to_dict()
        del data["files"]

        revived = SessionSnapshot.from_dict(data)

        self.assertEqual(revived.files, {})


class CaptureRestoreTests(unittest.TestCase):
    def test_checkpoint_captures_then_restores_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.txt"
            target.write_text("original")
            files = capture_files([target])

            target.write_text("modified after checkpoint")
            summary = restore_files(files, tmp)

            self.assertEqual(target.read_text(), "original")
            self.assertIn(str(target), summary.reverted)

    def test_file_created_after_checkpoint_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "created.txt"
            files = capture_files([target])

            self.assertFalse(target.exists())
            target.write_text("new file")

            summary = restore_files(files, tmp)

            self.assertFalse(target.exists())
            self.assertIn(str(target), summary.deleted)

    def test_path_outside_cwd_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "victim.txt"
            outside.write_text("do not touch")
            files = capture_files([outside])

            outside.write_text("changed")
            summary = restore_files(files, tmp)

            self.assertEqual(outside.read_text(), "changed")
            self.assertTrue(any(reason == "outside-cwd" for _, reason in summary.skipped))
            self.assertNotIn(str(outside), summary.reverted)

    def test_per_file_cap_drops_oversized_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "big.bin"
            target.write_bytes(b"x" * 2048)

            files = capture_files([target], max_file_bytes=1024)
            entry = files["files"][0]

            self.assertEqual(entry["skipped"], "file-too-large")
            self.assertNotIn("content", entry)

            target.write_bytes(b"y" * 10)
            summary = restore_files(files, tmp)
            self.assertEqual(target.read_bytes(), b"y" * 10)
            self.assertTrue(
                any(reason == "file-too-large" for _, reason in summary.skipped)
            )

    def test_total_cap_drops_later_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.bin"
            second = Path(tmp) / "b.bin"
            first.write_bytes(b"a" * 800)
            second.write_bytes(b"b" * 800)

            files = capture_files([first, second], max_total_bytes=1000)
            by_path = {e["path"]: e for e in files["files"]}

            kept = [p for p, e in by_path.items() if "content" in e]
            dropped = [p for p, e in by_path.items() if e.get("skipped") == "total-limit"]

            self.assertEqual(len(kept), 1)
            self.assertEqual(len(dropped), 1)

    def test_directory_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            some_dir = Path(tmp) / "subdir"
            files = capture_files([some_dir])

            some_dir.mkdir()
            summary = restore_files(files, tmp)

            self.assertTrue(some_dir.is_dir())
            self.assertNotIn(str(some_dir), summary.deleted)

    def test_missing_snapshot_source_records_nonexistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ghost.txt"
            files = capture_files([target])

            self.assertFalse(files["files"][0]["existed"])


if __name__ == "__main__":
    unittest.main()
