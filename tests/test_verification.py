import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from agent.verification import Verifier, VerificationResult, _truncate
from config.config import Config


def config_for(path: Path) -> Config:
    config = Config()
    config.cwd = path
    return config


class DetectCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def verifier(self) -> Verifier:
        return Verifier(config_for(self.root))

    def test_explicit_config_command_wins(self):
        (self.root / "Cargo.toml").write_text("[package]", encoding="utf-8")
        config = config_for(self.root)
        config.verification.command = "just check"

        self.assertEqual(Verifier(config).detect_command(), "just check")

    def test_npm_test_when_script_present(self):
        (self.root / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}}), encoding="utf-8"
        )

        self.assertEqual(self.verifier().detect_command(), "npm test")

    def test_package_json_without_test_script_is_skipped(self):
        (self.root / "package.json").write_text(
            json.dumps({"scripts": {"build": "tsc"}}), encoding="utf-8"
        )

        self.assertIsNone(self.verifier().detect_command())

    def test_malformed_package_json_does_not_raise(self):
        (self.root / "package.json").write_text("{not json", encoding="utf-8")

        self.assertIsNone(self.verifier().detect_command())

    def test_cargo_and_go(self):
        (self.root / "Cargo.toml").write_text("[package]", encoding="utf-8")
        self.assertEqual(self.verifier().detect_command(), "cargo test")

        (self.root / "Cargo.toml").unlink()
        (self.root / "go.mod").write_text("module x", encoding="utf-8")
        self.assertEqual(self.verifier().detect_command(), "go test ./...")

    def test_pytest_detected_from_pyproject(self):
        (self.root / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pytest"]', encoding="utf-8"
        )

        self.assertEqual(self.verifier().detect_command(), "python -m pytest -q")

    def test_unittest_fallback_when_tests_dir_exists(self):
        (self.root / "pyproject.toml").write_text("[project]", encoding="utf-8")
        (self.root / "tests").mkdir()

        self.assertEqual(
            self.verifier().detect_command(), "python -m unittest discover -s tests"
        )

    def test_makefile_test_target(self):
        (self.root / "Makefile").write_text("build:\n\tcc x\ntest:\n\t./t\n", encoding="utf-8")

        self.assertEqual(self.verifier().detect_command(), "make test")

    def test_no_signal_returns_none(self):
        self.assertIsNone(self.verifier().detect_command())


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_with(self, command: str, timeout: float = 30) -> VerificationResult:
        config = config_for(self.root)
        config.verification.command = command
        config.verification.timeout_seconds = timeout

        return asyncio.run(Verifier(config).run())

    def test_passing_command(self):
        result = self.run_with("exit 0")

        self.assertTrue(result.passed)
        self.assertFalse(result.timed_out)

    def test_failing_command_captures_output(self):
        result = self.run_with("echo 'assertion failed' >&2; exit 1")

        self.assertFalse(result.passed)
        self.assertIn("assertion failed", result.output)

    def test_timeout_is_reported(self):
        result = self.run_with("sleep 5", timeout=1)

        self.assertFalse(result.passed)
        self.assertTrue(result.timed_out)

    def test_no_command_returns_none(self):
        self.assertIsNone(asyncio.run(Verifier(config_for(self.root)).run()))

    def test_attempts_are_counted(self):
        config = config_for(self.root)
        config.verification.command = "exit 0"
        config.verification.max_attempts = 2
        verifier = Verifier(config)

        self.assertTrue(verifier.attempts_remaining())
        asyncio.run(verifier.run())
        asyncio.run(verifier.run())
        self.assertFalse(verifier.attempts_remaining())


class FeedbackTests(unittest.TestCase):
    def test_failure_feedback_includes_command_and_output(self):
        feedback = VerificationResult(
            command="pytest", passed=False, output="2 failed"
        ).to_feedback()

        self.assertIn("pytest", feedback)
        self.assertIn("2 failed", feedback)
        self.assertIn("not complete", feedback)

    def test_timeout_feedback_says_timed_out(self):
        feedback = VerificationResult(
            command="pytest", passed=False, output="", timed_out=True
        ).to_feedback()

        self.assertIn("timed out", feedback)

    def test_truncate_keeps_head_and_tail(self):
        text = "A" * 5_000 + "MIDDLE" + "B" * 5_000
        truncated = _truncate(text)

        self.assertLess(len(truncated), len(text))
        self.assertTrue(truncated.startswith("A"))
        self.assertTrue(truncated.endswith("B"))
        self.assertNotIn("MIDDLE", truncated)
        self.assertIn("omitted", truncated)


if __name__ == "__main__":
    unittest.main()
