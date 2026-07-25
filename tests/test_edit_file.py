import tempfile
import unittest
from pathlib import Path

from config.config import Config
from tools.base import ToolInvocation
from tools.builtin.edit_file import (
    EditTool,
    base_indent,
    detect_line_ending,
    normalize_line_endings,
    reindent,
    restore_line_endings,
)


class EditToolTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name)
        self.tool = EditTool(Config(cwd=self.cwd))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write(self, name: str, content: str, newline: str = "\n") -> Path:
        path = self.cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.replace("\n", newline).encode("utf-8"))
        return path

    def read_bytes(self, name: str) -> bytes:
        return (self.cwd / name).read_bytes()

    def read(self, name: str) -> str:
        return (self.cwd / name).read_text(encoding="utf-8")

    async def edit(self, **params):
        return await self.tool.execute(ToolInvocation(params=params, cwd=self.cwd))

    async def confirm(self, **params):
        return await self.tool.get_confirmation(
            ToolInvocation(params=params, cwd=self.cwd)
        )


class HelperTests(unittest.TestCase):
    def test_detect_line_ending(self):
        self.assertEqual(detect_line_ending("a\r\nb\r\n"), "\r\n")
        self.assertEqual(detect_line_ending("a\nb\n"), "\n")
        self.assertEqual(detect_line_ending(""), "\n")

    def test_normalize_and_restore_round_trip(self):
        raw = "a\r\nb\r\nc"

        normalized = normalize_line_endings(raw)

        self.assertEqual(normalized, "a\nb\nc")
        self.assertEqual(restore_line_endings(normalized, "\r\n"), raw)

    def test_normalize_handles_classic_mac_endings(self):
        self.assertEqual(normalize_line_endings("a\rb"), "a\nb")

    def test_base_indent_skips_blank_lines(self):
        self.assertEqual(base_indent("\n\n    value = 1"), "    ")
        self.assertEqual(base_indent("value = 1"), "")

    def test_reindent_moves_block_onto_new_anchor(self):
        text = "if x:\n    y = 1\n\n    z = 2"

        result = reindent(text, "", "        ")

        self.assertEqual(
            result,
            "        if x:\n            y = 1\n\n            z = 2",
        )

    def test_reindent_leaves_blank_lines_bare(self):
        self.assertEqual(reindent("a\n\nb", "", "  "), "  a\n\n  b")

    def test_reindent_is_identity_when_anchors_match(self):
        self.assertEqual(reindent("    a", "    ", "    "), "    a")


class ExactMatchTests(EditToolTestCase):
    async def test_replaces_single_exact_occurrence(self):
        self.write("a.py", "alpha\nbeta\ngamma\n")

        result = await self.edit(path="a.py", old_string="beta", new_string="delta")

        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("a.py"), "alpha\ndelta\ngamma\n")
        self.assertEqual(result.metadata["replaced_count"], 1)
        self.assertEqual(result.metadata["match_strategy"], "exact")

    async def test_multiline_exact_replacement(self):
        self.write("a.py", "start\nfoo\nbar\nend\n")

        result = await self.edit(
            path="a.py", old_string="foo\nbar", new_string="baz\nqux"
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("a.py"), "start\nbaz\nqux\nend\n")

    async def test_deleting_text_with_empty_new_string(self):
        self.write("a.py", "keep\nremove\n")

        result = await self.edit(path="a.py", old_string="remove\n", new_string="")

        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("a.py"), "keep\n")
        self.assertEqual(result.metadata["line_diff"], -1)

    async def test_exact_match_wins_over_fuzzy_candidates(self):
        # An exact hit exists on line 3; a whitespace-only variant exists too.
        self.write("a.py", "x  =  1\ny = 2\nx = 1\n")

        result = await self.edit(path="a.py", old_string="x = 1", new_string="x = 99")

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["match_strategy"], "exact")
        self.assertEqual(self.read("a.py"), "x  =  1\ny = 2\nx = 99\n")

    async def test_no_change_when_old_equals_new(self):
        self.write("a.py", "same\n")

        result = await self.edit(path="a.py", old_string="same", new_string="same")

        self.assertFalse(result.success)
        self.assertIn("No change made", result.error)


class NoMatchTests(EditToolTestCase):
    async def test_missing_old_string_reports_closest_region_diff(self):
        self.write(
            "a.py",
            "def compute(value):\n"
            "    total = value * 2\n"
            "    return total\n",
        )

        result = await self.edit(
            path="a.py",
            old_string="def compute(value):\n    total = value * 3\n    return total",
            new_string="whatever",
        )

        self.assertFalse(result.success)
        self.assertIn("old_string not found", result.error)
        self.assertIn("Closest match is at line 1", result.error)
        # The diff must show the actual file line, not a grep for "def".
        self.assertIn("+    total = value * 2", result.error)
        self.assertIn("-    total = value * 3", result.error)

    async def test_error_does_not_merely_grep_first_token(self):
        self.write("a.py", "def a():\n    pass\n\n\ndef b():\n    pass\n")

        result = await self.edit(
            path="a.py", old_string="def c():\n    raise", new_string="x"
        )

        self.assertFalse(result.success)
        self.assertIn("Closest match", result.error)
        self.assertNotIn("Possible similar lines", result.error)

    async def test_unrelated_old_string_gives_generic_guidance(self):
        self.write("a.py", "aaaa\n")

        result = await self.edit(
            path="a.py", old_string="zzzzzzzzzzzzzzz", new_string="y"
        )

        self.assertFalse(result.success)
        self.assertIn("read_file", result.error)

    async def test_file_unchanged_after_failed_match(self):
        original = "one\ntwo\n"
        self.write("a.py", original)

        await self.edit(path="a.py", old_string="three", new_string="four")

        self.assertEqual(self.read("a.py"), original)


class AmbiguousMatchTests(EditToolTestCase):
    async def test_multiple_exact_matches_are_rejected(self):
        self.write("a.py", "dup\nother\ndup\n")

        result = await self.edit(path="a.py", old_string="dup", new_string="new")

        self.assertFalse(result.success)
        self.assertIn("found 2 times", result.error)
        self.assertIn("replace_all=true", result.error)
        self.assertEqual(result.metadata["occurrence_count"], 2)
        self.assertEqual(self.read("a.py"), "dup\nother\ndup\n")

    async def test_ambiguity_error_lists_line_numbers(self):
        self.write("a.py", "dup\nother\ndup\n")

        result = await self.edit(path="a.py", old_string="dup", new_string="new")

        self.assertIn("line 1", result.error)
        self.assertIn("line 3", result.error)

    async def test_ambiguous_fuzzy_match_errors_instead_of_guessing(self):
        # Neither line matches exactly, both match once whitespace is collapsed.
        self.write("a.py", "    value  =  1\n        value   =  1\n")

        result = await self.edit(
            path="a.py", old_string="value = 1", new_string="value = 2"
        )

        self.assertFalse(result.success)
        self.assertIn("found 2 times", result.error)
        self.assertEqual(self.read("a.py"), "    value  =  1\n        value   =  1\n")


class ReplaceAllTests(EditToolTestCase):
    async def test_replace_all_exact(self):
        self.write("a.py", "x\ny\nx\nz\nx\n")

        result = await self.edit(
            path="a.py", old_string="x", new_string="q", replace_all=True
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["replaced_count"], 3)
        self.assertEqual(self.read("a.py"), "q\ny\nq\nz\nq\n")

    async def test_replace_all_fuzzy_reanchors_each_site(self):
        self.write("a.py", "    a  =  1\n        a =  1\n")

        result = await self.edit(
            path="a.py", old_string="a = 1", new_string="a = 2", replace_all=True
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["replaced_count"], 2)
        self.assertEqual(self.read("a.py"), "    a = 2\n        a = 2\n")

    async def test_replace_all_false_by_default(self):
        self.write("a.py", "x\nx\n")

        result = await self.edit(path="a.py", old_string="x", new_string="q")

        self.assertFalse(result.success)


class LineEndingTests(EditToolTestCase):
    async def test_lf_old_string_matches_crlf_file(self):
        self.write("crlf.py", "alpha\nbeta\ngamma\n", newline="\r\n")

        result = await self.edit(
            path="crlf.py", old_string="beta\ngamma", new_string="delta\nepsilon"
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(
            self.read_bytes("crlf.py"), b"alpha\r\ndelta\r\nepsilon\r\n"
        )

    async def test_crlf_file_keeps_crlf_after_edit(self):
        self.write("crlf.py", "one\ntwo\n", newline="\r\n")

        await self.edit(path="crlf.py", old_string="two", new_string="three")

        data = self.read_bytes("crlf.py")
        self.assertNotIn(b"\r\r", data)
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertEqual(data, b"one\r\nthree\r\n")

    async def test_lf_file_stays_lf(self):
        self.write("lf.py", "one\ntwo\n")

        await self.edit(path="lf.py", old_string="two", new_string="three")

        self.assertEqual(self.read_bytes("lf.py"), b"one\nthree\n")

    async def test_crlf_old_string_matches_lf_file(self):
        self.write("lf.py", "one\ntwo\nthree\n")

        result = await self.edit(
            path="lf.py", old_string="two\r\nthree", new_string="four\r\nfive"
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read_bytes("lf.py"), b"one\nfour\nfive\n")

    async def test_crlf_metadata_reports_line_ending(self):
        self.write("crlf.py", "one\ntwo\n", newline="\r\n")

        result = await self.edit(path="crlf.py", old_string="two", new_string="three")

        self.assertEqual(result.metadata["line_ending"], "crlf")

    async def test_multiline_replacement_inserts_crlf_for_new_lines(self):
        self.write("crlf.py", "a\nb\n", newline="\r\n")

        await self.edit(path="crlf.py", old_string="b", new_string="b1\nb2\nb3")

        self.assertEqual(self.read_bytes("crlf.py"), b"a\r\nb1\r\nb2\r\nb3\r\n")


class FuzzyFallbackTests(EditToolTestCase):
    async def test_indentation_tolerant_match_reanchors_replacement(self):
        self.write(
            "a.py",
            "def f():\n    if True:\n        a = 1\n        b = 2\n",
        )

        result = await self.edit(
            path="a.py", old_string="a = 1\nb = 2", new_string="a = 10\nb = 20"
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["match_strategy"], "indentation")
        self.assertEqual(
            self.read("a.py"),
            "def f():\n    if True:\n        a = 10\n        b = 20\n",
        )

    async def test_indentation_match_preserves_relative_nesting(self):
        self.write(
            "a.py",
            "class C:\n    def m(self):\n        if x:\n            y = 1\n",
        )

        result = await self.edit(
            path="a.py",
            old_string="if x:\n    y = 1",
            new_string="if x:\n    y = 2",
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(
            self.read("a.py"),
            "class C:\n    def m(self):\n        if x:\n            y = 2\n",
        )

    async def test_trailing_whitespace_in_file_is_tolerated(self):
        # The trailing spaces after "alpha" defeat an exact multi-line match.
        self.write("a.py", "alpha   \nbeta\ngamma\n")

        result = await self.edit(
            path="a.py", old_string="alpha\nbeta", new_string="omega\ndelta"
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["match_strategy"], "indentation")
        self.assertEqual(self.read("a.py"), "omega\ndelta\ngamma\n")

    async def test_whitespace_collapsed_match(self):
        self.write("a.py", "result   =   compute(  a,   b )\n")

        result = await self.edit(
            path="a.py",
            old_string="result = compute( a, b )",
            new_string="result = compute(a, b)",
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["match_strategy"], "whitespace")
        self.assertEqual(self.read("a.py"), "result = compute(a, b)\n")

    async def test_fuzzy_miss_still_errors(self):
        self.write("a.py", "completely different content\n")

        result = await self.edit(
            path="a.py", old_string="not\nin\nthe\nfile", new_string="x"
        )

        self.assertFalse(result.success)
        self.assertIn("old_string not found", result.error)

    async def test_whitespace_only_old_string_never_fuzzy_matches(self):
        self.write("a.py", "a\n    \nb\n")

        result = await self.edit(path="a.py", old_string="\n \n", new_string="\n\n")

        self.assertFalse(result.success)

    async def test_fuzzy_match_does_not_fire_across_different_code(self):
        self.write("a.py", "def alpha():\n    return 1\n")

        result = await self.edit(
            path="a.py", old_string="def beta():\n    return 1", new_string="x"
        )

        self.assertFalse(result.success)
        self.assertEqual(self.read("a.py"), "def alpha():\n    return 1\n")


class UnicodeTests(EditToolTestCase):
    async def test_replaces_non_ascii_text(self):
        self.write("u.py", "greeting = 'héllo wörld'\n# 日本語のコメント\n")

        result = await self.edit(
            path="u.py", old_string="héllo wörld", new_string="こんにちは"
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(
            self.read("u.py"), "greeting = 'こんにちは'\n# 日本語のコメント\n"
        )

    async def test_emoji_and_combining_characters_survive(self):
        self.write("u.py", "x = '🚀'\ny = 'é'\n")

        result = await self.edit(path="u.py", old_string="'🚀'", new_string="'🛰️'")

        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("u.py"), "x = '🛰️'\ny = 'é'\n")

    async def test_unicode_no_match_error_is_readable(self):
        self.write("u.py", "α = 1\n")

        result = await self.edit(path="u.py", old_string="β = 1", new_string="β = 2")

        self.assertFalse(result.success)
        self.assertIn("old_string not found", result.error)


class EmptyAndNewFileTests(EditToolTestCase):
    async def test_edit_on_empty_existing_file_errors(self):
        self.write("empty.py", "")

        result = await self.edit(path="empty.py", old_string="x", new_string="y")

        self.assertFalse(result.success)
        self.assertIn("old_string not found", result.error)

    async def test_empty_old_string_on_existing_file_errors(self):
        self.write("a.py", "content\n")

        result = await self.edit(path="a.py", old_string="", new_string="new")

        self.assertFalse(result.success)
        self.assertIn("write_file", result.error)
        self.assertEqual(self.read("a.py"), "content\n")

    async def test_empty_old_string_on_empty_existing_file_errors(self):
        self.write("empty.py", "")

        result = await self.edit(path="empty.py", old_string="", new_string="new")

        self.assertFalse(result.success)
        self.assertEqual(self.read("empty.py"), "")

    async def test_creates_new_file_with_empty_old_string(self):
        result = await self.edit(
            path="nested/new.py", old_string="", new_string="print('hi')\n"
        )

        self.assertTrue(result.success, result.error)
        self.assertTrue(result.metadata["is_new_file"])
        self.assertEqual(self.read("nested/new.py"), "print('hi')\n")

    async def test_missing_file_with_old_string_errors(self):
        result = await self.edit(path="ghost.py", old_string="x", new_string="y")

        self.assertFalse(result.success)
        self.assertIn("does not exist", result.error)


class ConfirmationTests(EditToolTestCase):
    async def test_confirmation_diff_reflects_the_edit(self):
        self.write("a.py", "alpha\nbeta\n")

        confirmation = await self.confirm(
            path="a.py", old_string="beta", new_string="gamma"
        )

        self.assertIsNotNone(confirmation.diff)
        self.assertEqual(confirmation.diff.new_content, "alpha\ngamma\n")

    async def test_confirmation_for_crlf_file_shows_a_real_change(self):
        self.write("crlf.py", "alpha\nbeta\n", newline="\r\n")

        confirmation = await self.confirm(
            path="crlf.py", old_string="beta", new_string="gamma"
        )

        self.assertIsNotNone(confirmation.diff)
        self.assertNotEqual(
            confirmation.diff.old_content, confirmation.diff.new_content
        )

    async def test_confirmation_omits_diff_when_edit_cannot_apply(self):
        self.write("a.py", "alpha\n")

        confirmation = await self.confirm(
            path="a.py", old_string="missing", new_string="x"
        )

        self.assertIsNone(confirmation.diff)

    async def test_confirmation_for_new_file(self):
        confirmation = await self.confirm(
            path="brand_new.py", old_string="", new_string="hello"
        )

        self.assertTrue(confirmation.diff.is_new_file)


if __name__ == "__main__":
    unittest.main()
