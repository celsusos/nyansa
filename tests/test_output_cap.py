import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_ERROR_TOKENS,
    ToolResult,
    cap_output,
)
from utils.paths import resolve_path
from utils.text import count_tokens

TRUNCATION_MARKER = "[OUTPUT TRUNCATED:"


def oversized_text(tokens: int = DEFAULT_MAX_OUTPUT_TOKENS * 2) -> str:
    return "\n".join(f"line {i} padding padding padding" for i in range(tokens))


class TestCapOutput(unittest.TestCase):
    def test_short_output_is_untouched(self):
        output, truncated = cap_output("hello", hint="hint")

        self.assertEqual(output, "hello")
        self.assertFalse(truncated)

    def test_empty_output_is_untouched(self):
        output, truncated = cap_output("", hint="hint")

        self.assertEqual(output, "")
        self.assertFalse(truncated)

    def test_oversized_output_is_capped(self):
        output, truncated = cap_output(oversized_text(), hint="narrow it down")

        self.assertTrue(truncated)
        self.assertIn(TRUNCATION_MARKER, output)
        self.assertIn("narrow it down", output)
        self.assertLessEqual(count_tokens(output), DEFAULT_MAX_OUTPUT_TOKENS)

    def test_marker_reports_dropped_tokens(self):
        text = oversized_text()
        output, _ = cap_output(text, hint="hint")

        dropped = count_tokens(text) - DEFAULT_MAX_OUTPUT_TOKENS

        self.assertIn(str(dropped), output)


class TestToModelOutputGlobalCap(unittest.TestCase):
    def test_uncapped_success_output_is_capped(self):
        result = ToolResult.success_result(oversized_text())

        rendered = result.to_model_output()

        self.assertIn(TRUNCATION_MARKER, rendered)
        self.assertLessEqual(count_tokens(rendered), DEFAULT_MAX_OUTPUT_TOKENS)

    def test_cap_sets_truncated_flag(self):
        result = ToolResult.success_result(oversized_text())
        self.assertFalse(result.truncated)

        result.to_model_output()

        self.assertTrue(result.truncated)

    def test_small_output_is_unchanged_and_not_flagged(self):
        result = ToolResult.success_result("all good")

        self.assertEqual(result.to_model_output(), "all good")
        self.assertFalse(result.truncated)

    def test_already_truncated_output_within_budget_is_left_alone(self):
        already = "short output\n\n[OUTPUT TRUNCATED: by the tool itself]"
        result = ToolResult.success_result(already, truncated=True)

        rendered = result.to_model_output()

        self.assertEqual(rendered, already)
        self.assertEqual(rendered.count(TRUNCATION_MARKER), 1)

    def test_cap_is_idempotent(self):
        result = ToolResult.success_result(oversized_text())

        first = result.to_model_output()
        second = result.to_model_output()

        self.assertEqual(first, second)
        self.assertEqual(second.count(TRUNCATION_MARKER), 1)

    def test_repeated_capping_does_not_shrink_further(self):
        result = ToolResult.success_result(oversized_text())

        first = result.to_model_output()
        result.output = first
        second = result.to_model_output()

        self.assertEqual(first, second)


class TestToModelOutputErrors(unittest.TestCase):
    def test_error_prefix_survives_truncation(self):
        result = ToolResult.error_result("disk is on fire", output=oversized_text())

        rendered = result.to_model_output()

        self.assertTrue(rendered.startswith("Error: disk is on fire"))
        self.assertIn(TRUNCATION_MARKER, rendered)
        self.assertTrue(result.truncated)

    def test_error_body_is_capped_not_the_error(self):
        result = ToolResult.error_result("boom", output=oversized_text())

        rendered = result.to_model_output()

        self.assertIn("Error: boom", rendered)
        self.assertLessEqual(
            count_tokens(rendered), DEFAULT_MAX_OUTPUT_TOKENS + MAX_ERROR_TOKENS
        )

    def test_oversized_error_text_is_itself_capped(self):
        result = ToolResult.error_result(oversized_text(), output="")

        rendered = result.to_model_output()

        self.assertTrue(rendered.startswith("Error: "))
        self.assertIn(TRUNCATION_MARKER, rendered)
        self.assertTrue(result.truncated)

    def test_small_error_is_unchanged(self):
        result = ToolResult.error_result("not found", output="context")

        self.assertEqual(
            result.to_model_output(), "Error: not found\n\nOutput:\ncontext"
        )
        self.assertFalse(result.truncated)


class TestResolvePathSymmetry(unittest.TestCase):
    def test_relative_path_is_fully_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            resolved = resolve_path(base, "sub/../target.txt")

            self.assertEqual(resolved, base.resolve() / "target.txt")

    def test_relative_and_absolute_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            from_relative = resolve_path(base, "file.txt")
            from_absolute = resolve_path(base, base.resolve() / "file.txt")

            self.assertEqual(from_relative, from_absolute)

    def test_symlinked_base_resolves_to_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp).resolve() / "real"
            real.mkdir()

            link = Path(tmp).resolve() / "link"
            os.symlink(real, link)

            resolved = resolve_path(link, "file.txt")

            self.assertEqual(resolved, real / "file.txt")

    def test_symlinked_base_stays_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp).resolve() / "workspace"
            real.mkdir()

            link = Path(tmp).resolve() / "workspace-link"
            os.symlink(real, link)

            resolved = resolve_path(link, "nested/file.txt")

            self.assertTrue(resolved.is_relative_to(Path(link).resolve()))

    def test_absolute_path_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()

            resolved = resolve_path(base, str(base / "a" / ".." / "b.txt"))

            self.assertEqual(resolved, base / "b.txt")


if __name__ == "__main__":
    unittest.main()
